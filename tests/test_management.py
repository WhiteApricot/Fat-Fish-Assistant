"""Batch 1 management regressions using isolated synthetic instances."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fat_fish_management import controller as c, windows
from fat_fish_management.components import communication_health, lifecycle, snowluma_status
from fat_fish_management.configure_onebot import configure
from fat_fish_management.instance import require_verified_instance, initialize
from fat_fish_management.lifecycle_lock import exclusive

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fat_fish.adapters.connection_health import probe


class ManagementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = c.owned_root(self.root / 'install', create=True)
        self.instance = self.installation / 'instance'
        (self.instance / 'private').mkdir(parents=True)
        (self.instance / 'data').mkdir()
        self.token = 'synthetic-test-secret'
        self.platform = dict(id='feiyu-onebot', type='aiocqhttp', enable=True,
                             ws_reverse_host='127.0.0.1', ws_reverse_port=6199,
                             ws_reverse_token=self.token)
        self.config = self.instance / 'data/cmd_config.json'
        c.atomic(self.config, {'platform': [self.platform]})
        (self.instance / 'private/onebot-token.txt').write_text(self.token)

    def dispatch(self, *args):
        return c.dispatch(c.parser().parse_args(['--root', str(self.installation), *args]))

    def official_layout(self):
        path = self.root / 'official'
        path.mkdir()
        (path / 'launcher.bat').write_text('exit /b 99\n')
        c.atomic(path / 'release.json', {'version': '2.4.2', 'target': 'win32-x64'})
        return path

    def test_component_selection_preserves_dependencies(self):
        self.assertEqual(c.selection('chat,snowluma'), {'core', 'plugins', 'astrbot', 'python', 'snowluma'})
        self.assertIn('speech', c.selection('speech'))
        with self.assertRaises(c.Fault):
            c.selection('work')

    def test_health_before_install_is_diagnostic(self):
        result = self.dispatch('health')
        self.assertEqual(result['connection']['model_call'], 'not_checked')
        self.assertFalse(result['connection']['onebot']['connected'])
        self.assertIsNone(result['connection']['qq_login'])
        self.assertEqual({row['id']: row['state'] for row in result['components']}['astrbot'],
                         'not_installed')

    def test_unregistered_transport_is_unknown(self):
        result = snowluma_status({})
        self.assertEqual(result['state'], 'waiting_for_user')
        self.assertIsNone(result['program_present'])
        self.assertIsNone(result['running'])

    @unittest.skipUnless(os.name == 'nt', 'Windows official release layout')
    def test_register_and_recheck_official_program(self):
        official = self.official_layout()
        before = {p.name: p.read_bytes() for p in official.iterdir()}
        self.assertIsNone(windows.snowluma_launcher(official, '0.0.0'))
        self.assertTrue(self.dispatch('register-external', 'snowluma', '--path', str(official),
                                      '--version', '2.4.2')['registered_read_only'])
        status = self.dispatch('component', 'snowluma', 'health')
        self.assertTrue(status['program_present'])
        self.assertEqual(status['ownership'], 'official-external')
        self.assertIsNone(status['running'])
        for action in ('start', 'stop', 'restart'):
            result = self.dispatch('component', 'snowluma', action)
            self.assertFalse(result['complete'])
            self.assertEqual(result['operations'][0]['requested_action'], action)
        self.assertEqual(before, {p.name: p.read_bytes() for p in official.iterdir()})
        (official / 'launcher.bat').unlink()
        self.assertEqual(self.dispatch('component', 'snowluma', 'status')['state'], 'unhealthy')

    def test_registration_rejects_managed_directory(self):
        with self.assertRaises(c.Fault) as exc:
            self.dispatch('register-external', 'snowluma', '--path', str(self.installation),
                          '--version', '2.4.2')
        self.assertEqual(exc.exception.kind, 'read_only')

    def test_snowluma_install_requests_official_flow_without_creating_root(self):
        target = self.root / 'not-created'
        options = c.parser().parse_args(['--root', str(target), 'install', '--components', 'snowluma'])
        with self.assertRaises(c.Fault) as exc:
            c.dispatch(options)
        self.assertEqual(exc.exception.kind, 'needs_input')
        self.assertFalse(target.exists())

    def test_unknown_installation_components_are_refused_without_mutation(self):
        metadata = self.installation / 'installation.json'
        c.atomic(metadata, {'schema_version': 1, 'components': {'unsupported': {'installed': True}}})
        before = metadata.read_bytes()
        with self.assertRaises(c.Fault) as exc:
            self.dispatch('status')
        self.assertEqual(exc.exception.kind, 'incompatible')
        self.assertEqual(metadata.read_bytes(), before)

    def test_lifecycle_distinguishes_install_init_process_and_fault(self):
        component = dict(installed=True, program_present=True, running=None)
        self.assertEqual(lifecycle(component)['state'], 'installed')
        self.assertEqual(lifecycle(component, initialized=False)['state'], 'not_initialized')
        self.assertEqual(lifecycle(dict(component, running=True))['state'], 'running')
        self.assertEqual(lifecycle(dict(component, running=False))['state'], 'stopped')
        self.assertEqual(lifecycle(dict(component, running=True, dirty=True))['state'], 'unhealthy')
        self.assertEqual(lifecycle(dict(component, program_present=False))['state'], 'unhealthy')

    def test_runtime_binding_is_idempotent_and_keeps_private_token(self):
        original = self.config.read_bytes()
        for _ in range(2):
            result = configure(self.instance, '100000001')
            self.assertEqual(result['reverse_ws_url'], 'ws://127.0.0.1:6199/ws')
            self.assertNotIn(self.token, json.dumps(result))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(c.read(self.instance / 'private/bot-identity.json'), {'bot_id': '100000001'})
        with self.assertRaisesRegex(ValueError, 'differs'):
            configure(self.instance, '100000002')

    def test_verified_instance_initializes_local_databases_and_registers_bound_bot(self):
        from fat_fish.communication.model import ProviderId
        from fat_fish_management.instance import require_platform_gateway_release, require_verified_instance
        from fat_fish.communication.registry import ProviderRegistry
        from fat_fish.core.identity import IdentityDirectory
        from fat_fish.memory.service import MemoryService

        target = self.root / 'verified'
        release = self.root / 'release'
        (release / 'plugins').mkdir(parents=True)
        (release / 'payload-manifest.json').write_text('[]')
        ffmpeg = SimpleNamespace(get_ffmpeg_exe=lambda: 'fixture')
        with patch.dict(sys.modules, {'imageio_ffmpeg': ffmpeg}), patch(
            'fat_fish_management.instance.link_or_copy'
        ), patch('fat_fish_management.instance.protect_boundary'), patch(
            'fat_fish_management.instance.subprocess.run'
        ):
            initialize(target, release, seed_persona=False)
        require_verified_instance(target)
        gateway_entry = release / 'astrbot/astrbot/core/fat_fish_gateway.py'
        gateway_entry.parent.mkdir(parents=True)
        gateway_entry.write_text('from fat_fish.gateway.scoped_gateway import ScopedGateway')
        with self.assertRaisesRegex(ValueError, 'retired gateway'):
            require_platform_gateway_release(release)
        gateway_entry.write_text('from fat_fish.gateway.platform_gateway import PlatformGateway')
        require_platform_gateway_release(release)
        private = target / 'private'
        self.assertFalse((private / 'p2c-access.json').exists())
        with self.assertRaises(c.Fault):
            c.parser().parse_args(['install', '--owner', '12345'])
        self.assertEqual(IdentityDirectory(private / 'ffa-identities.sqlite3').owner_account(), None)
        self.assertEqual(MemoryService(private / 'ffa-memory.sqlite3').status()['activities'], 0)
        from fat_fish_management.data_integrity import validate
        with patch('fat_fish_management.data_integrity.subprocess.run') as native_validator:
            self.assertEqual(validate(target, release)['native_memory'], 'unused')
        native_validator.assert_not_called()
        registry = ProviderRegistry(private / 'ffa-communication.json')
        self.assertEqual(registry.configured(), {})
        c.atomic(target / 'data/cmd_config.json', {'platform': [self.platform]})
        (private / 'onebot-token.txt').write_text(self.token)
        for _ in range(2):
            configure(target, '100000001')
        self.assertEqual(list(registry.configured()), [ProviderId('qq', 'feiyu-onebot')])

    def test_binding_rejects_bad_listener_and_token(self):
        for field, value in [('ws_reverse_host', '0.0.0.0'), ('ws_reverse_port', True),
                             ('ws_reverse_token', 'wrong-token'), ('enable', False)]:
            with self.subTest(field=field):
                c.atomic(self.config, {'platform': [dict(self.platform, **{field: value})]})
                with self.assertRaises(ValueError):
                    configure(self.instance, '100000001')
        self.assertFalse((self.instance / 'private/bot-identity.json').exists())

    def test_second_qq_adapter_is_refused(self):
        for other in ({'type': 'qq_official'}, {'type': 'qq_official_webhook'},
                      dict(self.platform, id='another-onebot')):
            with self.subTest(other=other['type']):
                c.atomic(self.config, {'platform': [self.platform, dict(other, enable=True)]})
                with self.assertRaisesRegex(ValueError, 'only the managed SnowLuma'):
                    configure(self.instance, '100000001')
        self.assertFalse((self.instance / 'private/bot-identity.json').exists())

    def test_binding_respects_live_writer_lock_and_dirty_marker(self):
        with exclusive(self.instance):
            with self.assertRaises(RuntimeError):
                configure(self.instance, '100000001')
        (self.instance / 'private/DIRTY').touch()
        with self.assertRaisesRegex(ValueError, 'Stop FFA cleanly'):
            configure(self.instance, '100000001')

    def test_lock_released_after_error_and_does_not_append(self):
        lock = self.instance / 'private/.instance-lock'
        lock.write_bytes(b'0')
        with self.assertRaisesRegex(ValueError, 'test exception'):
            with exclusive(self.instance):
                raise ValueError('test exception')
        for _ in range(3):
            with exclusive(self.instance):
                with self.assertRaises(RuntimeError):
                    with exclusive(self.instance):
                        self.fail('lock unexpectedly acquired')
        self.assertEqual(lock.read_bytes(), b'0')

    def test_installation_lock_reports_busy(self):
        with c.installation_lock(self.installation):
            with self.assertRaises(c.Fault) as exc:
                with c.installation_lock(self.installation):
                    self.fail('installation lock unexpectedly acquired')
        self.assertEqual(exc.exception.kind, 'busy')

    def test_public_cli_returns_pending_without_launching_external_process(self):
        completed = subprocess.run(
            [sys.executable, '-B', '-I', 'ffa.py', '--root', str(self.installation), 'restart'],
            capture_output=True, text=True, check=True,
        )
        result = json.loads(completed.stdout)
        self.assertFalse(result['data']['complete'])
        self.assertEqual(result['data']['operations'][0]['id'], 'snowluma')

    def test_windows_diagnostics_routes_both_interpreters(self):
        chat = self.root / 'chat-python.exe'
        speech = self.root / 'speech-python.exe'
        record = {'schema_version': 1, 'components': {'python': {'installed': True}},
                  'chat_python': str(chat), 'speech_python': str(speech)}
        c.atomic(self.installation / 'installation.json', record)
        with patch.object(windows, 'verify_environment', return_value={'checked': True}) as verify:
            self.assertTrue(self.dispatch('component', 'python', 'integrity')['checked'])
            verify.assert_called_once_with(self.installation, chat, speech)

    def test_migration_plan_accepts_only_ffa_components_and_current_schema(self):
        try:
            from fat_fish_management.migration import LOCKS, validate_snapshot
        except ModuleNotFoundError as exc:
            if exc.name == 'cryptography':
                self.skipTest('Run with FFA runtime dependencies to check migration plans')
            raise
        snapshot = self.root / 'snapshot'
        c.atomic(snapshot / 'manifest.json', {'schema': 2, 'files': []})
        plan = {'schema': 2, 'components': ['python', 'astrbot', 'plugins'],
                'locks': {name: c.digest(c.PROJECT / 'manifests' / name) for name in LOCKS}}
        path = snapshot / 'installation-plan.json'
        c.atomic(path, plan)
        self.assertEqual(validate_snapshot(snapshot)[1], plan)
        c.atomic(path, dict(plan, components=plan['components'] + ['snowluma']))
        with self.assertRaisesRegex(ValueError, 'Unsupported components'):
            validate_snapshot(snapshot)
        c.atomic(path, dict(plan, schema=1))
        with self.assertRaisesRegex(ValueError, 'Unsupported installation plan'):
            validate_snapshot(snapshot)


class ProtocolTest(unittest.IsolatedAsyncioTestCase):
    def context(self, result=None, identity=None, *, connected=True):
        bot = SimpleNamespace(
            _wsr_api_clients={'100000001': object()} if connected else {},
            _wsr_event_clients={object()},
            call_action=AsyncMock(side_effect=[
                {'online': True, 'good': True} if result is None else result,
                {'user_id': 100000001} if identity is None else identity,
            ]),
        )
        platform = SimpleNamespace(bot=bot, config={'id': 'feiyu-onebot'})
        return SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform])), bot

    async def test_ready_evidence_never_claims_chat_or_model(self):
        context, bot = self.context()
        result = communication_health(await probe(context, '100000001'))
        self.assertTrue(result['onebot']['connected'])
        self.assertTrue(result['astrbot_connected'])
        self.assertTrue(result['onebot_service_ready'])
        self.assertTrue(result['qq_login'])
        self.assertEqual(result['business_chat'], 'not_checked')
        self.assertEqual([call.args[0] for call in bot.call_action.await_args_list],
                         ['get_status', 'get_login_info'])

    async def test_disconnected_channels_skip_protocol_actions(self):
        context, bot = self.context(connected=False)
        result = await probe(context, '100000001')
        self.assertFalse(result['astrbot_connected'])
        self.assertIsNone(result['qq_online'])
        bot.call_action.assert_not_awaited()

    async def test_online_and_identity_do_not_substitute_for_each_other(self):
        context, _ = self.context(identity={'user_id': 100000002})
        result = await probe(context, '100000001')
        self.assertFalse(result['connected'])
        self.assertTrue(result['qq_online'])
        self.assertFalse(result['identity_matches'])

    async def test_protocol_failure_keeps_transport_observation(self):
        context, _ = self.context(result=TimeoutError())
        result = await probe(context, '100000001')
        self.assertTrue(result['astrbot_connected'])
        self.assertIsNone(result['onebot_ready'])
        self.assertFalse(result['connected'])


class EvidenceTest(unittest.TestCase):
    def test_evidence_requires_fresh_owned_process_and_strict_flags(self):
        try:
            from fat_fish_management.connection_health import read_connection
        except ModuleNotFoundError as exc:
            if exc.name == 'psutil':
                self.skipTest('Run with FFA runtime dependencies to check live-writer evidence')
            raise
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'private/runtime-health.json'
            c.atomic(root / 'instance.json', {'deployment_id': 'synthetic'})
            record = dict(schema=2, pid=17, deployment_id='synthetic', time=time.time(),
                          connected=True, reason='ready', astrbot_connected=True,
                          onebot_ready=True, qq_online=True, identity_matches=True, core_loaded=True)
            with patch('fat_fish_management.connection_health.process', return_value=SimpleNamespace(pid=17)):
                c.atomic(path, record)
                self.assertTrue(read_connection(root)['connected'])
                for overrides in ({'time': time.time() - 30}, {'deployment_id': 'other'},
                                  {'pid': 18}, {'qq_online': 'true'}):
                    with self.subTest(overrides=overrides):
                        c.atomic(path, dict(record, **overrides))
                        self.assertFalse(read_connection(root)['connected'])


class SnowLumaReleaseTest(unittest.TestCase):
    def setUp(self):
        from hashlib import sha256
        from zipfile import ZipFile
        from fat_fish_management import snowluma

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.local = Path(temporary.name)
        self.root = c.owned_root(self.local / 'ffa', create=True)
        self.destination = self.local / 'official'
        self.archive = self.local / 'official.zip'
        self.program = {'release.json': json.dumps({'target': 'win32-x64', 'version': '2.4.2'}),
                        'native/fixture.node': 'synthetic only',
                        **{name: 'synthetic only' for name in snowluma.PROGRAM_FILES
                           if name != 'release.json'}}
        with ZipFile(self.archive, 'w') as package:
            for path, value in self.program.items():
                package.writestr(path, value)
        self.lock = dict(version='2.4.2', launcher='launcher.bat', filename='official.zip',
                         url='https://github.com/SnowLuma/SnowLuma/releases/download/v2.4.2/official.zip',
                         size=self.archive.stat().st_size, sha256=sha256(self.archive.read_bytes()).hexdigest())
        original_load = c.load

        def load(name):
            return self.lock if name == snowluma.LOCK else original_load(name)

        for module in (c, snowluma, windows):
            load_patch = patch.object(module, 'load', side_effect=load)
            load_patch.start()
            self.addCleanup(load_patch.stop)

    def invoke(self, *args):
        return c.dispatch(c.parser().parse_args(['--root', str(self.root), 'snowluma', *args]))

    def install(self):
        return self.invoke('acquire', '--destination', str(self.destination),
                           '--archive', str(self.archive), '--offline')

    @unittest.skipUnless(os.name == 'nt', 'Windows release layout')
    def test_acquisition_is_independent_and_idempotent(self):
        self.assertTrue(self.install()['registered'])
        self.assertTrue(self.install()['registered'])
        self.assertEqual(c.state(self.root)['components']['snowluma']['ownership'], 'official-external')
        self.assertFalse((self.destination / 'config').exists())
        for name, content in self.program.items():
            self.assertEqual((self.destination / name).read_bytes(), content.encode())
        with self.assertRaisesRegex(c.Fault, '不能静默替换'):
            self.invoke('acquire', '--destination', str(self.local / 'other'))

    @unittest.skipUnless(os.name == 'nt', 'Windows release layout')
    def test_bad_zip_and_unknown_directory_are_preserved(self):
        self.archive.write_bytes(b'bad archive')
        with self.assertRaisesRegex(c.Fault, 'SHA256'):
            self.install()
        self.assertFalse(self.destination.exists())
        self.destination.mkdir()
        sentinel = self.destination / 'unrelated'
        sentinel.write_text('preserve')
        with self.assertRaises(c.Fault) as exc:
            self.install()
        self.assertEqual(exc.exception.kind, 'read_only')
        self.assertEqual(sentinel.read_text(), 'preserve')

    @unittest.skipUnless(os.name == 'nt', 'Windows release layout')
    def test_launch_refuses_unverified_or_modified_program(self):
        self.install()
        with patch.object(windows, 'launch_snowluma') as launch:
            (self.destination / 'native/fixture.node').write_text('modified')
            with self.assertRaisesRegex(c.Fault, '已校验'):
                self.invoke('launch')
            launch.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows release layout')
    def test_launch_uses_only_verified_launcher_and_blocks_duplicate(self):
        self.install()
        from fat_fish_management import snowluma
        with patch.object(snowluma, 'webui_state', return_value='not_listening'), \
                patch.object(windows, 'running_process_creation', return_value=919), \
                patch.object(windows, 'launch_snowluma', return_value=SimpleNamespace(pid=912)) as launch:
            response = self.invoke('launch', '--webui-port', '5999')
            self.assertEqual(response['webui_url'], 'http://127.0.0.1:5999/')
            launch.assert_called_once()
            actual_path, launcher, port = launch.call_args.args
            self.assertTrue(actual_path.samefile(self.destination))
            self.assertEqual((launcher, port), ('launcher.bat', 5999))
            self.assertTrue(self.invoke('status')['tracked_launcher_running'])
            self.assertEqual(self.invoke('status')['webui_url'], response['webui_url'])
            with self.assertRaises(c.Fault) as exc:
                self.invoke('launch')
            self.assertEqual(exc.exception.kind, 'busy')
            launch.assert_called_once()

    def test_webui_opens_only_after_public_loopback_verification(self):
        from fat_fish_management import snowluma
        with patch.object(snowluma, 'webui_state', return_value='unexpected_http_service'), \
                patch.object(windows, 'open_snowluma_webui') as browser:
            with self.assertRaises(c.Fault):
                self.invoke('webui')
            browser.assert_not_called()
        with patch.object(snowluma, 'webui_state', return_value='reachable'), \
                patch.object(windows, 'open_snowluma_webui') as browser:
            self.assertTrue(self.invoke('webui')['opened'])
            browser.assert_called_once_with('http://127.0.0.1:5099/')

    def test_guide_and_clipboard_keep_credential_out_of_response(self):
        token = 'synthetic-clipboard-secret'
        target = self.root / 'instance'
        c.atomic(self.root / 'installation.json', {'schema_version': 1, 'instance': 'instance'})
        c.atomic(target / 'private/bot-identity.json', {'bot_id': '100000001'})
        (target / 'private/onebot-token.txt').write_text(token)
        c.atomic(target / 'data/cmd_config.json', {'platform': [
            {'id': 'feiyu-onebot', 'type': 'aiocqhttp', 'enable': True, 'ws_reverse_token': token,
             'ws_reverse_host': '127.0.0.1', 'ws_reverse_port': 6199},
        ]})
        guide = self.invoke('guide')
        self.assertEqual(guide['reverse_websocket_url'], 'ws://127.0.0.1:6199/ws')
        self.assertNotIn(token, json.dumps(guide))
        with patch.object(windows, 'copy_to_clipboard') as clip:
            self.assertNotIn(token, json.dumps(self.invoke('copy', 'token')))
            clip.assert_called_once_with(token)
        self.assertEqual(self.invoke('stop-guide')['state'], 'waiting_for_user')

    def test_official_download_ignores_alternate_mirrors_and_reuses_verified_cache(self):
        import io
        from fat_fish_management.dependencies import acquire

        class Response(io.BytesIO):
            url = self.lock['url']
            headers = {}

        cache = self.local / 'cache'
        opener = SimpleNamespace(open=unittest.mock.Mock(return_value=Response(self.archive.read_bytes())))
        lock = dict(self.lock, mirrors=['https://unapproved.invalid/wrong.zip'])
        with patch('urllib.request.build_opener', return_value=opener), \
                patch('urllib.request.getproxies', return_value={}), \
                patch.dict(os.environ, {'FFA_GITHUB_TRANSFER_BASE': 'https://unapproved.invalid'}):
            self.assertEqual(acquire(lock, cache, official_release=True).read_bytes(), self.archive.read_bytes())
            opener.open.assert_called_once_with(lock['url'], timeout=15)
            self.assertTrue(acquire(lock, cache, True, official_release=True).samefile(cache / lock['filename']))
            opener.open.assert_called_once()

    def test_official_download_falls_back_to_direct_and_rejects_hash_error(self):
        import io
        import urllib.error
        from fat_fish_management.dependencies import acquire

        class Response(io.BytesIO):
            url = self.lock['url']
            headers = {}

        first = SimpleNamespace(open=unittest.mock.Mock(side_effect=TimeoutError()))
        direct = SimpleNamespace(open=unittest.mock.Mock(return_value=Response(b'x' * self.lock['size'])))
        cache = self.local / 'cache'
        with patch('urllib.request.build_opener', side_effect=[first, direct]), \
                patch('urllib.request.getproxies', return_value={'https': 'http://127.0.0.1:9'}):
            with self.assertRaisesRegex(RuntimeError, 'TimeoutError.*ValueError'):
                acquire(self.lock, cache, official_release=True)
        self.assertEqual(list(cache.iterdir()), [])
        first.open.assert_called_once_with(self.lock['url'], timeout=15)
        direct.open.assert_called_once_with(self.lock['url'], timeout=15)

    def test_official_redirect_rejects_external_host_or_plain_http(self):
        import urllib.request
        from fat_fish_management.dependencies import OfficialReleaseRedirect

        handler = OfficialReleaseRedirect()
        request = urllib.request.Request(self.lock['url'])
        for url in ('https://unapproved.invalid/file.zip',
                    'http://release-assets.githubusercontent.com/file.zip'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'Untrusted'):
                handler.redirect_request(request, None, 302, '', {}, url)


    def test_verify_waits_for_positive_protocol_and_reports_timeout(self):
        from fat_fish_management import snowluma
        ready = {'connected': True, 'reason': 'ready'}
        with patch.object(snowluma.time, 'sleep') as sleep:
            probe = unittest.mock.Mock(side_effect=[{}, ready])
            self.assertEqual(snowluma.wait_for_connection(probe, 2), ready)
            sleep.assert_called_once()
        evidence = {'connected': False, 'reason': 'reverse_websocket_not_connected'}
        with patch.object(snowluma.time, 'monotonic', side_effect=[0, 2]):
            self.assertEqual(snowluma.wait_for_connection(lambda: evidence, 1), evidence)

    def test_public_homepage_probe_uses_loopback_and_does_not_follow_redirect(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        from fat_fish_management import snowluma

        class Handler(BaseHTTPRequestHandler):
            redirect = False

            def do_GET(self):
                if self.redirect:
                    self.send_response(302)
                    self.send_header('Location', 'https://unapproved.invalid/')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'<title>SnowLuma</title>')

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                self.assertEqual(snowluma.webui_state(server.server_port), 'reachable')
                Handler.redirect = True
                self.assertEqual(snowluma.webui_state(server.server_port), 'unexpected_http_service')
            finally:
                server.shutdown()
                worker.join()

    def test_port_probe_distinguishes_filtered_free_port_from_occupied(self):
        import socket
        from fat_fish_management import snowluma

        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen()
            with patch('http.client.HTTPConnection.request', side_effect=TimeoutError()):
                self.assertEqual(snowluma.webui_state(port), 'unverified')
        with patch('http.client.HTTPConnection.request', side_effect=TimeoutError()):
            self.assertEqual(snowluma.webui_state(port), 'not_listening')

    @unittest.skipUnless(os.name == 'nt', 'Windows adapter')
    def test_windows_launch_is_foreground_official_and_does_not_inherit_license_flags(self):
        with patch.dict(os.environ, {'SNOWLUMA_ACCEPT_EULA': '1', 'SNOWLUMA_DEV_MODE': '1',
                                    'NODE_OPTIONS': '--require example'}), \
                patch('subprocess.Popen') as launch:
            windows.launch_snowluma(self.destination, 'launcher.bat', 5999)
            args, kwargs = launch.call_args
            self.assertEqual(args[0][1:], ['/d', '/c', 'launcher.bat'])
            self.assertEqual(kwargs['cwd'], self.destination)
            self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
            self.assertEqual(kwargs['env']['SNOWLUMA_WEBUI_HOST'], '127.0.0.1')
            self.assertEqual(kwargs['env']['SNOWLUMA_HOOK_AUTOLOAD'], '0')
            for name in ('SNOWLUMA_ACCEPT_EULA', 'SNOWLUMA_DEV_MODE', 'NODE_OPTIONS'):
                self.assertNotIn(name, kwargs['env'])
        self.assertIsInstance(windows.running_process_creation(os.getpid()), int)


if __name__ == '__main__':
    unittest.main()

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


if __name__ == '__main__':
    unittest.main()

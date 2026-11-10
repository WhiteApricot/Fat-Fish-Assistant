"""Read-only OneBot health over the existing authenticated AstrBot connection."""
import asyncio


async def probe(context, bot_id):
    status = {
        'connected': False, 'astrbot_connected': None, 'onebot_ready': None,
        'qq_online': None, 'identity_matches': None,
    }
    if not bot_id:
        return {**status, 'reason': 'bot_identity_missing'}
    for platform in context.platform_manager.get_insts():
        if getattr(platform, 'config', {}).get('id') != 'feiyu-onebot':
            continue
        bot = getattr(platform, 'bot', None)
        clients = getattr(bot, '_wsr_api_clients', {})
        events = getattr(bot, '_wsr_event_clients', set())
        status['astrbot_connected'] = str(bot_id) in clients and bool(events)
        if not status['astrbot_connected']:
            return {**status, 'reason': 'reverse_websocket_not_connected'}
        try:
            response = await asyncio.wait_for(bot.call_action('get_status', self_id=str(bot_id)), 5)
        except Exception:
            return {**status, 'reason': 'readonly_status_probe_failed'}
        if not isinstance(response, dict):
            return {**status, 'reason': 'invalid_status_response'}
        if type(response.get('good')) is bool:
            status['onebot_ready'] = response['good']
        if type(response.get('online')) is bool:
            status['qq_online'] = response['online']
        try:
            identity = await asyncio.wait_for(bot.call_action('get_login_info', self_id=str(bot_id)), 5)
        except Exception:
            return {**status, 'reason': 'readonly_identity_probe_failed'}
        status['identity_matches'] = isinstance(identity, dict) and str(identity.get('user_id')) == str(bot_id)
        status['connected'] = all(status[key] is True for key in ('onebot_ready', 'qq_online', 'identity_matches'))
        return {**status, 'reason': 'ready' if status['connected'] else 'offline_or_identity_mismatch'}
    return {**status, 'astrbot_connected': False, 'reason': 'managed_onebot_platform_missing'}

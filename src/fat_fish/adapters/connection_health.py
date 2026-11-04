"""Read-only OneBot protocol health over the existing authenticated connection."""
import asyncio

async def probe(context,bot_id):
    if not bot_id:return {'connected':False,'reason':'bot_identity_missing'}
    for platform in context.platform_manager.get_insts():
        if getattr(platform,'config',{}).get('id')!='feiyu-onebot':continue
        bot=getattr(platform,'bot',None)
        clients=getattr(bot,'_wsr_api_clients',{})
        events=getattr(bot,'_wsr_event_clients',set())
        if str(bot_id) not in clients or not events:return {'connected':False,'reason':'reverse_websocket_not_connected'}
        try:
            status=await asyncio.wait_for(bot.call_action('get_status',self_id=str(bot_id)),5)
            identity=await asyncio.wait_for(bot.call_action('get_login_info',self_id=str(bot_id)),5)
            good=isinstance(status,dict) and status.get('online') is True and status.get('good') is True
            matches=isinstance(identity,dict) and str(identity.get('user_id'))==str(bot_id)
            return {'connected':good and matches,'reason':'ready' if good and matches else 'offline_or_identity_mismatch'}
        except Exception:return {'connected':False,'reason':'readonly_protocol_probe_failed'}
    return {'connected':False,'reason':'managed_onebot_platform_missing'}

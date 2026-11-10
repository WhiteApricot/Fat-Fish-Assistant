"""Configuration policy for the managed AstrBot QQ listener."""

QQ_ADAPTERS = {'aiocqhttp', 'qq_official', 'qq_official_webhook'}


def listener(config):
    active = [row for row in config.get('platform', [])
              if row.get('enable') and row.get('type') in QQ_ADAPTERS]
    if not active:
        return None
    if len(active) != 1 or active[0].get('type') != 'aiocqhttp' or active[0].get('id') != 'feiyu-onebot':
        raise ValueError('Use only the managed SnowLuma OneBot listener for QQ')
    platform = active[0]
    port = platform.get('ws_reverse_port')
    token = platform.get('ws_reverse_token')
    if (platform.get('ws_reverse_host') != '127.0.0.1' or type(port) is not int
            or not 1024 <= port <= 65535 or not isinstance(token, str) or not token.strip()):
        raise ValueError('The managed OneBot listener needs a loopback port and token')
    return platform

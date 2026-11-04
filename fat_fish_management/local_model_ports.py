"""Rebind an owned, stopped local model when its old endpoint is occupied."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import errno,json,secrets,socket,urllib.parse
from fat_fish_management.lifecycle_lock import exclusive


def repair_port(runtime,component):
    with exclusive(runtime):
        path=runtime/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
        ident,field=('feiyu-local-embedding','embedding_api_base') if component=='embedding' else ('feiyu-local-stt','api_base')
        provider=next((x for x in cfg.get('provider',[]) if x.get('id')==ident),None)
        if not provider:return None
        endpoint=urllib.parse.urlsplit(provider.get(field,''))
        if endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost'} or endpoint.username or endpoint.password:return None
        old=endpoint.port or 80
        with socket.socket() as probe:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:probe.bind(('127.0.0.1',old));return None
            except OSError as exc:
                if exc.errno!=errno.EADDRINUSE and getattr(exc,'winerror',None) not in {10013,10048}:raise
        with socket.socket() as probe:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            probe.bind(('127.0.0.1',0));new=probe.getsockname()[1]
        provider[field]=urllib.parse.urlunsplit(endpoint._replace(netloc=f'{endpoint.hostname}:{new}'))
        history=runtime/'private/config-history'/('port-conflict-'+secrets.token_hex(8));history.mkdir(parents=True)
        (history/'cmd_config.json').write_bytes(before)
        pending=path.with_name(path.name+'.port-pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
        return {'from':old,'to':new}

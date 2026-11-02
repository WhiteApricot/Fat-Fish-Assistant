"""Managed AstrBot lifecycle with a local stop-file control and native cleanup."""
import argparse
import asyncio
import json
import os
import sys
sys.dont_write_bytecode=True
from pathlib import Path
from lifecycle_lock import exclusive

p=argparse.ArgumentParser()
p.add_argument('--instance',type=Path,required=True)
a=p.parse_args()
instance=a.instance.resolve()
release=Path(json.loads((instance/'instance.json').read_text())['release'])
os.environ.update(ASTRBOT_ROOT=str(instance),FEIYU_RUNTIME_ROOT=str(instance),FEIYU_SOURCE_ROOT=str(release/'src'),FEIYU_PERSONA_ROOT=str(instance/'persona'))
os.environ.update(FFA_RUNTIME_ROOT=str(instance),FFA_SOURCE_ROOT=str(release/'src'),FFA_PERSONA_ROOT=str(instance/'persona'))
os.environ['PATH']=str(instance/'bin')+os.pathsep+os.environ.get('PATH','')
os.chdir(instance)
sys.path[:0]=[str(release/'astrbot'),str(release/'src')]
key=instance/'private/deepseek-key.txt'
if key.exists():os.environ['DEEPSEEK_API_KEY']=key.read_text().strip()
from ffa_local_credentials import install_capture,load_password
install_capture(instance)
if (instance/'private/dashboard-password.txt').exists():load_password(instance)

async def run():
    running=instance/'private/RUNNING'
    stop=instance/'private/STOP'
    if stop.exists():stop.unlink()
    (instance/'private/STOPPED').unlink(missing_ok=True)
    with running.open('x') as f:f.write(str(os.getpid()))
    clean=False
    try:
        main.check_env()
        main.log_broker=main.LogBroker()
        main.LogManager.set_queue_handler(main.logger,main.log_broker)
        # Managed releases include the pinned UI: never fall back to an implicit
        # network download (or its upstream insecure-TLS retry) at startup.
        dashboard=release/'astrbot/astrbot/dashboard/dist'
        resolved=main.resolve_dashboard_dist(str(dashboard)) if dashboard.is_dir() else None
        if resolved is None or Path(resolved).resolve()!=dashboard.resolve():
            raise ValueError('Verified release dashboard is unavailable or incompatible; reinstall a pinned release')
        task=asyncio.create_task(main.main_async(str(dashboard)))
        while not stop.exists() and not task.done():await asyncio.sleep(.25)
        if not task.done():task.cancel()
        await task
        clean=True
    finally:
        if clean:
            running.unlink(missing_ok=True)
            (instance/'private/STOPPED').write_text('native lifecycle completed')
        else:(instance/'private/DIRTY').write_text('Native shutdown incomplete; recover before migration')

with exclusive(instance):
    # All subsequent application imports live in the independent local release
    # and environment. Reuse their caches without writing into formal scripts/.
    sys.pycache_prefix=None
    os.environ.pop('PYTHONPYCACHEPREFIX',None)
    sys.dont_write_bytecode=False
    os.environ.pop('PYTHONDONTWRITEBYTECODE',None)
    import main
    asyncio.run(run())

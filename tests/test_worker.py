"""Diagnostic subprocess harness; preserves the test's real modules and assertions."""
import faulthandler
import runpy
import sys
from pathlib import Path

if __name__=='__main__':
    target=Path(sys.argv[1]).resolve()
    sys.argv=sys.argv[1:]
    sys.path.insert(0,str(target.parent))
    faulthandler.enable()
    faulthandler.dump_traceback_later(60,repeat=True)
    try:runpy.run_path(str(target),run_name='__main__')
    finally:faulthandler.cancel_dump_traceback_later()

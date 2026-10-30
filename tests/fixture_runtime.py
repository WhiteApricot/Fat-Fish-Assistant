"""Keep AstrBot module-import side effects inside disposable synthetic storage."""
import os
import tempfile
from pathlib import Path

_bootstrap=tempfile.TemporaryDirectory(prefix='feiyu-test-imports-',ignore_cleanup_errors=True)
os.environ['ASTRBOT_ROOT']=_bootstrap.name
os.environ['FEIYU_RUNTIME_ROOT']=_bootstrap.name

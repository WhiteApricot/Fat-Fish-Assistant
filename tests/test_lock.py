"""A second process cannot obtain the live instance's maintenance lock."""
import subprocess
import sys
import tempfile
from pathlib import Path

scripts=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(scripts))
from lifecycle_lock import exclusive

child="""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from lifecycle_lock import exclusive
try:
 with exclusive(Path(sys.argv[2])):pass
except RuntimeError:
 raise SystemExit(23)
"""
with tempfile.TemporaryDirectory(prefix='feiyu-lock-') as tmp:
    root=Path(tmp);(root/'private').mkdir()
    with exclusive(root):
        result=subprocess.run([sys.executable,'-c',child,str(scripts),str(root)],capture_output=True,timeout=15)
        assert result.returncode==23,result.stderr
    result=subprocess.run([sys.executable,'-c',child,str(scripts),str(root)],capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
print('LOCK_RESULT=concurrent acquisition refused; released lock reusable')

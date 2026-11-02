"""Native probe detects only Python writers for a synthetic target, without WMI."""
import json,os,subprocess,sys,tempfile
from pathlib import Path
source=Path(__file__).resolve().parents[1]
shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
with tempfile.TemporaryDirectory(prefix='writer-probe-') as folder:
 target=Path(folder)/'partial-environment';target.mkdir()
 worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)',str(target)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
 def probe():
  env=dict(os.environ,PSMODULEPATH=str(shell.parent/'Modules'))
  command=[str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(source/'scripts/Test-Build-Writers.ps1'),'-Destination',str(target),'-ControllerPid',str(os.getpid())]
  proc=subprocess.run(command,capture_output=True,text=True,env=env,timeout=20)
  if proc.returncode:raise RuntimeError(proc.stderr)
  return json.loads(proc.stdout.removeprefix('\ufeff'))['active_count']
 try:assert probe()>=1
 finally:worker.terminate();worker.wait(timeout=10)
 assert probe()==0
print('WMI_FREE_BUILD_WRITERS_PASS')

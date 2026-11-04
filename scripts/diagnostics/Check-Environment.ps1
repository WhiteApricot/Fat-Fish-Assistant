param(
 [Parameter(Mandatory=$true)][string]$ChatPython,
 [string]$SpeechPython
)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$oldPath=$env:PATH
$oldUserSite=$env:PYTHONNOUSERSITE
try{
 $env:PATH=(Join-Path $env:SystemRoot 'System32')+';'+$env:SystemRoot+';'+(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')+';'+(Split-Path -Parent $ChatPython)
 $env:PYTHONNOUSERSITE='1'
 & $ChatPython -X utf8 -I -c "import sys,numpy,faiss,psutil; assert sys.version_info[:3]==(3,12,3); print('Isolated CPython/native Chat imports passed')"
 if($LASTEXITCODE -ne 0){throw 'Native Chat prerequisites unavailable; inspect Python architecture and Microsoft C++ runtime'}
 if($SpeechPython){
  & $SpeechPython -X utf8 -I -c "import sys,numpy,sherpa_onnx; assert sys.version_info[:3]==(3,12,3); assert numpy.__version__=='2.2.6'; print('Isolated CPython/native Speech imports passed')"
  if($LASTEXITCODE -ne 0){throw 'Native Speech prerequisites unavailable; inspect the independent environment and Microsoft C++ runtime'}
 }
}finally{$env:PATH=$oldPath;$env:PYTHONNOUSERSITE=$oldUserSite}

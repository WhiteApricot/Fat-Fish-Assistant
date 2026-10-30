param(
 [ValidateSet('check','install','start','stop','restart','status','components','health','diagnose','wizard','config','backup','restore','upgrade','rollback','component','recover-upgrade','register-external')][string]$Action='install',
 [string]$Root=(Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant'),
 [string]$Python,
 [switch]$Json,
 [switch]$NonInteractive,
 [Parameter(ValueFromRemainingArguments=$true)][string[]]$Options
)
$ErrorActionPreference='Stop'
$projectRoot=$PSScriptRoot
try{
 if(-not $Python){
  $command=Get-Command python -CommandType Application -ErrorAction SilentlyContinue|Select-Object -First 1
  if($command -and $command.Source -notlike '*WindowsApps*'){
   $savedPreference=$ErrorActionPreference
   try{$ErrorActionPreference='Continue'; & $command.Source -I -c 'import sys,struct; assert sys.version_info[:3]==(3,12,3) and struct.calcsize("P")==8' 2>$null; $probeExit=$LASTEXITCODE}
   finally{$ErrorActionPreference=$savedPreference}
   if($probeExit -eq 0){$Python=$command.Source}
  }
 }
 if(-not $Python){
  $runtime=Join-Path $Root 'bootstrap-python-3.12.3'
  $candidate=Join-Path $runtime 'tools\python.exe'
  if(Test-Path -LiteralPath $candidate){$Python=$candidate}
  elseif($Action -eq 'install'){
   $absolute=[IO.Path]::GetFullPath($Root)
   $repository=[IO.Path]::GetFullPath($projectRoot)+[IO.Path]::DirectorySeparatorChar
   if(($absolute+[IO.Path]::DirectorySeparatorChar).StartsWith($repository,[StringComparison]::OrdinalIgnoreCase)){throw 'Installation root must be outside the repository'}
   $marker=Join-Path $Root '.ffa-managed.json'
   if(-not(Test-Path -LiteralPath $marker)){
    if((Test-Path -LiteralPath $Root) -and @(Get-ChildItem -Force -LiteralPath $Root).Count -gt 0){throw 'Refusing to adopt a nonempty unmanaged root'}
    New-Item -ItemType Directory -Path $Root -Force|Out-Null
    @{schema_version=1;project='Fat-Fish-Assistant';deployment_id=[guid]::NewGuid().ToString('N')}|ConvertTo-Json|Set-Content -LiteralPath $marker -Encoding UTF8
   }
   $identity=Get-Content -Raw -LiteralPath $marker|ConvertFrom-Json
   if($identity.project -ne 'Fat-Fish-Assistant' -or $identity.schema_version -ne 1){throw 'Unknown management root marker; refusing bootstrap'}
   [Console]::Error.WriteLine('[Fat-Fish-Assistant] Preparing pinned independent Python; no registry or system Python changes.')
   & (Join-Path $projectRoot 'scripts\Bootstrap-Python.ps1') -Destination $runtime -Cache (Join-Path $Root 'cache') 1>$null
   $Python=$candidate
  }else{
   @{schema_version=1;project='Fat-Fish-Assistant';request_id=[guid]::NewGuid().ToString('N');ok=$false;code=21;data=@{windows=[Environment]::OSVersion.VersionString;python_available=$false};errors=@(@{type='needs_input';message='Python 3.12.3 x64 is missing';hint='Run install to bootstrap an independent interpreter, or specify -Python.'})}|ConvertTo-Json -Depth 6 -Compress
   exit 21
  }
 }
 $forward=@('--root',$Root,$Action)
 if($Action -eq 'install' -and -not $NonInteractive -and $Options -notcontains '--owner' -and $Options -notcontains '--no-instance'){
  [Console]::Error.WriteLine('肥鱼助手 / Fat-Fish-Assistant: use your own QQ owner identity. No existing identity, group or database will be imported.')
  $ownerValue=Read-Host 'Your QQ owner identity (digits only)'
  $forward+=@('--owner',$ownerValue)
 }
 # Pump progress explicitly: Windows PowerShell 5 must not turn native stderr
 # progress lines into terminating NativeCommandError records.
 function Quote-Argument([string]$Value){
  $escaped=[regex]::Replace($Value,'(\\*)"','$1$1\"')
  $escaped=[regex]::Replace($escaped,'(\\+)$','$1$1')
  return '"'+$escaped+'"'
 }
 $start=New-Object Diagnostics.ProcessStartInfo
 $start.FileName=$Python
 $start.Arguments=(@('-I',(Join-Path $projectRoot 'ffa.py'))+$forward+@($Options)|Where-Object {$null -ne $_}|ForEach-Object {Quote-Argument $_}) -join ' '
 $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.WindowStyle='Hidden'
 $start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
 $process=New-Object Diagnostics.Process;$process.StartInfo=$start
 [void]$process.Start()
 $outputTask=$process.StandardOutput.ReadToEndAsync()
 $errorTask=$process.StandardError.ReadLineAsync()
 while(-not $process.HasExited -or $null -ne $errorTask){
  if($null -ne $errorTask -and $errorTask.IsCompleted){
   $line=$errorTask.GetAwaiter().GetResult()
   if($null -eq $line){$errorTask=$null}
   else{[Console]::Error.WriteLine($line);$errorTask=$process.StandardError.ReadLineAsync()}
  }else{Start-Sleep -Milliseconds 100}
 }
 $process.WaitForExit();$resultText=$outputTask.GetAwaiter().GetResult();$resultCode=$process.ExitCode
 if($Json){$resultText.Trim()}
 else{
  $result=$resultText|ConvertFrom-Json
  if($result.ok){
   '肥鱼助手 / Fat-Fish-Assistant: '+$Action+' completed.'
   if($Action -eq 'install'){foreach($step in $result.data.next_steps){$step}}
   elseif($Action -eq 'wizard'){foreach($step in $result.data.manual_steps){$step}}
   else{$result.data|ConvertTo-Json -Depth 12}
  }else{foreach($errorItem in $result.errors){[Console]::Error.WriteLine($errorItem.message);[Console]::Error.WriteLine($errorItem.hint)}}
 }
 exit $resultCode
}catch{
 @{schema_version=1;project='Fat-Fish-Assistant';request_id=[guid]::NewGuid().ToString('N');ok=$false;code=30;data=$null;errors=@(@{type='failed';message=$_.Exception.Message;hint='Inspect the error and repeat; existing external components are not modified.'})}|ConvertTo-Json -Depth 6 -Compress
 exit 30
}

# Shared low-level stop primitive. Callers first verify executable/instance ownership.
function Read-ManagedJson([string]$Path){
 $reader=New-Object IO.StreamReader($Path,(New-Object Text.UTF8Encoding($false,$true)),$true)
 try{try{$text=$reader.ReadToEnd()}catch [Text.DecoderFallbackException]{$text=[IO.File]::ReadAllText($Path,[Text.Encoding]::Default)}}finally{$reader.Dispose()}
 return $text|ConvertFrom-Json
}
function Stop-VerifiedProcess($Info){
 $owned=Get-Process -Id $Info.ProcessId -ErrorAction SilentlyContinue
 if($null -eq $owned){return}
 try{
  # Hold the process handle before validating its creation time. The handle binds
  # termination and waiting to this process even if the numeric PID is recycled.
  [void]$owned.Handle
  if($owned.HasExited){return}
  # A venv redirector may exit as soon as its child is stopped. Its handle still
  # proves that this is the same exited process; no termination remains to do.
  try{$started=$owned.StartTime}catch{if($owned.HasExited){return};throw}
  if($null -eq $started){if($owned.HasExited){return};throw 'Cannot verify process creation time; refusing termination'}
  if($null -eq $Info.CreationDate){throw 'Missing recorded creation time; refusing termination'}
  # CIM timestamps have microsecond precision; Process.StartTime has 100 ns precision.
  if([Math]::Abs([decimal]$started.ToUniversalTime().Ticks-[decimal]$Info.CreationDate.ToUniversalTime().Ticks) -gt 10){throw 'Process creation identity changed; refusing termination'}
  if(-not $owned.HasExited){try{$owned.Kill()}catch{if(-not $owned.HasExited){throw}}}
  if(-not $owned.WaitForExit(15000)){throw 'Owned process did not exit; cold operations are blocked'}
 }finally{$owned.Dispose()}
}

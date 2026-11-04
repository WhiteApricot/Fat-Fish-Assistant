param([Parameter(Mandatory=$true)][string]$Installer)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$spec=Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\..\manifests\assets.json')|ConvertFrom-Json|Where-Object {$_.id -eq 'msvc-runtime-x64-14.51.36247'}
if((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $spec.sha256){throw 'Microsoft runtime hash mismatch'}
# Called only for a missing runtime after explicit external-license acceptance.
$process=Start-Process -FilePath $Installer -ArgumentList '/install /passive /norestart' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
if($process.ExitCode -notin @(0,1638,3010)){throw ('Microsoft runtime installation failed: '+$process.ExitCode)}
if($process.ExitCode -eq 3010){throw 'Microsoft runtime requires a Windows restart; restart Windows then repeat the installer safely.'}

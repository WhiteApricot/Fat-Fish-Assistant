param([string]$Output)
$ErrorActionPreference='Stop'
$env:FFA_TRANSFER_PROGRESS='1'
New-Item -ItemType Directory -Force -Path $Output|Out-Null
. (Join-Path $PSScriptRoot '../scripts/Receive-Download.ps1')
$script:payload=New-Object byte[] 131072
function OpenDownloadResponse([string]$Source){
 $response=[pscustomobject]@{ResponseUri=[Uri]$Source;ContentLength=$script:payload.Length}
 $response|Add-Member ScriptMethod GetResponseStream {[IO.MemoryStream]::new($script:payload,$false)}
 $response|Add-Member ScriptMethod Close {}
 return $response
}
$original=[Console]::Error;$capture=New-Object IO.StringWriter
try{[Console]::SetError($capture);ReceiveDownload 'https://mirror.example/python.nupkg' (Join-Path $Output 'python.nupkg') 'python.nupkg'}finally{[Console]::SetError($original)}
$frames=@($capture.ToString() -split "`r?`n"|Where-Object {$_}|ForEach-Object {($_ -replace '^\[Fat-Fish-Assistant\] transfer: ','')|ConvertFrom-Json})
if((Get-Item (Join-Path $Output 'python.nupkg')).Length -ne $script:payload.Length){throw 'Stream bytes missing'}
if($frames[0].bytes -ne 0 -or $frames[-1].bytes -ne 131072 -or -not $frames[-1].done -or $frames[-1].source -ne 'mirror.example'){throw 'Bootstrap transfer status failed'}
$frames|ConvertTo-Json|Set-Content (Join-Path $Output 'frames.json') -Encoding UTF8
'Pinned Python stream progress passed'

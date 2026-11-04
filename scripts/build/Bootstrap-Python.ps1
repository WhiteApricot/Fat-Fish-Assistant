param(
 [Parameter(Mandatory=$true)][string]$Destination,
 [Parameter(Mandatory=$true)][string]$Cache,
 [string]$Manifest
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/build/Receive-Download.ps1')
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
if(-not $Manifest){$Manifest=Join-Path $PSScriptRoot '..\..\manifests\python-runtime.json'}
$spec=Get-Content -Raw -Encoding UTF8 -LiteralPath $Manifest|ConvertFrom-Json
$target=[IO.Path]::GetFullPath($Destination)
if(Test-Path -LiteralPath $target){
 $receipt=Join-Path $target 'ffa-bootstrap.json'
 $existingPython=Join-Path $target 'tools\python.exe'
 if(-not(Test-Path -LiteralPath $receipt) -or (Get-Content -Raw -Encoding UTF8 -LiteralPath $receipt|ConvertFrom-Json).sha256 -ne $spec.sha256){throw 'Existing Python target is unmanaged or incompatible; no overwrite'}
 & $existingPython -I -c "import sys,venv,ensurepip,ssl,sqlite3; assert sys.version_info[:3]==(3,12,3)"
 if($LASTEXITCODE -ne 0){throw 'Existing managed interpreter failed validation'}
 $existingPython
 exit 0
}
$cacheRoot=[IO.Path]::GetFullPath($Cache)
New-Item -ItemType Directory -Path $cacheRoot -Force|Out-Null
$archive=Join-Path $cacheRoot $spec.filename
if(-not(Test-Path -LiteralPath $archive)){
 $previousTls=[Net.ServicePointManager]::SecurityProtocol
 try{
  [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
  $sources=@()
  try{
   $index=Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 -Uri 'https://repo.huaweicloud.com/repository/nuget/v3/index.json'|Select-Object -ExpandProperty Content|ConvertFrom-Json
   $baseUrl=($index.resources|Where-Object {$_.'@type' -eq 'PackageBaseAddress/3.0.0'}|Select-Object -First 1).'@id'
   $baseUri=[Uri]$baseUrl
   if($baseUri.Scheme -eq 'https' -and $baseUri.Host -eq 'repo.huaweicloud.com'){$sources+=($baseUrl.TrimEnd('/')+'/python/'+$spec.version+'/'+$spec.filename)}
  }catch{} # Missing/unavailable mirror metadata falls back to pinned official.
  $sources+=@($spec.url)
  $downloaded=$false
  foreach($source in $sources){
   $partial=$archive+'.part-'+[guid]::NewGuid().ToString('N')
   try{
    ReceiveDownload $source $partial $spec.filename
    if((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $spec.sha256){throw 'Downloaded Python hash mismatch'}
    Move-Item -LiteralPath $partial -Destination $archive
    @{file=$spec.filename;source=$source;sha256=$spec.sha256;time=[DateTime]::UtcNow.ToString('o')}|ConvertTo-Json -Compress|Add-Content -LiteralPath (Join-Path $cacheRoot 'acquisition.jsonl') -Encoding UTF8
    $downloaded=$true;break
   }catch{if(Test-Path -LiteralPath $partial){Remove-Item -LiteralPath $partial};if($source -eq $spec.url){throw}}
  }
  if(-not $downloaded){throw 'Downloaded Python hash mismatch'}
 }finally{[Net.ServicePointManager]::SecurityProtocol=$previousTls}
}
if((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $spec.sha256){throw 'Python package hash mismatch; refusing extraction'}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip=[IO.Compression.ZipFile]::OpenRead($archive)
try{
 foreach($entry in $zip.Entries){
  $path=[IO.Path]::GetFullPath([IO.Path]::Combine($target,$entry.FullName))
  if(-not $path.StartsWith($target+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)){throw 'Python archive path escapes target'}
 }
}finally{$zip.Dispose()}
$stage=$target+'.staging-'+[guid]::NewGuid().ToString('N')
[IO.Compression.ZipFile]::ExtractToDirectory($archive,$stage)
$python=Join-Path $stage 'tools\python.exe'
& $python -I -c "import sys,venv,ensurepip,ssl,sqlite3; assert sys.version_info[:3]==(3,12,3); print('Independent CPython 3.12.3 runtime verified')"
if($LASTEXITCODE -ne 0){throw 'Python runtime probe failed'}
@{schema_version=1;sha256=$spec.sha256;version=$spec.version}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $stage 'ffa-bootstrap.json') -Encoding UTF8
Move-Item -LiteralPath $stage -Destination $target
Join-Path $target 'tools\python.exe'

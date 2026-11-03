$ErrorActionPreference='Stop'
$desktop=Join-Path (Split-Path $PSScriptRoot) 'scripts/Desktop.ps1'
$tokens=$null
$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($desktop,[ref]$tokens,[ref]$parseErrors)
if($parseErrors){throw ($parseErrors -join ', ')}
foreach($name in @('OpenQQLogin','TestQQLoginReady','WatchQQLogin')){
 $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
 . ([scriptblock]::Create($node.Extent.Text))
}
$script:root=Join-Path $env:TEMP ('ffa-napcat-fixture-'+[guid]::NewGuid())
$configPath=Join-Path $script:root 'napcat/config'
New-Item -ItemType Directory -Force $configPath|Out-Null
@{host='127.0.0.1';port=6099;token='fixture+&token'}|ConvertTo-Json|Set-Content (Join-Path $configPath 'webui.json')
$record=[pscustomobject]@{components=@{napcat=@{installed=$true;path='napcat'}}}
$NoBrowser=$false
$script:ready=@{isLogin=$true;coreReady=$true;isOffline=$false}
$script:partial=@{isLogin=$true;coreReady=$false;isOffline=$false}
function CheckCancel {if($script:polls -ge $script:limit){throw [OperationCanceledException]::new('fixture complete')}}
function Start-Sleep {param($Seconds)}
function Write-Host {param($Object,$ForegroundColor) $script:messages+=@([string]$Object)}
function Api($Arguments){
 if($Arguments[-1] -eq 'recover-login'){return @{restarted=$false}}
 $script:polls++
 if($script:polls -in $script:readyPolls){return $script:ready}
 if($script:polls -in $script:failedPolls){throw 'synthetic probe unavailable'}
 return $script:partial
}
function Start-Process($Address){
 $script:attempts++
 if($script:browserFails -and $script:attempts -eq 1){throw 'synthetic browser failure'}
 $script:opened+=@($Address)
}
function Exercise($limit,$readyPolls,$failedPolls,$browserFails){
 $script:polls=0;$script:limit=$limit;$script:readyPolls=$readyPolls;$script:failedPolls=$failedPolls
 $script:browserFails=$browserFails;$script:messages=@();$script:opened=@();$script:attempts=0
 try{WatchQQLogin $record}catch [OperationCanceledException]{}
}
Exercise 18 @(9) @() $false
if($opened.Count -ne 2){throw 'Expected one page per offline episode, with recovery rearming the watcher'}
if($opened[0] -ne 'http://127.0.0.1:6099/webui/web_login?token=fixture%2B%26token'){throw 'Login URL/token encoding mismatch'}
if(($messages -join ' ') -notmatch '重新打开浏览器' -or ($messages -join ' ') -match 'fixture'){throw 'Missing operator notice or token leaked'}
Exercise 17 @() @(1,2,3) $true
if($attempts -ne 2 -or $opened.Count -ne 1){throw 'Expected browser launch retry after failed probes and failed browser open'}
Exercise 9 @(8,9) @() $false
if($opened.Count){throw 'Recovery during countdown must suppress browser launch'}
[Console]::WriteLine('NapCat launcher offline, relogin, retry, recovery and token handling: PASS')

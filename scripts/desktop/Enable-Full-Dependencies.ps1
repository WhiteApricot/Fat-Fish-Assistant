param([string]$Root,[switch]$Offline,[string]$Cache,[switch]$AcceptExternalLicenses)
$ErrorActionPreference='Stop'
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/desktop/Initialize-DesktopInteraction.ps1')
$project=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if(-not $Root){
 $registry=Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant-launcher\installation.txt'
 $Root=if(Test-Path -LiteralPath $registry){(Get-Content -Raw -Encoding UTF8 -LiteralPath $registry).Trim()}else{Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant'}
}
$record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'installation.json')|ConvertFrom-Json
$marker=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root '.ffa-managed.json')|ConvertFrom-Json
if($marker.project -ne 'Fat-Fish-Assistant' -or $marker.schema_version -ne 1){throw 'Unknown management root marker; refusing operation'}
if(-not $AcceptExternalLicenses){
 $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/desktop/Invoke-Desktop.ps1'),[ref]$null,[ref]$null)
 $licenseFunction=$ast.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq 'AcceptLicenses'}
 . ([scriptblock]::Create($licenseFunction.Extent.Text))
 AcceptLicenses
}
$python=if([IO.Path]::IsPathRooted($record.chat_python)){$record.chat_python}else{Join-Path $Root $record.chat_python}
$basePython=if([IO.Path]::IsPathRooted($record.components.python.path)){$record.components.python.path}else{Join-Path $Root $record.components.python.path}
$manager=Join-Path $project 'Fat-Fish-Assistant.ps1'
& $manager stop -Root $Root -Python $python -NonInteractive
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
$components='chat'
foreach($name in @('embedding','speech')){if($record.components.$name.installed){$components+=','+$name}}
$plugins=(@($record.components.plugins.version.PSObject.Properties.Name) -join ',')
$options=@('--dependency-profile','chat','--repair','--components',$components,'--plugins',$plugins,'--accept-external-licenses')
if($Offline){$options+='--offline'}
if($Cache){$options+=@('--cache',$Cache)}
& $manager install -Root $Root -Python $basePython -NonInteractive @options
exit $LASTEXITCODE

param([string]$Output)
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Path $Output -Force|Out-Null
$project=Split-Path $PSScriptRoot -Parent
. (Join-Path $project 'scripts/Desktop-Interaction.ps1')
$script:root=$Output;$script:desktopLog=Join-Path $Output 'Desktop.log';'synthetic'|Set-Content $script:desktopLog
$queue=New-Object 'Collections.Generic.Queue[ConsoleKeyInfo]'
function Key([ConsoleKey]$Code,[char]$Char=[char]0){$queue.Enqueue((New-Object ConsoleKeyInfo($Char,$Code,$false,$false,$false)))}
function ReadConsoleKey {if(-not $queue.Count){throw 'Unexpected extra key read'};return $queue.Dequeue()}
function Folder([string]$Description,[string]$Default){$script:folders++;return (Join-Path $Output ('choice'+$script:folders))}
$script:folders=0;Key A;Key X;Key Enter
$selected=ChooseInstallRoot $Output
if($script:folders -ne 2 -or $selected -ne (Join-Path $Output 'choice2')){throw 'Single-key folder confirmation/reselection failed'}
Key Enter;if(-not (EnterKey 'synthetic accept')){throw 'Enter did not accept'}
Key N;if(EnterKey 'synthetic reject'){throw 'Non-enter did not reject'}
$plain=@(PlainLicense "|依赖|许可|`n|---|---|`n|Demo|[MIT](https://example.test)|" 6>&1) -join "`n"
if($plain -match '\|---' -or $plain -notmatch '许可：MIT \(https://example.test\)'){throw 'Plaintext license table failed'}
[FeiyuConsole]::CancelRequested=$true
$cancelled=$false;try{CheckCancel}catch [OperationCanceledException]{$cancelled=$true}
if(-not $cancelled){throw 'Ctrl+C request did not cancel'}
[FeiyuConsole]::CancelRequested=$false
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $project 'scripts/Desktop.ps1'),[ref]$null,[ref]$null)
# Exercise the actual finalizer against an owned synthetic root, with a fake API.
'{}'|Set-Content (Join-Path $Output 'installation.json')
function Api([string[]]$Arguments){if($Arguments.Count -ne 1 -or $Arguments[0] -ne 'stop'){throw 'Unexpected cancellation operation'};$script:stopped=$true}
[FeiyuConsole]::CancelRequested=$true
$finalizer=$ast.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.TryStatementAst]}|Select-Object -Last 1
. ([scriptblock]::Create($finalizer.Finally.Extent.Text.TrimStart('{').TrimEnd('}')))
if(-not $script:stopped){throw 'Cancellation did not stop managed services'}
[FeiyuConsole]::CancelRequested=$false;$script:stopping=$false
if((LogReference) -notmatch ([regex]::Escape($script:desktopLog))){throw 'Missing detailed log path'}
foreach($file in Get-ChildItem (Join-Path $project 'scripts') -Filter '*.ps1'){$errors=$null;[void][Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$null,[ref]$errors);if($errors){throw ($errors|Out-String)}}
$result=@{single_key_selection=$true;reselection=$true;license_enter_and_reject=$true;plaintext_table=$true;ctrl_c_stop_dispatch=$true;detailed_log_file=$true;syntax=$true;interactive_windows_visual='pending manual';sandboxie='not tested'}
$result|ConvertTo-Json|Set-Content (Join-Path $Output 'results.json') -Encoding UTF8
$result|ConvertTo-Json -Compress

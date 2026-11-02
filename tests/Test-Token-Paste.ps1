$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../scripts/Desktop-Interaction.ps1')
$queue=New-Object 'Collections.Generic.Queue[ConsoleKeyInfo]'
function ReadConsoleKey {$queue.Dequeue()}
function ReadClipboardText {return "synthetic-中文`r`n-token"}
$queue.Enqueue([ConsoleKeyInfo]::new([char]22,[ConsoleKey]::V,$false,$false,$true))
$queue.Enqueue([ConsoleKeyInfo]::new([char]13,[ConsoleKey]::Enter,$false,$false,$false))
$capture=@(& {$script:secure=ReadInput 'synthetic hidden token' -AsSecureString} 6>&1)|Out-String
$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($script:secure)
try{if([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) -cne 'synthetic-中文-token'){throw 'Ctrl+V paste failed'}}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr);$script:secure.Dispose()}
if($capture -match 'synthetic-中文-token'){throw 'Secret echoed'}
if($queue.Count){throw 'Unexpected pending key'}
$queue.Enqueue([ConsoleKeyInfo]::new([char]0,[ConsoleKey]::Insert,$true,$false,$false))
$queue.Enqueue([ConsoleKeyInfo]::new([char]13,[ConsoleKey]::Enter,$false,$false,$false))
$value=ReadInput 'synthetic plain text'
if($value -cne 'synthetic-中文-token'){throw 'Shift+Insert failed'}
'TOKEN_PASTE_PASS'

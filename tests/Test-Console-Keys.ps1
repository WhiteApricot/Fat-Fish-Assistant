$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../scripts/Desktop-Interaction.ps1')
[FeiyuConsole]::Attach()
try{
 if(-not (EnterKey 'TEST_ENTER')){throw 'Enter failed'}
 if(EnterKey 'TEST_OTHER_KEY'){throw 'Other key failed'}
 if((ReadInput 'TEST_LINE') -ne 'abc'){throw 'Line input failed'}
 Write-Host 'TEST_CTRL_C'
 $passed=$false
 try{while($true){CheckCancel;Start-Sleep -Milliseconds 50}}catch [OperationCanceledException]{$passed=$true}
 if(-not $passed){throw 'Ctrl+C failed'}
 Write-Host 'CONSOLE_PASS'
}finally{[FeiyuConsole]::Detach()}

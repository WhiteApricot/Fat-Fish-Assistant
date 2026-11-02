# Console-only interaction primitives; no application state is modified here.
if(-not ('FeiyuConsole' -as [type])){
 Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FeiyuClipboard {
 [DllImport("user32.dll",SetLastError=true)] static extern bool OpenClipboard(IntPtr owner);
 [DllImport("user32.dll")] static extern bool CloseClipboard();
 [DllImport("user32.dll")] static extern IntPtr GetClipboardData(uint format);
 [DllImport("kernel32.dll")] static extern IntPtr GlobalLock(IntPtr handle);
 [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr handle);
 public static string Read(){
  if(!OpenClipboard(IntPtr.Zero))throw new InvalidOperationException("Clipboard is busy; retry Ctrl+V");
  try{var handle=GetClipboardData(13);if(handle==IntPtr.Zero)return "";
   var data=GlobalLock(handle);if(data==IntPtr.Zero)throw new InvalidOperationException("Cannot read clipboard text");
   try{return Marshal.PtrToStringUni(data)??"";}finally{GlobalUnlock(handle);}
  }finally{CloseClipboard();}
 }
}
public static class FeiyuConsole {
 public static volatile bool CancelRequested;
 static bool previous;
 static void Cancel(object s,ConsoleCancelEventArgs e){if(e.SpecialKey==ConsoleSpecialKey.ControlC){e.Cancel=true;CancelRequested=true;}}
 public static void Attach(){CancelRequested=false;previous=Console.TreatControlCAsInput;Console.TreatControlCAsInput=true;Console.CancelKeyPress+=Cancel;}
 public static void Detach(){Console.CancelKeyPress-=Cancel;Console.TreatControlCAsInput=previous;}
}
'@
}
function ReadClipboardText { [FeiyuClipboard]::Read() }
function ReadConsoleKey { [Console]::ReadKey($true) }
function CheckCancel {
 if($script:stopping){return}
 if(-not [Console]::IsInputRedirected -and [Console]::KeyAvailable){
  $key=ReadConsoleKey
  if($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control)){[FeiyuConsole]::CancelRequested=$true}
 }
 if([FeiyuConsole]::CancelRequested){throw [OperationCanceledException]::new('用户按 Ctrl+C 停止服务')}
}
function ReadInput([string]$Prompt,[switch]$AsSecureString){
 Write-Host ($Prompt+': ') -NoNewline
 $text=New-Object Text.StringBuilder;$secret=New-Object Security.SecureString
 while($true){
  $key=ReadConsoleKey
  if($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control)){[FeiyuConsole]::CancelRequested=$true;CheckCancel}
  if($key.Key -eq 'Enter'){Write-Host '';if($AsSecureString){return $secret};return $text.ToString()}
  if($key.Key -eq 'Backspace'){
   $length=if($AsSecureString){$secret.Length}else{$text.Length}
   if($length){if($AsSecureString){$secret.RemoveAt($length-1)}else{[void]$text.Remove($length-1,1)};Write-Host "`b `b" -NoNewline};continue
  }
  $characters=[string]$key.KeyChar
  if(($key.Key -eq 'V' -and ($key.Modifiers -band [ConsoleModifiers]::Control)) -or ($key.Key -eq 'Insert' -and ($key.Modifiers -band [ConsoleModifiers]::Shift))){
   try{$characters=ReadClipboardText}catch{[Console]::Beep();continue}
  }
  # Paste is input only: CR/LF never submits a secret or answers the next prompt.
  foreach($character in $characters.ToCharArray()){
   if([char]::IsControl($character)){continue}
   if($AsSecureString){$secret.AppendChar($character);Write-Host '*' -NoNewline}else{[void]$text.Append($character);Write-Host $character -NoNewline}
  }
 }
}
function EnterKey([string]$Prompt){
 Write-Host $Prompt
 $key=ReadConsoleKey
 if($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control)){[FeiyuConsole]::CancelRequested=$true;CheckCancel}
 return $key.Key -eq 'Enter'
}
function ChooseInstallRoot([string]$Default){
 Write-Host ('默认安装位置：'+$Default)
 if(EnterKey '回车使用此位置；按其他任意键选择安装目录') {return $Default}
 while($true){
  $selected=Folder '选择空目录或已有肥鱼助手安装目录；禁止选择正式源码目录' $Default
  if(EnterKey ('确认安装位置：'+$selected+'；回车执行安装，按其他键重新选择')){return $selected}
  $Default=$selected
 }
}
function PlainLicense([string]$Text){
 $headers=$null
 foreach($line in ($Text -split "`r?`n")){
  $line=$line -replace '\[([^]]+)\]\(([^)]+)\)','$1 ($2)' -replace '`',''
  if($line -match '^\|[-:| ]+\|$'){continue}
  if($line.StartsWith('|')){
   $cells=@($line.Trim('|').Split('|')|ForEach-Object {$_.Trim()})
   if($null -eq $headers){$headers=$cells;continue}
   for($i=0;$i -lt $cells.Count;$i++){Write-Host ('  '+$headers[$i]+'：'+$cells[$i])}
   Write-Host ''
  }else{$headers=$null;Write-Host ($line -replace '^#{1,6}\s*','')}
 }
}
function ClearTransfer {
 if($null -ne $script:transferTop -and -not [Console]::IsOutputRedirected){
  try{[Console]::SetCursorPosition(0,$script:transferTop);[Console]::Write((' ' * ([Console]::WindowWidth-1))+"`n"+(' ' * ([Console]::WindowWidth-1)));[Console]::SetCursorPosition(0,$script:transferTop)}catch{}
 }
 $script:transferTop=$null
}
function ConsoleLine([string]$Text,[int]$Width){
 $out='';$used=0
 foreach($ch in $Text.ToCharArray()){$size=if([int]$ch -ge 0x2e80){2}else{1};if($used+$size -gt $Width){break};$out+=$ch;$used+=$size}
 return $out+(' '*[Math]::Max(0,$Width-$used))
}
function ShowTransfer($Data){
 $amount=if($Data.total -gt 0){'{0:N1}/{1:N1} MiB ({2:N1}%)' -f ($Data.bytes/1MB),($Data.total/1MB),(100*$Data.bytes/$Data.total)}else{'{0:N1} MiB / 总量未知' -f ($Data.bytes/1MB)}
 $eta=if($null -ne $Data.eta_seconds){[string]$Data.eta_seconds+' 秒'}else{'待估算'}
 $lines=@(('文件：'+$Data.file),('来源：'+$Data.source+' | '+$amount+' | 预计剩余：'+$eta))
 if([Console]::IsOutputRedirected){if($Data.done){$lines|ForEach-Object {Write-Host $_}};return}
 try{
  $width=[Math]::Max(10,[Console]::WindowWidth-1)
  if($null -eq $script:transferTop){$script:transferTop=[Console]::CursorTop;[Console]::Write("`n`n")}
  [Console]::SetCursorPosition(0,$script:transferTop)
  [Console]::Write((ConsoleLine $lines[0] $width)+"`n"+(ConsoleLine $lines[1] $width))
 }catch{if($Data.done){$lines|ForEach-Object {Write-Host $_}}}
}
function LogReference {
 $paths=@($script:desktopLog)
 foreach($folder in @((Join-Path $script:root 'logs'),(Join-Path $script:root 'instance/logs'))){
  if(Test-Path -LiteralPath $folder){$paths+=@(Get-ChildItem -LiteralPath $folder -Filter '*.log' -File|Sort-Object LastWriteTime -Descending|Select-Object -First 2|ForEach-Object FullName)}
 }
 return ($paths -join '；')
}

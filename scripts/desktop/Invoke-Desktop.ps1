param([ValidateSet('Install','Start','Migrate')][string]$Action,[string]$Root,[switch]$NoBrowser)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$project=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$registry=Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant-launcher\installation.txt'
. (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts/desktop/Initialize-DesktopInteraction.ps1')
function ShowProgress([string]$Line){
 if($script:desktopLog){Add-Content -LiteralPath $script:desktopLog -Value $Line -Encoding UTF8}
 if($Line -match '^\[Fat-Fish-Assistant\] transfer: (.*)$'){ShowTransfer ($Matches[1]|ConvertFrom-Json);return}
 ClearTransfer
 if($Line -match '1/5'){Write-Host '（1/5）检查电脑和网络……'}
 elseif($Line -match '2/5'){Write-Host '（2/5）准备运行环境……'}
 elseif($Line -match '3/5'){Write-Host '（3/5）安装肥鱼助手和插件……'}
 elseif($Line -match '4/5'){Write-Host '（4/5）安装你选择的功能……'}
 elseif($Line -match '5/5'){Write-Host '（5/5）准备你的设置……'}
 elseif($Line -match 'download|下载'){return}elseif($Line -match '等待|冷启动'){Write-Host '肥鱼正在启动，首次启动可能需要几分钟，请不要关闭窗口……'}
}
function RunJson([string]$Executable,[string[]]$Arguments,[hashtable]$Payload){
 function QuoteArg([string]$Value){
  $escaped=[regex]::Replace($Value,'(\\*)"','$1$1\"')
  $escaped=[regex]::Replace($escaped,'(\\+)$','$1$1')
  return '"'+$escaped+'"'
 }
 $start=New-Object Diagnostics.ProcessStartInfo
 $start.FileName=$Executable
 $start.Arguments=($Arguments|ForEach-Object {QuoteArg $_}) -join ' '
 $start.EnvironmentVariables['FFA_TRANSFER_PROGRESS']='1'
 $start.UseShellExecute=$false;$start.CreateNoWindow=$true;$start.WindowStyle='Hidden'
 $start.RedirectStandardInput=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
 $start.StandardOutputEncoding=New-Object Text.UTF8Encoding($false);$start.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
 $process=New-Object Diagnostics.Process;$process.StartInfo=$start
 try{
  [void]$process.Start()
  $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadLineAsync()
  if($null -ne $Payload){
   $json=$Payload|ConvertTo-Json -Compress
   $json=[regex]::Replace($json,'[^\x00-\x7F]',[Text.RegularExpressions.MatchEvaluator]{param($m) '\u{0:x4}' -f [int][char]$m.Value[0]})
   $process.StandardInput.WriteLine($json);$json=$null
  }
  $process.StandardInput.Close()
  while(-not $process.HasExited -or $null -ne $stderr){
   try{CheckCancel}catch [OperationCanceledException]{
    if(-not $script:cancelAnnounced){ClearTransfer;Write-Host '已收到 Ctrl+C，当前步骤结束后停止全部服务……';$script:cancelAnnounced=$true}
   } # Finish the owned operation before stopping services.
   if($null -ne $stderr -and $stderr.IsCompleted){
    $line=$stderr.GetAwaiter().GetResult()
    if($null -eq $line){$stderr=$null}
    else{if($line.StartsWith('[Fat-Fish-Assistant]')){ShowProgress $line};$stderr=$process.StandardError.ReadLineAsync()}
   }else{Start-Sleep -Milliseconds 100}
  }
  $process.WaitForExit();$raw=$stdout.GetAwaiter().GetResult();$exitCode=$process.ExitCode
 }finally{$process.Dispose()}
 ClearTransfer
 if([FatFishConsole]::CancelRequested -and -not $script:stopping){throw [OperationCanceledException]::new('用户按 Ctrl+C 停止服务')}
 if(-not $raw){throw ('管理接口没有返回结果，详细日志：'+(LogReference))}
 $result=$raw|ConvertFrom-Json
 if(-not $result.ok){throw (($result.errors|ForEach-Object {$_.message+' '+$_.hint}) -join "`n")}
 if($exitCode -ne 0){throw ('管理进程异常退出：'+$exitCode+'，详细日志：'+(LogReference))}
 return $result.data
}
function Api([string[]]$Arguments){
 $forward=@('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $project 'Fat-Fish-Assistant.ps1'),'-Root',$script:root,'-Json','-NonInteractive')
 $recordPath=Join-Path $script:root 'installation.json'
 if(Test-Path -LiteralPath $recordPath){
  $record=Get-Content -Raw -Encoding UTF8 -LiteralPath $recordPath|ConvertFrom-Json
  if($record.chat_python){
   $candidate=if([IO.Path]::IsPathRooted($record.chat_python)){$record.chat_python}else{Join-Path $script:root $record.chat_python}
   if(Test-Path -LiteralPath $candidate){$forward+=@('-Python',$candidate)}
  }
 }
 return RunJson 'powershell.exe' ($forward+$Arguments) $null
}
function SecretApi([string[]]$Arguments,[hashtable]$Payload){
 $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $script:root 'installation.json')|ConvertFrom-Json
 $python=if([IO.Path]::IsPathRooted($record.chat_python)){$record.chat_python}else{Join-Path $script:root $record.chat_python}
 # Secrets only enter stdin, never command-line arguments or a temporary file.
 return RunJson $python (@('-X','utf8','-B','-I',(Join-Path $project 'ffa.py'),'--root',$script:root)+$Arguments) $Payload
}
function Folder([string]$Description,[string]$Default){
 $dialog=New-Object Windows.Forms.FolderBrowserDialog
 $dialog.Description=$Description;$dialog.SelectedPath=$Default
 try{if($dialog.ShowDialog() -ne 'OK'){throw '用户取消了目录选择'};return $dialog.SelectedPath}finally{$dialog.Dispose()}
}
function ReadSecret([string]$Prompt,[switch]$Confirm){
 $secure=ReadInput $Prompt -AsSecureString
 $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
 try{$value=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}
 if($Confirm){
  $again=ReadSecret '请再次输入迁移密码'
  if($value -cne $again){throw '两次密码不一致'}
 }
 return $value
}
function RegisterRoot {
 $marker=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $script:root '.ffa-managed.json')|ConvertFrom-Json
 if($marker.project -ne 'Fat-Fish-Assistant' -or $marker.schema_version -ne 1){throw '拒绝登记未知安装目录'}
 New-Item -ItemType Directory -Force (Split-Path $registry -Parent)|Out-Null
 $pending=$registry+'.pending-'+[guid]::NewGuid().ToString('N')
 Set-Content -LiteralPath $pending -Value $script:root -Encoding UTF8
 Move-Item -LiteralPath $pending -Destination $registry -Force
}
function RequiresConfiguration($Record,[string]$InstallationRoot){
 if(-not $Record.instance){return $true}
 $runtime=Join-Path $InstallationRoot $Record.instance
 foreach($file in @('private/onebot-token.txt','private/deepseek-key.txt','private/bot-identity.json')){if(-not(Test-Path -LiteralPath (Join-Path $runtime $file))){return $true}}
 return $false
}
function AcceptLicenses {
 PlainLicense (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $project 'THIRD_PARTY_NOTICES.md'))
 if(-not (EnterKey '回车接受以上所有条款；按其他任意键表示不接受')){throw '未接受外部许可'}
}

function SetBotIdentity {
 $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $script:root 'installation.json')|ConvertFrom-Json
 $runtime=Join-Path $script:root $record.instance
 if(Test-Path -LiteralPath (Join-Path $runtime 'private/bot-identity.json')){return}
 $bot=ReadInput '请输入 SnowLuma 登录所用的机器人 QQ 号码（仅数字）'
 if($bot -notmatch '^[0-9]+$'){throw '机器人 QQ 格式不正确'}
 Api @('config','initialize-onebot','--bot-id',$bot)|Out-Null
}
function OpenAstrBotSettings {
 if($NoBrowser){return}
 $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $script:root 'installation.json')|ConvertFrom-Json
 $python=if([IO.Path]::IsPathRooted($record.chat_python)){$record.chat_python}else{Join-Path $script:root $record.chat_python}
 $runtime=Join-Path $script:root $record.instance
 $loginArgs=@('-X','utf8','-B','-I',(Join-Path $project 'fat_fish_management/dashboard_login.py'),'--instance',$runtime)
 try{
  $login=RunJson $python $loginArgs $null
  if($login.mode -eq 'credentials_required'){
   $password=ReadSecret '请输入当前 AstrBot Dashboard 密码（隐藏输入）'
   try{
    $login=RunJson $python ($loginArgs+@('--update')) @{password=$password}
   }finally{$password=$null}
  }
  if($login.saved){Write-Host 'AstrBot Dashboard 密码已验证，并由 Windows 加密保存。'}
  Start-Process $login.url
  if($login.mode -eq 'automatic'){Write-Host 'AstrBot 设置页已打开，正在自动登录本机账户。'}
  elseif($login.mode -eq 'two_factor'){Write-Host 'AstrBot 设置页已打开，请在浏览器完成二次验证。' -ForegroundColor Yellow}
  else{Write-Host 'AstrBot 设置页已打开，请在浏览器登录。' -ForegroundColor Yellow}
 }catch [OperationCanceledException]{throw}
 catch{
  Write-Host 'AstrBot 自动登录未完成。已保存的凭据可在下次启动时重试；请在浏览器登录。' -ForegroundColor Yellow
  $view=Api @('config','show');Start-Process ('http://127.0.0.1:'+$view.dashboard.port)
 }finally{$login=$null}
}
function AssertInstalledSourceCurrent {
 $recordPath=Join-Path $script:root 'installation.json'
 if(-not (Test-Path -LiteralPath $recordPath)){return}
 $record=Get-Content -Raw -Encoding UTF8 -LiteralPath $recordPath|ConvertFrom-Json
 if(-not $record.instance){return}
 $instancePath=Join-Path (Join-Path $script:root $record.instance) 'instance.json'
 $active=Get-Content -Raw -Encoding UTF8 -LiteralPath $instancePath|ConvertFrom-Json
 foreach($tree in @('src','plugins')){
 $sourceRoot=Join-Path $project $tree
 foreach($source in Get-ChildItem -LiteralPath $sourceRoot -File -Recurse){
  if($source.Extension -eq '.pyc' -or $source.FullName -match '[\/]__pycache__[\/]'){continue}
  $relative=$source.FullName.Substring($sourceRoot.Length+1)
  $installed=Join-Path (Join-Path $active.release $tree) $relative
  if(-not (Test-Path -LiteralPath $installed -PathType Leaf) -or
     (Get-FileHash -LiteralPath $source.FullName).Hash -ne (Get-FileHash -LiteralPath $installed).Hash){
   throw '已安装的 Bot 发布包与当前源码不同。请运行 Install-Fat-Fish-Assistant.cmd，选择 2 修复/继续中断安装，完成后再启动和复测。'
  }
 }
}
}
function Launch {
 AssertInstalledSourceCurrent
 Write-Host '正在启动肥鱼助手，请稍候……'
 $result=Api @('daily-start')
 if($result.dashboard.authenticated_dashboard -eq 'login_required'){
  Write-Host 'AstrBot 已启动。Dashboard 需要重新认证，QQ 连接检查将继续。' -ForegroundColor Yellow
 }
 foreach($warning in $result.warnings){
  $label=switch($warning.component){'embedding'{'长期记忆'}'speech'{'语音识别'}default{'附加功能'}}
  Write-Host ($label+'暂时无法启动，其他可用功能会继续启动。详细日志：'+(LogReference)) -ForegroundColor Yellow
 }
 $view=Api @('config','show')
 $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $script:root 'installation.json')|ConvertFrom-Json
 Write-Host ('人格设置：在浏览器打开 http://127.0.0.1:'+$view.dashboard.port+'，进入「人格」编辑默认人格 feiyu-v1；群人格请在对应群使用 /群人格。')
 $settingsRuntime=Join-Path $script:root $record.instance
 Write-Host ('人格参考文件：'+(Join-Path $settingsRuntime 'persona/Persona_Master.md')+'；仅编辑参考文件不会自动覆盖已保存的人格，请在设置页修改。')
 Write-Host ('表情包：在设置页进入「插件 → 表情包管理器」导入，再保存使用规则；插件配置文件：'+(Join-Path $settingsRuntime 'data/config/meme_manager_config.json'))
 Write-Host ('其他机器人设置文件：'+(Join-Path $settingsRuntime 'data/cmd_config.json')+'；直接编辑配置文件前请先停止肥鱼助手。')
 Write-Host ('设置页登录密码文件：'+(Join-Path $script:root $view.dashboard_password_file))
 try{OpenAstrBotSettings}
 catch [OperationCanceledException]{throw}
 catch{Write-Host 'AstrBot 设置页暂未打开。' -ForegroundColor Yellow}
 if($result.connection.onebot.connected){
  Write-Host '已通过 OneBot 验证绑定的 QQ 身份和在线状态。请在 QQ 中发送一条消息验证回复。'
 }else{
  Write-Host 'AstrBot 已启动，等待 QQ 通信。请用 SnowLuma 官方 Launcher 启动，在官方 WebUI 登录并配置反向 WebSocket，完成后重新查看健康状态。' -ForegroundColor Yellow
 }
}
try{
 if(-not $root){if(Test-Path -LiteralPath $registry){$root=(Get-Content -Raw -Encoding UTF8 -LiteralPath $registry).Trim()}else{$root=Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant'}}
 $logFolder=Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant-launcher/logs'
 New-Item -ItemType Directory -Force -Path $logFolder|Out-Null
 $script:desktopLog=Join-Path $logFolder ('Desktop-'+[guid]::NewGuid().ToString('N')+'.log')
 Set-Content -LiteralPath $script:desktopLog -Value ('Action='+$Action) -Encoding UTF8
 if(-not [Console]::IsInputRedirected){[FatFishConsole]::Attach();$script:consoleAttached=$true}
 if($Action -eq 'Install'){
  AcceptLicenses
  $root=ChooseInstallRoot $root
  if(-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10){throw '需要 Windows 10/11 x64'}
  $drive=New-Object IO.DriveInfo([IO.Path]::GetPathRoot($root))
  if($drive.AvailableFreeSpace -lt 2GB){throw '磁盘空间不足，核心安装至少需要 2 GB，可选模型需要更多空间'}
  $existing=Test-Path -LiteralPath (Join-Path $root 'installation.json')
  $checkOnly=$false
  $resumeConfiguration=$false
  if($existing){
   $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'installation.json')|ConvertFrom-Json
   $journal=Join-Path $root 'install-journal.json'
   if((Test-Path -LiteralPath $journal) -and ((Get-Content -Raw -Encoding UTF8 -LiteralPath $journal|ConvertFrom-Json).state -eq 'completed') -and (RequiresConfiguration $record $root)){
    $resumeConfiguration=$true
    Write-Host '软件已安装，将继续完成上次未完成的设置。已有聊天和设置会保留。'
   }else{
    Write-Host '检测到已有环境：1 重复检查（默认）；2 修复/继续中断安装（会正常停止服务）。现有聊天、身份及权限不会替换。'
    $checkOnly=(ReadInput '请选择 1 或 2') -ne '2'
   }
  }
  if($checkOnly){$check=Api @('check','--network');if(-not $check.ready){throw '电脑或网络检查未通过，请查看诊断日志后重试。'};Write-Host '检查完成，已有安装可以使用。';RegisterRoot}
  else{
   if($resumeConfiguration){Api @('stop')|Out-Null}
   else{
   $components='chat'
   Write-Host '进入插件选配环节。'
   foreach($item in @(@{id='embedding';label='长期记忆（推荐）';default=$true},@{id='speech';label='语音识别（可选）';default=$true})){
    $hint=if($item.default){'[Y/N]，回车安装；输入 N 不安装'}else{'[y/N]，直接回车跳过'}
    $answer=ReadInput ('是否安装 '+$item.label+'？'+$hint)
    if($answer -match '^[Yy]$' -or (-not $answer -and $item.default)){$components+=','+$item.id}
   }
   $owner=$null
   if($existing){$record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'installation.json')|ConvertFrom-Json;if($record.instance){$owner=(Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root ($record.instance+'/private/instance-identity.json'))|ConvertFrom-Json).owner}}
   if(-not $owner){$owner=ReadInput '请输入主人 QQ 号码（仅数字）'}
   if($owner -notmatch '^[0-9]+$'){throw '主人 QQ 格式不正确'}
   Write-Host '正在下载安装，首次使用可能需要较长时间，请保留窗口。'
   $options=@('install','--owner',$owner,'--components',$components,'--accept-external-licenses')
   if($existing){Api @('stop')|Out-Null;$options+='--repair'}
   Api $options|Out-Null
   RegisterRoot
   }
   $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'installation.json')|ConvertFrom-Json
   $runtime=Join-Path $root $record.instance
   if($record.components.embedding.installed -or $record.components.speech.installed){Api @('config','initialize-local-models')|Out-Null}
   if(-not(Test-Path -LiteralPath (Join-Path $runtime 'private/onebot-token.txt'))){Api @('config','initialize-chat')|Out-Null}
   if(-not(Test-Path -LiteralPath (Join-Path $runtime 'private/deepseek-key.txt'))){
    $key=ReadSecret '请输入 API Key（隐藏输入，不写普通日志）'
    try{SecretApi @('config','apply','--file','-') @{deepseek_api_key=$key}|Out-Null}finally{$key=$null}
   }
   SetBotIdentity
   if(-not(Test-Path -LiteralPath (Join-Path $runtime 'private/experience-v3.json'))){
    Write-Host '正在准备分条回复和鲸鱼娘表情……'
    Api @('config','initialize-experience')|Out-Null
   }
   RegisterRoot
  }
 }
 if($Action -eq 'Migrate'){
  Write-Host '迁移向导：1 迁出当前肥鱼助手；2 从迁移包导入（目标不能已有实例）。'
  $choice=ReadInput '请选择 1 或 2'
  if($choice -eq '1'){
   if((ReadInput '请先通过官方渠道停止 SnowLuma。迁出会停止 FFA 受管服务并暂停源实例。继续请输入 YES') -cne 'YES'){throw '已取消迁出'}
   $dialog=New-Object Windows.Forms.SaveFileDialog
   $dialog.Filter='肥鱼助手加密迁移包 (*.ffa.zip)|*.ffa.zip';$dialog.FileName='Fat-Fish-Assistant-Migration-'+(Get-Date -Format 'yyyy-MM-dd')+'.ffa.zip'
   try{if($dialog.ShowDialog() -ne 'OK'){throw '已取消保存'};$destination=$dialog.FileName}finally{$dialog.Dispose()}
   $password=ReadSecret '设置迁移密码（至少 12 个字符；请妥善保管）' -Confirm
   try{$result=SecretApi @('migration-export','--destination',$destination) @{password=$password}}finally{$password=$null}
   Write-Host ('迁移包已保存：'+$result.path)
   Write-Host ('包含 '+$result.files+' 个数据文件，大小 '+[Math]::Round($result.bytes/1MB,1)+' MB')
   Write-Host ('文件校验码 SHA256：'+$result.sha256)
   Write-Host '迁出完成。只需搬运这个加密包；源实例已暂停，请保持停止，在目标主机选择导入。'
   exit 0
  }elseif($choice -eq '2'){
   $dialog=New-Object Windows.Forms.OpenFileDialog
   $dialog.Filter='肥鱼助手迁移包 (*.ffa.zip)|*.ffa.zip'
   try{if($dialog.ShowDialog() -ne 'OK'){throw '已取消导入'};$package=$dialog.FileName}finally{$dialog.Dispose()}
   $password=ReadSecret '请输入迁移密码'
   AcceptLicenses
   $root=Folder '选择新的空安装目录，不得覆盖现有实例' (Join-Path $env:LOCALAPPDATA 'Fat-Fish-Assistant-Imported')
   if((ReadInput '确认源主机已经停止并保持暂停，目标是唯一使用此 QQ 的实例：请输入 YES') -cne 'YES'){throw '尚未确认源实例停止'}
   Write-Host '准备固定版本运行环境；随后验证密码、数据和依赖版本，自动恢复到新实例。'
   try{
    Api @('install','--no-instance','--components','chat','--accept-external-licenses')|Out-Null
    $result=SecretApi @('migration-import','--package',$package,'--confirm-source-offline') @{password=$password}
   }finally{$password=$null}
   Write-Host ('已恢复 '+$result.files+' 个数据文件。正在准备登录……')
   SetBotIdentity
   RegisterRoot
  }else{throw '选项无效'}
 }
 if($Action -eq 'Install'){
  $record=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $root 'installation.json')|ConvertFrom-Json
  if($record.instance -and (Test-Path -LiteralPath (Join-Path $root ($record.instance+'/private/MIGRATED-OUT.json')))){
   if((ReadInput '源实例因迁出已暂停。只有目标主机已停止且不再使用此 QQ 时，输入 YES 撤销暂停') -cne 'YES'){throw '源实例保持暂停，防止两个主机同时消费同一 QQ'}
   Api @('resume-source','--confirm-target-offline')|Out-Null
  }
 }
 if(-not(Test-Path -LiteralPath (Join-Path $root '.ffa-managed.json'))){throw '未找到已注册安装，请先双击安装脚本。'}
 Launch
 Write-Host '按 Ctrl+C 停止 FFA 受管服务。SnowLuma 请在官方界面单独停止。'
 while($true){CheckCancel;Start-Sleep -Milliseconds 100}
}catch [OperationCanceledException]{
 Write-Host '正在停止全部服务……'
}catch{
 Write-Host ('操作未完成：'+$_.Exception.Message) -ForegroundColor Red
 if($script:desktopLog){Add-Content -LiteralPath $script:desktopLog -Value $_.Exception.Message -Encoding UTF8;Write-Host ('诊断日志：'+(LogReference)+'；已有数据和失败阶段会保留。')}
 exit 1
}
finally{
 ClearTransfer
 if([FatFishConsole]::CancelRequested -and $root -and (Test-Path -LiteralPath (Join-Path $root 'installation.json'))){
  $script:stopping=$true
  try{Api @('stop')|Out-Null;Add-Content -LiteralPath $script:desktopLog -Value 'All managed services stopped' -Encoding UTF8;Write-Host '全部受管服务已停止。'}catch{Add-Content -LiteralPath $script:desktopLog -Value $_.Exception.Message -Encoding UTF8;Write-Host ('停止未完成：'+$_.Exception.Message+'；详细日志：'+(LogReference));$script:stopFailed=$true}
 }
 if($script:consoleAttached){[FatFishConsole]::Detach()}
 if($script:stopFailed){exit 1}
}

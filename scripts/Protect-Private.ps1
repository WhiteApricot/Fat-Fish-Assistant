param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference='Stop'
$boundary=(Resolve-Path -LiteralPath $Path).Path
if(-not(Test-Path -LiteralPath (Join-Path $boundary '.fat-fish-assistant-private')) -and -not(Test-Path -LiteralPath (Join-Path $boundary '.feiyu-private-boundary')) -and -not(Test-Path -LiteralPath (Join-Path $boundary 'instance.json'))){throw 'Not a marked Fat-Fish-Assistant private boundary; refusing ACL change'}
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=Get-Acl -LiteralPath $boundary
$before=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($true,$false)
foreach($rule in @($acl.Access)){$acl.RemoveAccessRuleAll($rule)}
$inherit=[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$propagation=[Security.AccessControl.PropagationFlags]::None
foreach($sid in @($owner,(New-Object Security.Principal.SecurityIdentifier('S-1-5-18')),(New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))){
 $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl',$inherit,$propagation,'Allow')
 $acl.AddAccessRule($rule)
}
if($before -eq $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)){
 'Private boundary permissions already match; no recursive propagation needed.'
 exit 0
}
$directory=Get-Item -LiteralPath $boundary
if($PSVersionTable.PSEdition -eq 'Desktop'){$directory.SetAccessControl($acl)}
else{[IO.FileSystemAclExtensions]::SetAccessControl($directory,$acl)}
'Private boundary restricted to its operator, SYSTEM and local administrators.'

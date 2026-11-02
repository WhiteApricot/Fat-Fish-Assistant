# Toolhelp supplies parent IDs without WMI. QueryLimitedInformation supplies
# executable and creation identity; recorded PID queries fail closed.
if(-not ('FeiyuProcessSnapshot' -as [type])){
 Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class FeiyuProcessSnapshot {
 [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Auto)]
 struct Entry { public uint size,usage,pid; public IntPtr heap; public uint module,threads,parent; public int priority; public uint flags; [MarshalAs(UnmanagedType.ByValTStr,SizeConst=260)] public string name; }
 public class Row { public int ProcessId; public int ParentProcessId; public string Name; }
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags,uint pid);
 [DllImport("kernel32.dll",CharSet=CharSet.Auto,SetLastError=true)] static extern bool Process32First(IntPtr h,ref Entry e);
 [DllImport("kernel32.dll",CharSet=CharSet.Auto,SetLastError=true)] static extern bool Process32Next(IntPtr h,ref Entry e);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 public static Row[] Read(){
  var h=CreateToolhelp32Snapshot(2,0);if(h==new IntPtr(-1))throw new System.ComponentModel.Win32Exception();
  try {var e=new Entry();e.size=(uint)Marshal.SizeOf(e);var rows=new List<Row>();
   if(!Process32First(h,ref e))throw new System.ComponentModel.Win32Exception();
   do{rows.Add(new Row{ProcessId=(int)e.pid,ParentProcessId=(int)e.parent,Name=e.name});}while(Process32Next(h,ref e));
   if(Marshal.GetLastWin32Error()!=18)throw new System.ComponentModel.Win32Exception();
   return rows.ToArray();
  }finally{CloseHandle(h);}
 }
}
'@
}

if(-not ('FeiyuIdentity' -as [type])){
 Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class FeiyuIdentity {
 public class Info { public int ProcessId; public string ExecutablePath; public DateTime CreationDate; }
 [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr h,uint flags,StringBuilder name,ref int length);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr h,out long creation,out long exit,out long kernel,out long user);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr h,int kind,IntPtr buffer,int size,out int needed);
 [DllImport("ntdll.dll")] static extern uint RtlNtStatusToDosError(int status);
 public static string CommandLine(int pid){
  var h=OpenProcess(0x1000,false,pid);if(h==IntPtr.Zero)throw new System.ComponentModel.Win32Exception();
  try{int size;NtQueryInformationProcess(h,60,IntPtr.Zero,0,out size);
   if(size<=0)throw new InvalidOperationException("Cannot query command line size");
   var buffer=Marshal.AllocHGlobal(size);
   try{int status=NtQueryInformationProcess(h,60,buffer,size,out size);
    if(status<0)throw new System.ComponentModel.Win32Exception((int)RtlNtStatusToDosError(status));
    var text=Marshal.ReadIntPtr(buffer,IntPtr.Size==8?8:4);return Marshal.PtrToStringUni(text,(ushort)Marshal.ReadInt16(buffer)/2)??"";
   }finally{Marshal.FreeHGlobal(buffer);}
  }finally{CloseHandle(h);}
 }
 public static Info Read(int pid){
  var h=OpenProcess(0x1000,false,pid);
  if(h==IntPtr.Zero){int e=Marshal.GetLastWin32Error();if(e==87)return null;throw new System.ComponentModel.Win32Exception(e);}
  try{var path=new StringBuilder(32768);int size=path.Capacity;long c,e,k,u;
   if(!QueryFullProcessImageName(h,0,path,ref size)||!GetProcessTimes(h,out c,out e,out k,out u))throw new System.ComponentModel.Win32Exception();
   return new Info{ProcessId=pid,ExecutablePath=path.ToString(),CreationDate=DateTime.FromFileTimeUtc(c)};
  }finally{CloseHandle(h);}
 }
}
'@
}
function Get-NativeProcessInfo([int]$ProcessId,[int]$ParentProcessId=0,[string]$ExpectedPath,[switch]$Scan){
 try{$info=[FeiyuIdentity]::Read($ProcessId)}catch{
  # A scan is discovery, not authorization to control an unreadable process.
  # Managed PID queries never use Scan and must fail closed.
  if($Scan){return};throw
 }
 if($null -eq $info){return}
 if($ExpectedPath -and -not [string]::Equals($info.ExecutablePath,$ExpectedPath,[StringComparison]::OrdinalIgnoreCase)){return}
 [pscustomobject]@{ProcessId=$info.ProcessId;ParentProcessId=$ParentProcessId;ExecutablePath=$info.ExecutablePath;CreationDate=$info.CreationDate}
}

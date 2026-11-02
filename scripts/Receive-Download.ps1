function OpenDownloadResponse([string]$Source){
 $request=[Net.WebRequest]::Create($Source);$request.Timeout=30000;$request.ReadWriteTimeout=30000
 return $request.GetResponse()
}
function ReceiveDownload([string]$Source,[string]$Destination,[string]$Name){
 $response=$null;$inputStream=$null;$outputStream=$null
 $timer=[Diagnostics.Stopwatch]::StartNew();$received=0L;$last=-1000L;$total=0L
 function NotifyDownload([bool]$Done){
  if($env:FFA_TRANSFER_PROGRESS -ne '1'){return}
  $eta=if($total -gt 0 -and $received -gt 0){[Math]::Round([Math]::Max(0,$total-$received)*$timer.Elapsed.TotalSeconds/$received)}else{$null}
  $frame=@{file=$Name;source=([Uri]$Source).Host;bytes=$received;total=$total;eta_seconds=$eta;done=$Done}|ConvertTo-Json -Compress
  [Console]::Error.WriteLine('[Fat-Fish-Assistant] transfer: '+$frame)
 }
 try{
  NotifyDownload $false
  $response=OpenDownloadResponse $Source
  if($response.ResponseUri.Scheme -ne 'https'){throw 'Insecure redirect refused'}
  $total=[Math]::Max(0,[long]$response.ContentLength)
  $inputStream=$response.GetResponseStream();$outputStream=[IO.File]::Open($Destination,[IO.FileMode]::CreateNew)
  $buffer=New-Object byte[] 65536
  while(($count=$inputStream.Read($buffer,0,$buffer.Length)) -gt 0){
   $outputStream.Write($buffer,0,$count);$received+=$count
   if($timer.ElapsedMilliseconds-$last -ge 250){NotifyDownload $false;$last=$timer.ElapsedMilliseconds}
  }
  NotifyDownload $true
 }finally{
  if($outputStream){$outputStream.Dispose()};if($inputStream){$inputStream.Dispose()};if($response){$response.Close()}
 }
}

param([ValidateSet('stop','start','is-stopped')][string]$Action,[Parameter(Mandatory=$true)][string]$Executable,[int]$WaitSeconds=20)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
trap { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
$resolved=[IO.Path]::GetFullPath($Executable)
if((Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant() -ne '0e55aea3120d8c6c487af633542db9d79760b5ac90a1ecd9ae56219c890349e2'){
    [Console]::Error.WriteLine('WeFlow.exe SHA-256 mismatch'); exit 2
}
$processes=@(Get-Process WeFlow -ErrorAction SilentlyContinue | Where-Object {$_.Path -and [IO.Path]::GetFullPath($_.Path) -eq $resolved})
if($Action -eq 'is-stopped'){if($processes.Count -gt 0){exit 1};exit 0}
if($Action -eq 'start'){
    if($processes.Count -eq 0){Start-Process -FilePath $resolved -WorkingDirectory ([IO.Path]::GetDirectoryName($resolved)) -WindowStyle Hidden | Out-Null}
    exit 0
}
if($processes.Count -eq 0){exit 0}
foreach($proc in $processes){if($proc.MainWindowHandle -ne 0){$null=$proc.CloseMainWindow()}}
$deadline=[DateTime]::UtcNow.AddSeconds($WaitSeconds)
do {
    Start-Sleep -Milliseconds 250
    $remaining=@(Get-Process WeFlow -ErrorAction SilentlyContinue | Where-Object {$_.Path -and [IO.Path]::GetFullPath($_.Path) -eq $resolved})
    if($remaining.Count -eq 0){exit 0}
} while([DateTime]::UtcNow -lt $deadline)
exit 10

$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
try {
    if(-not [Environment]::Is64BitOperatingSystem){throw '本包语音驱动安装入口需要64位Windows。'}
    $taskVoiceManifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskDownloadRoot=Join-Path $PSScriptRoot 'runtime\downloads'
    New-Item -ItemType Directory -Path $taskDownloadRoot -Force | Out-Null
    $taskArchive=Join-Path $taskDownloadRoot 'VBCABLE_Driver_Pack45.zip'
    if(-not (Test-Path -LiteralPath $taskArchive) -or (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskVoiceManifest.voice_driver_sha256){
        if($env:WECHATBOT_OFFLINE -eq '1'){throw '首次安装语音驱动需要从VB-Audio官网下载；请临时联网。'}
        $taskPartial=$taskArchive+'.part'
        Invoke-WebRequest -UseBasicParsing -Uri $taskVoiceManifest.voice_driver_url -OutFile $taskPartial
        if((Get-FileHash -LiteralPath $taskPartial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskVoiceManifest.voice_driver_sha256){Remove-Item -LiteralPath $taskPartial;throw '语音驱动归档校验失败。'}
        Move-Item -LiteralPath $taskPartial -Destination $taskArchive -Force
    }
    $taskExtractRoot=Join-Path $taskDownloadRoot 'vbcable-45'
    Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskExtractRoot -Force
    $taskInstaller=Join-Path $taskExtractRoot 'VBCABLE_Setup_x64.exe'
    $taskSignature=Get-AuthenticodeSignature -LiteralPath $taskInstaller
    if($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch '^CN=BUREL VINCENT Entrepreneur individuel,'){throw 'VB-Audio安装程序签名不匹配，停止安装。'}
    Write-Host '即将打开VB-Audio官方驱动安装器，请允许Windows UAC并点击Install Driver；完成后重启电脑。'
    # This is an interactive driver installer: its Install Driver button must be visible.
    Start-Process -FilePath $taskInstaller -WorkingDirectory $taskExtractRoot -Verb RunAs -WindowStyle Normal -Wait
    Write-Host '安装器已退出。请按官方要求重启电脑，随后再次双击一键启动。'
} catch {
    Write-Host ('语音驱动安装未完成：'+$_.Exception.Message) -ForegroundColor Red
    exit 1
}

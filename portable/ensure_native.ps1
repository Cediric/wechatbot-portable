# Native prerequisites use standard Microsoft installers and Windows UAC.
function Ensure-WechatbotVisualCpp([bool]$InspectOnly=$false) {
    $taskVcPresent=(Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\vcruntime140.dll')) -and (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\msvcp140.dll'))
    if($taskVcPresent){return}
    if($InspectOnly){Write-Host '未检测到 x64 Visual C++ 运行库；正式启动会从微软下载并请求 Windows UAC 安装。';return}
    if($env:WECHATBOT_OFFLINE -eq '1'){throw '系统缺少 x64 Visual C++ 运行库；请联网启动一次，或先从微软安装运行库。'}
    $taskVcManifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskNativeRoot=Join-Path $PSScriptRoot 'runtime\downloads'
    New-Item -ItemType Directory -Path $taskNativeRoot -Force | Out-Null
    $taskVcInstaller=Join-Path $taskNativeRoot 'VC_redist.x64.exe'
    if(-not (Test-Path -LiteralPath $taskVcInstaller) -or (Get-FileHash -LiteralPath $taskVcInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskVcManifest.vc_runtime_sha256){
        $taskPartial=$taskVcInstaller+'.part'
        Invoke-WebRequest -UseBasicParsing -Uri $taskVcManifest.vc_runtime_url -OutFile $taskPartial
        if((Get-FileHash -LiteralPath $taskPartial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskVcManifest.vc_runtime_sha256){Remove-Item -LiteralPath $taskPartial;throw '微软运行库下载校验失败。'}
        Move-Item -LiteralPath $taskPartial -Destination $taskVcInstaller -Force
    }
    $taskSignature=Get-AuthenticodeSignature -LiteralPath $taskVcInstaller
    if($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation(?:,|$)'){throw '微软运行库数字签名无效，停止安装。'}
    Write-Host '安装缺失的微软运行库；请在 Windows UAC 中允许官方安装程序。'
    $taskInstall=Start-Process -FilePath $taskVcInstaller -ArgumentList @('/install','/quiet','/norestart') -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if($taskInstall.ExitCode -notin @(0,1638,3010)){throw ('微软运行库安装未完成，退出码 '+$taskInstall.ExitCode)}
    if($taskInstall.ExitCode -eq 3010){Write-Host '微软运行库已安装；Windows 建议稍后重启。'}
}

param([ValidateSet('all','open','bridge','restart','restart-astrbot','prepare','check')][string]$Action='all')
$ErrorActionPreference='Stop'
# A caller running PowerShell 7 may supply only its own module directories.
# Keep those paths, but locate this Windows PowerShell process's modules first.
$taskBuiltinModuleRoot=[IO.Path]::Combine($PSHOME,'Modules')
$taskInheritedModulePaths=$env:PSModulePath
$env:PSModulePath=$taskBuiltinModuleRoot
if(-not [String]::IsNullOrWhiteSpace($taskInheritedModulePaths)){$env:PSModulePath+=[IO.Path]::PathSeparator+$taskInheritedModulePaths}
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$packageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot=Join-Path $PSScriptRoot 'runtime'
$vendorRoot=Join-Path $PSScriptRoot 'vendor'
$script:taskStartupStage='读取便携运行清单'
$script:taskPythonBootstrapStarted=$false
$script:taskPythonBootstrapStartUtc=[DateTime]::MinValue
function Protect-WechatbotDiagnostic([string]$value){
    $safe=$value
    $safe=[regex]::Replace($safe,'(?im)^.*(?:\bcommand(?:\s|=|:)|--(?:api[_-]?key|token|password|secret|credential|authorization)(?:=|\s)).*$','[子进程命令已隐藏]')
    foreach($item in (Get-ChildItem Env:)){
        if($item.Name -match '(?i)api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|credential|(?:^|[_-])key$' -and -not [String]::IsNullOrEmpty($item.Value)){$safe=$safe.Replace($item.Value,'[已脱敏]')}
    }
    $safe=[regex]::Replace($safe,'(?im)^(.*?)(?:\{|\[)[^\r\n]*(?:main_llm_|api[_-]?key|provider_settings|system_prompt|memory|password)[^\r\n]*$','$1[配置对象已隐藏]')
    $safe=[regex]::Replace($safe,'(?s)\{.*\}','[对象内容已隐藏]')
    $safe=[regex]::Replace($safe,'\x1b\[[0-?]*[ -/]*[@-~]','')
    $safe=[regex]::Replace($safe,'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]','')
    $safe=[regex]::Replace($safe,'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=~-]+','Bearer [已脱敏]')
    $safe=[regex]::Replace($safe,'(?i)\bsk-[A-Za-z0-9_-]{8,}','[已脱敏]')
    $safe=[regex]::Replace($safe,'(?i)(https?://)[^\s/@]+:[^\s/@]+@','$1[已脱敏]@')
    $safe=[regex]::Replace($safe,'(?i)(https?://[^\s?''"<>]+)\?[^\s''"<>]+','$1?[查询参数已隐藏]')
    $field='(?:[\w.-]*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization|credential)[\w.-]*|key|密码|密钥|令牌)'
    $prefix='(["'']?'+$field+'["'']?\s*[:=：]\s*)'
    $safe=[regex]::Replace($safe,'(?is)'+$prefix+'\[[^\]]*\]','$1[已脱敏]')
    $safe=[regex]::Replace($safe,'(?is)'+$prefix+'(?:"(?:\\.|[^"\\])*(?:"|$)|''(?:\\.|[^''\\])*(?:''|$))','$1[已脱敏]')
    $safe=[regex]::Replace($safe,'(?im)(\bauthorization\b["'']?\s*[:=：]\s*)[^\r\n]*','$1[已脱敏]')
    $safe=[regex]::Replace($safe,'(?i)(["'']?'+$field+'["'']?\s*[:=：]\s*)(?:\[[^\]\r\n]*\]|"[^"\r\n]*"|''[^''\r\n]*''|[^\s,;\}\]\r\n]+)','$1[已脱敏]')
    return $safe
}
function Get-WechatbotDiagnosticExcerpt([string]$value){
    $safe=Protect-WechatbotDiagnostic $value
    $lines=@($safe -split '\r?\n' | Where-Object {
        $line=$_.Trim()
        $line -and $line -notmatch '^(?:\{|\[|["'']|Command |command=|env=|settings=|config=)' -and
        $line -notmatch 'os\.environ|subprocess\.(?:run|Popen)|run_checked\(|Invoke-Checked|--api[_-]?key' -and
        ($_ -notmatch '^\s' -or $line.StartsWith('File "')) -and
        $line -match '(?i)Traceback|\bFile "|\b\w*(?:Error|Exception)\b|\berror\b|\bfailed\b|\bfailure\b|not found|no such|denied|cannot|can''t|timeout|timed? out|missing|invalid|requires|unsupported|启动未完成|阶段[：:]|原因[：:]|缺少|失败|无效|未找到|无法|超时|准备|检测|检查|修复|环境准备完成'
    } | Select-Object -Last 32)
    $result=$lines -join [Environment]::NewLine
    if($result.Length -gt 12000){$result=$result.Substring($result.Length-12000)}
    return $result
}
function Invoke-Checked([string]$exe,[string[]]$argv,[string]$Stage,[switch]$CaptureOutput,[switch]$ShowStages){
    if($Stage){$script:taskStartupStage=$Stage}
    # Only audited, complete Python bootstrap stage lines may stream to the
    # console. All other stdout/stderr still wait for diagnostic redaction.
    $taskProgressStages=@(
        '阶段：桥接环境：创建独立虚拟环境',
        '阶段：桥接环境：安装并核验锁定依赖',
        '阶段：桥接环境：导入检查',
        '阶段：桥接环境：修复后导入检查',
        '阶段：AstrBot环境：创建独立虚拟环境',
        '阶段：AstrBot环境：安装并核验锁定依赖',
        '阶段：AstrBot环境：导入检查',
        '阶段：AstrBot环境：修复后导入检查',
        '阶段：AstrBot：初始化本包实例',
        '阶段：AstrBot：修复本包实例标记',
        '阶段：WeFlow：核验现有程序与桥接',
        'WeFlow 适配：若窗口缩至托盘，请右键托盘图标选择“退出”，安装器会重新打开 WeFlow。',
        'WeFlow 适配未完成：请右键 WeFlow 托盘图标选择“退出”，再双击本包的一键启动。',
        '阶段：切换旧版本微信bot',
        '阶段：启动本包桥接、AstrBot 与控制面板'
    )
    $taskShownStages=@()
    $taskOutputRecords=New-Object 'System.Collections.Generic.List[object]'
    $priorPreference=$ErrorActionPreference
    try {
        # Native stderr is data, not a PowerShell terminating error. Capture both
        # streams as their original records; never replay arbitrary raw lines.
        $ErrorActionPreference='Continue'
        & $exe @argv 2>&1 | ForEach-Object {
            $taskOutputRecords.Add($_)
            if($ShowStages -and -not $CaptureOutput -and $_ -isnot [Management.Automation.ErrorRecord]){
                $taskLine=$_.ToString()
                if($taskProgressStages -ccontains $taskLine){
                    Write-Host $taskLine
                    $taskShownStages+=$taskLine
                }
            }
        }
        $taskExitCode=$LASTEXITCODE
    } finally {$ErrorActionPreference=$priorPreference}
    $taskOutput=$taskOutputRecords.ToArray()
    $taskSafeOutput=Get-WechatbotDiagnosticExcerpt (($taskOutput | ForEach-Object {$_.ToString()}) -join [Environment]::NewLine)
    $launchFailure=@($taskOutput | Where-Object {$_ -is [Management.Automation.ErrorRecord] -and $_.Exception.GetType().Name -in @('CommandNotFoundException','ApplicationFailedException')})
    if($launchFailure.Count -or $null -eq $taskExitCode){$taskExitCode='无法启动'}
    if($taskExitCode -ne 0){
        $reason="阶段：$script:taskStartupStage`n原因：子进程退出码 $taskExitCode。"
        if($taskSafeOutput){$reason+=[Environment]::NewLine+$taskSafeOutput}
        throw $reason
    }
    # Native 2>&1 retains ErrorRecord stream identity in Windows PowerShell 5.1.
    # stdout remains data (uv python find); late stderr warnings are never paths.
    if($CaptureOutput){return @($taskOutput | Where-Object {$_ -isnot [Management.Automation.ErrorRecord]} | ForEach-Object {$_.ToString()})}
    if($taskShownStages.Count){$taskSafeOutput=($taskSafeOutput -split '\r?\n' | Where-Object {$taskShownStages -cnotcontains $_}) -join [Environment]::NewLine}
    if($taskSafeOutput){Write-Host $taskSafeOutput}
}
function Save-WechatbotStartupError($failure){
    if(-not (Test-Path -LiteralPath $runtimeRoot)){return}
    $taskErrorReport=Join-Path $runtimeRoot 'startup-error.txt'
    $pythonReason=''
    if($script:taskPythonBootstrapStarted -and (Test-Path -LiteralPath $taskErrorReport)){
        $reportInfo=Get-Item -LiteralPath $taskErrorReport
        if($reportInfo.LastWriteTimeUtc -ge $script:taskPythonBootstrapStartUtc){
            $prior=Get-Content -LiteralPath $taskErrorReport -Raw -Encoding UTF8
            if($prior.StartsWith('微信bot 启动诊断')){$pythonReason=(Protect-WechatbotDiagnostic $prior)+[Environment]::NewLine}
        }
    }
    $outer="外层启动诊断（已脱敏）`n阶段：$script:taskStartupStage`n错误类型：$($failure.Exception.GetType().Name)`n原因：$($failure.Exception.Message)`n$($failure.ScriptStackTrace)"
    [IO.File]::WriteAllText($taskErrorReport,($pythonReason+(Protect-WechatbotDiagnostic $outer)),(New-Object System.Text.UTF8Encoding($false)))
    return $taskErrorReport
}
try {
    $manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:taskStartupStage='检查系统兼容性与原生运行库'
    if(-not [Environment]::Is64BitOperatingSystem){throw '便携包需要 64 位 Windows。'}
    if([Environment]::OSVersion.Version -lt [Version]'10.0'){throw '便携包需要 Windows 10 或更新版本。'}
    . (Join-Path $PSScriptRoot 'ensure_native.ps1')
    . (Join-Path $PSScriptRoot 'extract_runtime.ps1')
    Ensure-WechatbotVisualCpp ($Action -in @('prepare','check'))
    New-Item -ItemType Directory -Path $runtimeRoot,$vendorRoot -Force | Out-Null
    # All managed runtimes and caches stay in this package. No global PATH/install changes.
    $env:UV_PYTHON_INSTALL_DIR=Join-Path $runtimeRoot 'python'
    $env:UV_CACHE_DIR=Join-Path $runtimeRoot 'uv-cache'
    $env:UV_TOOL_DIR=Join-Path $runtimeRoot 'tools'
    $env:UV_TOOL_BIN_DIR=Join-Path $runtimeRoot 'bin'
    $env:UV_PYTHON_PREFERENCE='only-managed'
    $env:UV_PYTHON_BIN_DIR=Join-Path $runtimeRoot 'bin'
    $env:PYTHONUTF8='1'
    $env:PYTHONDONTWRITEBYTECODE='1'
    New-Item -ItemType Directory -Path $env:UV_PYTHON_INSTALL_DIR -Force | Out-Null
    $script:taskStartupStage='核验本包 uv 运行归档'
    $archive=Join-Path $vendorRoot 'uv-x86_64-pc-windows-msvc.zip'
    if(-not (Test-Path -LiteralPath $archive)){
        if($env:WECHATBOT_OFFLINE -eq '1'){throw '离线包缺少uv运行归档，请重新取得完整便携包。'}
        Write-Host '首次启动：下载经过 SHA-256 核验的本包 uv 运行器。'
        $partial=$archive+'.part'
        Invoke-WebRequest -UseBasicParsing -Uri $manifest.uv_url -OutFile $partial
        if((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.uv_sha256){Remove-Item -LiteralPath $partial;throw 'uv 下载校验失败。'}
        Move-Item -LiteralPath $partial -Destination $archive
    }
    $uvRoot=Join-Path $runtimeRoot 'uv'
    $uv=Join-Path $uvRoot 'uv.exe'
    # Windows PowerShell's Expand-Archive may fail beyond MAX_PATH and mask the
    # real error while rolling back already extracted files. Use literal Win32
    # extended paths and verify completion before running a native executable.
    Expand-WechatbotRuntimeArchive -Archive $archive -Destination $uvRoot -ExpectedSha256 $manifest.uv_sha256 -RequiredFiles @('uv.exe')
    Write-Host '检测本包Python与内置离线依赖；后续启动复用本包环境。'
    $script:taskStartupStage='核验并解压本包 Python 归档'
    $pythonArchive=Join-Path $vendorRoot $manifest.python_archive
    $pythonRoot=Join-Path $env:UV_PYTHON_INSTALL_DIR ('cpython-'+$manifest.python_request+'-windows-x86_64-none')
    if(Test-Path -LiteralPath $pythonArchive){
        Expand-WechatbotRuntimeArchive -Archive $pythonArchive -Destination $pythonRoot -ExpectedSha256 $manifest.python_archive_sha256 -RequiredFiles @('python.exe','python312.dll','DLLs/_asyncio.pyd','DLLs/_ssl.pyd','Lib/encodings/__init__.py','Lib/venv/__init__.py')
    }
    # Registering Python in HKCU or adding user-level command shims is unnecessary.
    $taskPythonArguments=@('python','install',$manifest.python_request,'--no-config','--no-bin','--no-registry')
    if($env:WECHATBOT_OFFLINE -eq '1'){$taskPythonArguments+='--offline'}
    Invoke-Checked $uv $taskPythonArguments '注册本包 managed Python'
    $taskPythonOutput=Invoke-Checked $uv @('python','find',$manifest.python_request,'--no-config') '查找本包 managed Python' -CaptureOutput
    if(-not $taskPythonOutput){throw '未找到本包Python；请检查本包Python归档。'}
    $python=($taskPythonOutput | Select-Object -Last 1).Trim()
    if(-not (Test-Path -LiteralPath $python)){throw '本包Python路径不存在。'}
    $taskPythonSmoke=@('-X','utf8','-B','-c','import sys,asyncio,ssl,ctypes,json,venv,encodings; assert sys.version_info[:3] == (3,12,12)')
    try { Invoke-Checked $python $taskPythonSmoke 'Python：标准库导入检查' } catch {
        if(-not (Test-Path -LiteralPath $pythonArchive)){throw}
        Write-Host '本包Python检查未通过，正在校验并修复缺失组件。'
        Expand-WechatbotRuntimeArchive -Archive $pythonArchive -Destination $pythonRoot -ExpectedSha256 $manifest.python_archive_sha256 -RequiredFiles @('python.exe','python312.dll','DLLs/_asyncio.pyd','DLLs/_ssl.pyd','Lib/encodings/__init__.py','Lib/venv/__init__.py') -ForceVerify
        Invoke-Checked $python $taskPythonSmoke 'Python：修复后标准库导入检查'
    }
    $env:WECHATBOT_PORTABLE_UV=$uv
    $env:WECHATBOT_PORTABLE_PYTHON=$python
    $script:taskPythonBootstrapStarted=$true
    $script:taskPythonBootstrapStartUtc=[DateTime]::UtcNow
    Invoke-Checked $python @('-X','utf8','-B',(Join-Path $PSScriptRoot 'portable_bootstrap.py'),$Action) '准备两套环境、AstrBot 实例及本包服务' -ShowStages
    exit 0
} catch {
    $taskFailure=$_
    Write-Host ('启动未完成：'+(Protect-WechatbotDiagnostic $taskFailure.Exception.Message)) -ForegroundColor Red
    try {
        $taskErrorReport=Save-WechatbotStartupError $taskFailure
        if($taskErrorReport){Write-Host ('详细错误已保存：'+$taskErrorReport)}
    } catch { }
    Write-Host '现有配置、记忆和其他程序均保留。修复上述原因后再次运行即可。'
    exit 1
}

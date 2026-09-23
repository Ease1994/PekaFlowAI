<#
构建机一键安装 / 升级（Windows）

在构建机上打开 PowerShell：

    iwr http://平台地址:8080/api/v1/agents/install-script?role=builder -OutFile install-agent.ps1
    .\install-agent.ps1 -EnrollToken <接入凭证>

  -EnrollToken  接入凭证，平台「构建机 → 新增构建机」页面复制。jar 下载和首次注册都用它，
                之后凭据落在 C:\ProgramData\release-agent\builder\enrolled\，重跑不用再带。
  -Server       平台地址。从平台下载本脚本时已自动填好，一般不用管。
  -Workspace    工作空间目录，拉代码和编译产物都放这儿，很吃磁盘。
                默认在安装目录下（C 盘），C 盘紧张时务必指到数据盘，如 -Workspace D:\rp-workspace。

装完会注册一个名为 RELEASE-Build-Agent 的计划任务：开机自动拉起，进程挂了 2 分钟内自愈。
平台发布新版 jar 后，构建机会在空闲时（没有任务在跑）自动升级，不用再挨台机器重装。
#>
[CmdletBinding()]
param(
    [string]   $Server      = '__RELEASE_SERVER__',
    [string]   $Name        = $env:COMPUTERNAME,
    [string[]] $Tags        = @(),          # 留空则由 Agent 按当前系统自动打标签
    # 环境码：prod / test / uat / staging / dev，或自定义小写码。
    # 决定这台能构建哪种环境的流水线。只在首次接入时生效，之后以页面为准
    [string]   $Env         = 'prod',
    [int]      $Concurrency = 8,
    [string]   $WorkDir     = (Join-Path $env:USERPROFILE 'release-agent'),
    [string]   $Workspace   = '',       # 留空则用安装目录下的 workspace
    [string]   $EnrollToken = $env:RELEASE_ENROLL_TOKEN
)

$ErrorActionPreference = 'Stop'
$Env = $Env.Trim().ToLower()
if ($Env -notmatch '^[a-z][a-z0-9_-]{0,15}$') {
    throw "Env 不合法「$Env」，只允许小写字母开头、最多 16 位的字母数字-_，内置：prod、test、uat、staging、dev"
}
# 平台地址由下载脚本时回填到上面的默认值。这里只判断它像不像个地址，
# 不能出现占位符字面量——回填是全文替换，写在这儿会被一起换掉
if ($Server -notmatch '^https?://') {
    $Server = Read-Host '平台地址（如 http://172.18.10.233:8080）'
}
$Server = $Server.TrimEnd('/')
if ($Server -notmatch '^https?://.+') { throw "平台地址不对：$Server" }

# 与 Agent 的 Config.credentialFile() 保持一致：判断这台机器是否已登记过
$credKey  = ($Server + '|' + $Name) -replace '[^A-Za-z0-9._-]', '_'
$credFile = Join-Path $env:USERPROFILE ".release-agent\enrolled\$credKey.token"
$enrolled = Test-Path $credFile

Write-Host "==> 构建机 $Name -> $Server"
# 找一个「真的能跑」的 java，并固化成绝对路径写进启动器。
# 不能只认 PATH：Oracle 装过又卸过的机器上 C:\ProgramData\Oracle\Java\javapath 会留下
# 一堆失效软链接，where/Get-Command 都能列出来，真去执行才报「系统找不到文件」。
# 守护进程拉起时用的又是另一份 PATH，问题只在自愈时才暴露，非常难查。
function Resolve-JavaExe {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($env:JAVA_HOME) { $cands.Add((Join-Path $env:JAVA_HOME 'bin\java.exe')) }
    Get-Command java.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $cands.Add($_.Source) }
    foreach ($root in @("$env:ProgramFiles\Java", "${env:ProgramFiles(x86)}\Java",
                        "$env:ProgramFiles\Eclipse Adoptium", "$env:ProgramFiles\Amazon Corretto",
                        "$env:ProgramFiles\Microsoft\jdk", "$env:ProgramFiles\Zulu")) {
        if (Test-Path -LiteralPath $root) {
            Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { $cands.Add((Join-Path $_.FullName 'bin\java.exe')) }
        }
    }
    foreach ($c in $cands) {
        if (-not $c -or -not (Test-Path -LiteralPath $c -PathType Leaf)) { continue }
        # 必须真跑一次，失效软链接只有这样才露馅。套一层 cmd 是因为 java -version
        # 走的是 stderr，本脚本又是 ErrorActionPreference='Stop'，直接跑会被当成
        # 终止错误抛出，把所有候选都误判成不可用
        & cmd /c "`"$c`" -version >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    return $null
}

$javaExe = Resolve-JavaExe
if (-not $javaExe) {
    throw '没找到可用的 java，请安装 JDK 8 或以上。（若已装过又卸载，PATH 里可能残留 C:\ProgramData\Oracle\Java\javapath，需要清掉）'
}
# javaw 不带控制台窗口，和 java.exe 总在同一个 bin 下；没有就退回 java.exe
$javawExe = Join-Path (Split-Path -Parent $javaExe) 'javaw.exe'
if (-not (Test-Path -LiteralPath $javawExe -PathType Leaf)) { $javawExe = $javaExe }
Write-Host "    java: $javaExe"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Set-Location -LiteralPath $WorkDir

Write-Host '[1/5] 停掉旧 agent'
# 先停守护，否则我们刚杀掉旧进程它就用旧配置给拉回来了
Disable-ScheduledTask -TaskName 'RELEASE-Build-Agent' -ErrorAction SilentlyContinue | Out-Null
Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe'" |
    Where-Object { $_.CommandLine -like '*deploy-agent*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

Write-Host '[2/5] 下载 jar'
# 接入凭证：下载 jar 和首次注册都用它。已登记过的机器本地有凭据，
# 但下载 jar 仍需要凭证，所以这里统一问一次
if (-not $EnrollToken) {
    $hint = if ($enrolled) { '（本机已登记过，凭证仅用于下载 jar）' } else { '（首次安装必填）' }
    $EnrollToken = Read-Host "接入凭证，平台「构建机」页面复制 $hint"
}
if (-not $EnrollToken) { throw '没有接入凭证，无法下载 jar' }

try {
    Invoke-WebRequest -Uri "$Server/api/v1/agents/download" `
        -Headers @{ 'X-Enroll-Token' = $EnrollToken } -OutFile deploy-agent.jar
} catch {
    throw "下载 jar 失败：$($_.Exception.Message)。请确认平台地址可达、接入凭证正确（凭证轮换后要用新的）"
}
$expect = (Invoke-WebRequest -Uri "$Server/api/v1/agents/jar-sha256" `
    -Headers @{ 'X-Enroll-Token' = $EnrollToken }).Content.Trim()
if (-not $expect) { throw '拿不到平台 jar 指纹，拒绝使用刚下的包' }
$got = (Get-FileHash -LiteralPath deploy-agent.jar -Algorithm SHA256).Hash.ToLower()
if ($got -ne $expect.ToLower()) {
    Remove-Item deploy-agent.jar -Force -ErrorAction SilentlyContinue
    throw "jar 指纹不一致（本地 $got，平台 $expect），已删除，请重试安装"
}
Write-Host ("      {0:N0} KB  deploy-agent.jar" -f ((Get-Item deploy-agent.jar).Length / 1KB))
# 清掉上次留下的换包文件和卸载标记。重装就是再装一次：
# 留着 .new 会把刚下的 jar 顶掉；留着 uninstall.requested 会让守护一启动又把 Agent 卸掉。
Remove-Item deploy-agent.jar.new, deploy-agent.jar.bak, deploy-agent.upgrade-attempt, uninstall.requested `
    -Force -ErrorAction SilentlyContinue

Write-Host '[3/5] 生成启动器与守护脚本'
# --home 必须显式指定：守护进程拉起时的账户可能和现在不同，user.home 一变
# 登记凭据就找不着了，Agent 会退化成每次都要接入凭证
$agentHome = 'C:\ProgramData\release-agent\builder'
New-Item -ItemType Directory -Force -Path $agentHome | Out-Null
# --role builder 显式写出来，守护脚本靠它认自己的进程（节点用 --role node）
$agentArgs = "-jar deploy-agent.jar --server $Server --name `"$Name`" --role builder --env $Env" `
    + " --home `"$agentHome`" --concurrency $Concurrency"
if ($Tags.Count -gt 0) { $agentArgs += ' --tags ' + ($Tags -join ',') }

# 工作空间放代码和编译产物，是磁盘占用的大头。默认跟着安装目录走（多半在 C 盘），
# 装在系统盘紧张的机器上迟早撑爆，所以支持指到数据盘
if ($Workspace) {
    New-Item -ItemType Directory -Force -Path $Workspace | Out-Null
    $Workspace = (Resolve-Path -LiteralPath $Workspace).Path
    $agentArgs += " --workspace `"$Workspace`""
} else {
    $Workspace = Join-Path $WorkDir 'workspace'
}
$wsDrive = (Split-Path -Qualifier $Workspace) + '\'
try {
    $free = (Get-PSDrive -Name $wsDrive[0] -ErrorAction Stop).Free / 1GB
    Write-Host ("      工作空间 {0}（{1} 剩余 {2:N1} GB）" -f $Workspace, $wsDrive, $free)
    if ($free -lt 20) {
        Write-Warning "工作空间所在盘剩余不足 20 GB，建议重跑并加 -Workspace D:\rp-workspace 指到数据盘"
    }
} catch {
    Write-Host "      工作空间 $Workspace"
}

if ($enrolled) {
    Write-Host '      本机已登记过，Agent 会用本地凭据续期'
}
# 走环境变量传给子进程，凭证就不会落进 start-agent.cmd
$env:RELEASE_ENROLL_TOKEN = $EnrollToken

# 启动器：注释保持纯 ASCII —— cmd 按控制台代码页解析，中文注释里的字节会被当成管道符；
# 机器名可能含中文，所以按 OEM（控制台代码页）落盘，名字本身在引号里是安全的
$launcher = Join-Path $WorkDir 'start-agent.cmd'
@"
@echo off
cd /d "%~dp0"
rem javaw detaches from the console, so closing the window won't kill the agent.
rem The absolute path is baked in at install time on purpose: resolving it via PATH
rem breaks under the scheduled-task account and on boxes with stale Oracle javapath links.
start "release-agent" /b "$javawExe" $agentArgs >> agent.log 2>&1
"@ | Set-Content -Path $launcher -Encoding OEM

# 守护脚本：计划任务反复调用它，所以必须幂等。它还负责升级时的换包和日志滚动
$watchdog = Join-Path $WorkDir 'watchdog.ps1'
@'
# 由安装脚本生成，重跑安装脚本会覆盖。计划任务每隔几分钟调一次，必须幂等。
# 这里刻意不用 ErrorActionPreference='Stop'：守护自己中断了，Agent 就再没人拉起来，
# 而且整个过程是静默的，事后完全查不出原因。所以逐步容错，关键动作都记进 watchdog.log
$ErrorActionPreference = 'Continue'
$work = '__WORKDIR__'
$jar  = Join-Path $work 'deploy-agent.jar'
$new  = Join-Path $work 'deploy-agent.jar.new'
$bak  = Join-Path $work 'deploy-agent.jar.bak'
$wlog = Join-Path $work 'watchdog.log'
Set-Location -LiteralPath $work

# 平台下发卸载后 jar 会写下这个标记。看到就不再拉起，并删掉本计划任务。
if (Test-Path -LiteralPath (Join-Path $work 'uninstall.requested')) {
    try {
        '{0}  收到卸载标记，不再拉起' -f (Get-Date -Format 'MM-dd HH:mm:ss') |
            Add-Content -LiteralPath $wlog -Encoding UTF8
    } catch { }
    try { schtasks.exe /Delete /TN 'RELEASE-Build-Agent' /F | Out-Null } catch { }
    exit 0
}

function Note($m) {
    try {
        '{0}  {1}' -f (Get-Date -Format 'MM-dd HH:mm:ss'), $m | Add-Content -LiteralPath $wlog -Encoding UTF8
    } catch { }
}

# 返回 $true 在跑 / $false 确认不在 / $null 查不出来（这时候不能贸然再拉一个）
function Running {
    try {
        $hit = @(Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe'" -ErrorAction Stop |
                 Where-Object { $_.CommandLine -like '__MARKER__' })
        $hit.Count -gt 0
    } catch {
        Note ('查进程失败：' + $_.Exception.Message)
        $null
    }
}

foreach ($pair in @(@($wlog, 2MB), @((Join-Path $work '__LOGNAME__'), 20MB))) {
    try {
        if ((Test-Path -LiteralPath $pair[0]) -and ((Get-Item -LiteralPath $pair[0]).Length -gt $pair[1])) {
            Move-Item -LiteralPath $pair[0] -Destination ($pair[0] + '.1') -Force
        }
    } catch { }
}

# Agent 自升级只把新包放到 .new，真正的替换得在 JVM 起来之前做：
# Windows 上正在运行的 jar 是锁住的，覆盖不了。
# 所以先确认 Agent 确实不在跑：它还在的时候动手必然失败，而失败分支会把暂存包丢掉，
# 等于白下载一次，下轮心跳再来一遍
$alive = Running
if ((Test-Path -LiteralPath $new) -and ($alive -eq $false)) {
    $swapped = $false
    for ($i = 0; $i -lt 15; $i++) {
        try {
            Copy-Item -LiteralPath $jar -Destination $bak -Force   # 新版起不来时靠它回退
            Move-Item -LiteralPath $new -Destination $jar -Force
            $swapped = $true
            break
        } catch {
            Start-Sleep -Seconds 2   # 旧进程可能还没完全退出，等它松手
        }
    }
    if ($swapped) {
        Note '已换上新 jar，待启动验证'
    } else {
        # 留着它的话，Agent 起来后每轮心跳都会重新下载再退出，来回折腾
        Note '换 jar 失败，丢弃暂存包，继续用当前版本'
        Remove-Item -LiteralPath $new -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
    }
}

if ($alive -ne $false) { exit 0 }

Note 'Agent 不在，拉起'
Start-Process -FilePath (Join-Path $work '__LAUNCHER__') -WindowStyle Hidden

# 每次拉起都确认结果。只记「拉起」不记结果的话，反复拉起反复失败在日志里
# 看着和正常自愈一模一样，排查时只能靠猜
Start-Sleep -Seconds 25
if ((Running) -eq $true) {
    Note '已起来'
    Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue
} else {
    Note ('拉起后进程仍不在，原因看 __LOGNAME__ 末尾')
    # 刚换过版本就得回退：不然这台机器会一直离线，而旧 jar 已被覆盖，只能人工上机器救
    if (Test-Path -LiteralPath $bak) {
        Note '疑似新版本起不来，回退到升级前的 jar'
        try {
            Move-Item -LiteralPath $bak -Destination $jar -Force
            Start-Process -FilePath (Join-Path $work '__LAUNCHER__') -WindowStyle Hidden
        } catch {
            Note ('回退失败：' + $_.Exception.Message)
        }
    }
}
'@.Replace('__WORKDIR__', $WorkDir).
   Replace('__LOGNAME__', 'agent.log').
   Replace('__MARKER__', '*deploy-agent*--role builder*').
   Replace('__LAUNCHER__', 'start-agent.cmd') |
    Set-Content -Path $watchdog -Encoding UTF8

Write-Host '[4/5] 注册守护（开机自启 + 掉线自愈）'
# 用系统自带的计划任务，不引入 nssm 之类的额外二进制。
# 构建机优先用当前账户跑（S4U 不需要存密码）：编译工具链常依赖用户级的 PATH、
# npm/nuget 缓存和 git 配置，换成 SYSTEM 容易出现"手动能编、自动编不了"。
$taskName = 'RELEASE-Build-Agent'
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdog`""
    $trigBoot   = New-ScheduledTaskTrigger -AtStartup
    $trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    try {
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigBoot, $trigRepeat `
            -Principal $principal -Settings $settings -Force | Out-Null
        Write-Host "      已注册计划任务 $taskName（以 $me 身份，开机自启，每 2 分钟检查一次）"
        $registered = $true
    } catch {
        # S4U 需要账户有"作为批处理作业登录"权限，域策略收紧时会失败，退回 SYSTEM
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigBoot, $trigRepeat `
            -Principal $principal -Settings $settings -Force | Out-Null
        Write-Warning "以 $me 身份注册失败，已退回 SYSTEM 身份。若构建依赖用户级安装的工具，可能需要改成系统级安装"
        $registered = $true
    }
    # 上面为了换 jar 先 Disable 过，注册未必会把它带回启用态；不显式打开的话
    # 守护一次都不会跑，Agent 挂了也没人管，而且表面上看安装是成功的
    if ($registered) {
        Enable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
        if ((Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State -eq 'Disabled') {
            Write-Warning "计划任务 $taskName 处于禁用状态，请手动启用，否则掉线不会自愈"
        }
    }
} catch {
    Write-Warning "注册计划任务失败：$($_.Exception.Message)"
    Write-Warning "Agent 仍会启动，但机器重启后不会自动拉起。手动拉起：$launcher"
}

Write-Host '[5/5] 启动并检查'
# 这一次由安装脚本直接拉起：环境变量里有接入凭证，首次登记要靠它。
# 登记凭据落盘后，之后计划任务拉起就不需要凭证了
Start-Process -FilePath $launcher -WindowStyle Hidden
Start-Sleep -Seconds 5

$alive = Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe'" |
         Where-Object { $_.CommandLine -like '*deploy-agent*' }
if ($alive) {
    Write-Host "启动成功。日志：$WorkDir\agent.log"
    Write-Host ''
    Write-Host '以后这台机器不用再管：重启会自动拉起，进程挂了 2 分钟内自愈，'
    Write-Host 'jar 有新版本时会在空闲时自动升级。'
    Get-Content agent.log -Tail 5 -ErrorAction SilentlyContinue
} else {
    Write-Host '启动失败，日志末尾：'
    Get-Content agent.log -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}

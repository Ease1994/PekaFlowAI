<#
部署节点一键安装 / 升级（Windows Server 2012 R2 及以上）

在节点服务器上，以管理员身份打开 PowerShell：

    iwr http://平台地址:8080/api/v1/agents/install-script?role=node -OutFile install-node.ps1
    .\install-node.ps1 -AllowPaths D:\wwwroot\o2o -EnrollToken <接入凭证>

节点跑在生产服务器上，只执行「发送文件到节点」和「IIS 启停控制」两类步骤：
不编译、不拉代码、不下载插件、不执行任意脚本。编译打包请装构建机（install-agent.ps1）。

必须以管理员身份运行：操作 IIS 应用池需要管理员权限，否则 appcmd 会报「拒绝访问」。

  -AllowPaths   必填。节点允许写入的目录，多个用逗号分隔。这是这台机器的安全边界：
                任何落在它之外的写操作节点都会拒绝。只填站点根目录，别填盘符。
                装完以后要加目录，在平台「节点管理」页改即可，心跳下发，不必重装。
  -AllowIis     可选。老机器需要按名字再收紧时才填应用池/站点。
                IIS 启停默认看站点物理路径是否落在 -AllowPaths 内；查不到路径则失败。
                不填不表示拒绝全部，只是不再额外限制名字。
  -EnrollToken  接入凭证，平台「节点管理 → 新增节点」页面复制。jar 下载和首次注册都用它，
                之后凭据落在 C:\ProgramData\release-agent\node\enrolled\，重跑不用再带。
  -Server       平台地址。从平台下载本脚本时已自动填好，一般不用管。
  -BackupRoot   备份根。留空则用站点盘上的 release-backup（D:\wwwroot\o2o → D:\release-backup）。
                必须在 -AllowPaths 之外，否则发布会被拒绝。

装完会注册一个名为 RELEASE-Node-Agent 的计划任务：开机自动拉起，进程挂了 2 分钟内自愈。
以后升级 jar 不用再上这台机器，在平台「节点管理」页点「升级」即可——节点不会自作主张
升级，只有你点了才动，而且会等当前发布任务跑完再换版本。
#>
[CmdletBinding()]
param(
    [string]   $Server      = '__RELEASE_SERVER__',
    [string]   $Name        = $env:COMPUTERNAME,
    [Parameter(Mandatory = $true)]
    [string[]] $AllowPaths,
    # 允许控制的 IIS 应用池/站点。可留空：启停按物理路径是否在 AllowPaths 内判断。
    [string[]] $AllowIis = @(),
    # 环境码：prod / test / uat / staging / dev，或自定义小写码。
    # 决定往这台机器下发文件要不要审批（test/dev 免审，其余要审）。
    # 只在平台首次见到这台机器时生效，之后以页面上的设置为准。非法码直接退出。
    [string]   $Env         = 'prod',
    [string]   $WorkDir     = (Join-Path $env:USERPROFILE 'rp-node'),
    [string]   $EnrollToken = $env:RELEASE_ENROLL_TOKEN,
    # 备份根。留空则按第一个允许目录所在盘建 <盘符>\release-backup，
    # 例如 D:\wwwroot\o2o → D:\release-backup。必须在站点目录之外。
    [string]   $BackupRoot  = ''
)

$ErrorActionPreference = 'Stop'
if ($Name -match '[\s"$`;|&<>\\/]' -or $Name.Length -eq 0 -or $Name.Length -gt 64) {
    throw "Name 不能含空格或分号、引号这类会拆开命令的字符（收到「$Name」）"
}
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

Write-Host "==> 部署节点 $Name -> $Server"

# 管理员权限：不是可选项。装完才发现停不了应用池，等于白装
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw '请以管理员身份运行：右键 PowerShell → 以管理员身份运行。节点需要管理员权限才能启停 IIS 应用池'
}

# 找一个「真的能跑」的 java，并固化成绝对路径写进启动器。
# 不能只认 PATH：Oracle 装过又卸过的机器上 C:\ProgramData\Oracle\Java\javapath 会留下
# 一堆失效软链接，where/Get-Command 都能列出来，真去执行才报「系统找不到文件」。
# 守护进程以 SYSTEM 身份拉起时用的又是另一份 PATH，问题只在自愈时才暴露，非常难查。
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
    throw '没找到可用的 java，请安装 JRE 8 或以上。（若已装过又卸载，PATH 里可能残留 C:\ProgramData\Oracle\Java\javapath，需要清掉）'
}
# javaw 不带控制台窗口，和 java.exe 总在同一个 bin 下；没有就退回 java.exe
$javawExe = Join-Path (Split-Path -Parent $javaExe) 'javaw.exe'
if (-not (Test-Path -LiteralPath $javawExe -PathType Leaf)) { $javawExe = $javaExe }
# 打出来：机器上装了多套 JRE 或 PATH 里有失效残留时，这一行能省掉大量排查
Write-Host "    java: $javaExe"

# 校验白名单目录：写错一个字，发布时才报错就太晚了
$resolved = @()
foreach ($p in $AllowPaths) {
    $trimmed = $p.Trim()
    if (-not $trimmed) { continue }
    if (-not (Test-Path -LiteralPath $trimmed)) {
        throw "目录不存在：$trimmed（请先确认站点根目录路径，或手动创建）"
    }
    $full = (Resolve-Path -LiteralPath $trimmed).Path.TrimEnd('\')
    # 盘符根目录等于把整台机器交出去，白名单就失去意义了
    if ($full -match '^[A-Za-z]:$') {
        throw "不能把整个盘符 $full 设为允许目录，请填具体的站点根目录"
    }
    $resolved += $full
}
if ($resolved.Count -eq 0) { throw '-AllowPaths 不能为空' }

function Test-BackupRootSafe {
    param([string]$Bak, [string[]]$Allows)
    $b = $Bak.TrimEnd('\')
    if ($b -match '^[A-Za-z]:$') {
        throw "备份根不能是盘符根 $Bak"
    }
    foreach ($a in $Allows) {
        $aa = $a.TrimEnd('\').ToLowerInvariant()
        $ba = $b.ToLowerInvariant()
        if ($ba -eq $aa -or $ba.StartsWith($aa + '\')) {
            throw "备份根 $Bak 落在允许目录 $a 里面"
        }
        if ($aa.StartsWith($ba + '\')) {
            throw "备份根 $Bak 包住了允许目录 $a"
        }
    }
}

# 定备份根并交给 Agent --backup-root。不配的话流水线 backupDir 留空时
# 没有第二道闸，步骤可以指到任意非系统目录。
function Resolve-NodeBackupRoot {
    param([string]$Explicit, [string[]]$Allows)
    if ($Explicit -and $Explicit.Trim()) {
        $b = $Explicit.Trim().TrimEnd('\')
        Test-BackupRootSafe -Bak $b -Allows $Allows
        return $b
    }
    $drive = Split-Path -Qualifier $Allows[0]
    $candidate = $drive.TrimEnd('\') + '\release-backup'
    try {
        Test-BackupRootSafe -Bak $candidate -Allows $Allows
        return $candidate
    } catch {
        $fallback = 'C:\ProgramData\release-backup'
        Test-BackupRootSafe -Bak $fallback -Allows $Allows
        return $fallback
    }
}

$backupRoot = Resolve-NodeBackupRoot -Explicit $BackupRoot -Allows $resolved
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
Write-Host "     备份目录：$backupRoot"

Write-Host '     允许写入的目录：'
$resolved | ForEach-Object { Write-Host "       $_" }

if (-not (Test-Path 'C:\Windows\System32\inetsrv\appcmd.exe')) {
    Write-Warning '本机没找到 appcmd.exe（未安装 IIS）。发文件可用，但「IIS 启停控制」步骤会失败'
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
Set-Location -LiteralPath $WorkDir

Write-Host '[1/5] 停掉旧节点 agent'
# 先停守护，否则我们刚杀掉旧进程它就用旧配置给拉回来了
Disable-ScheduledTask -TaskName 'RELEASE-Node-Agent' -ErrorAction SilentlyContinue | Out-Null
Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe'" |
    Where-Object { $_.CommandLine -like '*deploy-agent*' -and $_.CommandLine -like '*--role node*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

Write-Host '[2/5] 下载 jar'
# 接入凭证：下载 jar 和首次注册都用它。已登记过的机器（升级场景）本地有凭据，
# 但下载 jar 仍需要凭证，所以这里统一问一次
if (-not $EnrollToken) {
    $hint = if ($enrolled) { '（本机已登记过，凭证仅用于下载 jar）' } else { '（首次安装必填）' }
    $EnrollToken = Read-Host "接入凭证，平台「节点管理」页面复制 $hint"
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
# 节点的 allow-paths 用分号分隔（和 Windows 的 PATH 习惯一致）
$allowArg = $resolved -join ';'
# --home 必须显式指定：守护进程以 SYSTEM 身份拉起时 user.home 会变成另一个目录，
# 登记凭据就找不着了，Agent 会退化成每次都要接入凭证
$agentHome = 'C:\ProgramData\release-agent\node'
New-Item -ItemType Directory -Force -Path $agentHome | Out-Null
$agentArgs = "-jar deploy-agent.jar --server $Server --name `"$Name`" --role node --env $Env" `
    + " --home `"$agentHome`" --allow-paths `"$allowArg`" --backup-root `"$backupRoot`""
$iisItems = @()
foreach ($item in @($AllowIis)) {
    if ($item -and $item.Trim()) { $iisItems += $item.Trim() }
}
if ($iisItems.Count -gt 0) {
    $iisArg = $iisItems -join ';'
    $agentArgs += " --allow-iis `"$iisArg`""
}

if ($enrolled) {
    Write-Host '      本机已登记过，Agent 会用本地凭据续期'
}
# 走环境变量传给子进程，凭证就不会落进 start-node.cmd
$env:RELEASE_ENROLL_TOKEN = $EnrollToken

# 启动器：注释保持纯 ASCII —— cmd 按控制台代码页解析，中文注释里的字节会被当成管道符；
# 机器名和路径可能含中文，所以按 OEM（控制台代码页）落盘，它们本身在引号里是安全的
$launcher = Join-Path $WorkDir 'start-node.cmd'
@"
@echo off
cd /d "%~dp0"
rem javaw detaches from the console, so closing the window won't kill the agent.
rem The absolute path is baked in at install time on purpose: resolving it via PATH
rem breaks under the SYSTEM account and on boxes with stale Oracle javapath links.
rem Cap heap so a runaway unzip cannot starve IIS on the same box.
start "rp-node" /b /belownormal "$javawExe" -Xms64m -Xmx256m $agentArgs >> node-agent.log 2>&1
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
    try { schtasks.exe /Delete /TN 'RELEASE-Node-Agent' /F | Out-Null } catch { }
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

# 生产机上日志不能无限涨。被占用时滚不动也无所谓，下一轮再说
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
   Replace('__LOGNAME__', 'node-agent.log').
   Replace('__MARKER__', '*deploy-agent*--role node*').
   Replace('__LAUNCHER__', 'start-node.cmd') |
    Set-Content -Path $watchdog -Encoding UTF8

Write-Host '[4/5] 注册守护（开机自启 + 掉线自愈）'
# 用系统自带的计划任务，不引入 nssm 之类的额外二进制：生产机上装的东西越少越好。
# SYSTEM 身份运行，权限足够启停 IIS，也不依赖任何人登录。
$taskName = 'RELEASE-Node-Agent'
try {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdog`""
    # 开机拉起 + 每 2 分钟兜一次底（进程被杀、OOM 退出都能自动回来）
    $trigBoot   = New-ScheduledTaskTrigger -AtStartup
    $trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigBoot, $trigRepeat `
        -Principal $principal -Settings $settings -Force | Out-Null
    # 上面为了换 jar 先 Disable 过，注册未必会把它带回启用态；不显式打开的话
    # 守护一次都不会跑，Agent 挂了也没人管，而且表面上看安装是成功的
    Enable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
    $state = (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
    if ($state -eq 'Disabled') {
        Write-Warning "计划任务 $taskName 处于禁用状态，请手动启用，否则掉线不会自愈"
    } else {
        Write-Host "      已注册计划任务 $taskName（开机自启，每 2 分钟检查一次）"
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
         Where-Object { $_.CommandLine -like '*deploy-agent*' -and $_.CommandLine -like '*--role node*' }
if ($alive) {
    Write-Host "启动成功。日志：$WorkDir\node-agent.log"
    Write-Host "到平台「节点管理」页刷新，应该能看到 $Name 上线"
    Write-Host ''
    Write-Host '以后这台机器不用再管：重启会自动拉起，进程挂了 2 分钟内自愈，'
    Write-Host '升级 jar 在平台「节点管理」页点「升级」即可，不用再上机器。'
    Get-Content node-agent.log -Tail 5 -ErrorAction SilentlyContinue
} else {
    Write-Host '启动失败，日志末尾：'
    Get-Content node-agent.log -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}

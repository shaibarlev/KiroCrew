<#
.SYNOPSIS
    Diagnose and recover a KiroCrew desktop/gateway that will not start after an
    in-place version upgrade on Windows.

.DESCRIPTION
    The failure this repairs
    ------------------------
    An in-place upgrade of the KiroCrew desktop app replaces the installed bytes
    but does NOT stop the gateway process the previous version left running. That
    old gateway keeps the process-lifetime advisory lock on
    ``<KIROCREW_HOME>\gateway.lock`` (see kiro_crew.gateway_lock.GatewayLock) and
    keeps its dashboard port bound. The freshly-installed gateway then cannot take
    the singleton lock, never (re)starts the MCP gateway daemon, and every pooled
    MCP stub connect fails with:

        connect failed: [WinError 2] The system cannot find the file specified

    logged to ``<KIROCREW_HOME>\logs\stub_fallback.jsonl`` (no
    ``\\.\pipe\kirocrew-mcp-*`` named pipe is ever created). To the user the app
    "does not start" / has no working tools.

    Because the orphaned gateway is often still answering HTTP, the backend's own
    port-reclaim (kiro_crew.dashboard.port_reclaim) classifies it as a HEALTHY_PEER
    and refuses to reclaim — so a plain relaunch does not fix it. This script closes
    that gap for the operator: it identifies the gateway that owns the data home,
    distinguishes the current install from a superseded one, and (with -Fix) stops
    the superseded gateway tree and clears the stale lock/pid/run state so the next
    launch is clean.

    Safety
    ------
    * Read-only by default. Pass -Fix to make changes.
    * State files are copied to ``<KIROCREW_HOME>\backups\gateway-recovery-<ts>``
      before anything is removed, so a fix is reversible.
    * The active gateway is identified by the bundled backend python under the
      install dir plus the dashboard port it holds; the whole process TREE is
      stopped (taskkill /T) so its kiro-cli / MCP children do not orphan.
    * This session's own kiro-cli (and any gateway that both answers HTTP AND is
      NOT superseded by a newer install) is left alone unless -Force is given.

.PARAMETER KiroCrewHome
    The data home. Defaults to $env:KIROCREW_HOME, else ~\.kiro\crew.

.PARAMETER Fix
    Perform the recovery. Without it the script only reports a diagnosis.

.PARAMETER Force
    Also stop a gateway that is currently answering HTTP. Use only when you have
    confirmed it is the superseded (old-version) instance.

.PARAMETER Relaunch
    After a successful -Fix, launch the installed KiroCrew desktop app.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\recover-gateway.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\recover-gateway.ps1 -Fix -Relaunch
#>
[CmdletBinding()]
param(
    [string]$KiroCrewHome = "",
    [switch]$Fix,
    [switch]$Force,
    [switch]$Relaunch
)

$ErrorActionPreference = "Stop"

function Write-Head($m) { Write-Host ""; Write-Host "== $m ==" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Write-Info($m) { Write-Host "  ->     $m" -ForegroundColor Gray }
function Write-Bad($m)  { Write-Host "  [!]    $m" -ForegroundColor Red }

# --- Resolve the data home -------------------------------------------------
if (-not $KiroCrewHome) {
    $KiroCrewHome = if ($env:KIROCREW_HOME) { $env:KIROCREW_HOME } else { Join-Path $env:USERPROFILE ".kiro\crew" }
}
if (-not (Test-Path $KiroCrewHome)) {
    Write-Bad "KIROCREW_HOME not found: $KiroCrewHome"
    exit 1
}
Write-Head "KiroCrew gateway recovery"
Write-Info "Data home: $KiroCrewHome"
Write-Info ("Mode: {0}{1}" -f ($(if ($Fix) { 'FIX' } else { 'diagnose-only' })), ($(if ($Force) { ' +force' } else { '' })))

$runDir     = Join-Path $KiroCrewHome "run"
$lockFile   = Join-Path $KiroCrewHome "gateway.lock"
$stubLog    = Join-Path $KiroCrewHome "logs\stub_fallback.jsonl"

# --- 1. Is a gateway lock present, and who holds the port? -----------------
Write-Head "1. Locating the gateway that owns this home"

# The run/gateway-<port>.pid sidecars name the port(s) a gateway advertised and
# the pid it recorded (kiro_crew.instances.run_marker). Read them to find the port.
$ports = @()
if (Test-Path $runDir) {
    Get-ChildItem $runDir -Filter "gateway-*.pid" -File -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.BaseName -match '^gateway-(\d+)$') { $ports += [int]$Matches[1] }
    }
}
if (-not $ports) { Write-Info "No run/gateway-<port>.pid markers; probing common port 5476."; $ports = @(5476) }

# Build a port -> owning-pid map ONCE from netstat -ano (fast), rather than a
# per-port Get-NetTCPConnection call (which measured ~18s each on a busy host).
$script:ListenMap = @{}
try {
    foreach ($line in (netstat -ano -p TCP 2>$null)) {
        # e.g. "  TCP    127.0.0.1:5476    0.0.0.0:0    LISTENING    30212"
        if ($line -match '^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$') {
            $lport = [int]$Matches[1]
            if (-not $script:ListenMap.ContainsKey($lport)) { $script:ListenMap[$lport] = [int]$Matches[2] }
        }
    }
} catch {}

function Get-PortOwnerPid([int]$port) {
    if ($script:ListenMap.ContainsKey($port)) { return $script:ListenMap[$port] }
    return 0
}

function Test-HttpAlive([int]$port) {
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 5
        return $true
    } catch {
        # A returned HTTP status (even 4xx/5xx) still proves an HTTP server answered.
        return ($null -ne $_.Exception.Response)
    }
}

# The installed desktop app dir; a gateway whose backend python lives OUTSIDE the
# current install dir is a superseded generation left by an upgrade.
$installExe = Get-Command KiroCrew.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
$installDir = if ($installExe) { Split-Path -Parent $installExe } else { "" }
if ($installDir) { Write-Info "Installed app dir: $installDir" } else { Write-Warn "KiroCrew.exe not on PATH; cannot compare install generations." }

# Resolve a single pid's image path with a bounded wait. Queried per-candidate
# (normally one or two listeners of interest), never for every process on the
# host — a full Win32_Process / Get-Process enumeration measured 12-17s on a busy
# machine. Even a single WMI/.Path lookup can stall for many seconds under load,
# so it runs in a job capped at $TimeoutSec; on timeout we fall back to the
# name-only 'tasklist' (fast) so the caller still gets 'is this a bundled python'.
function Get-ProcImagePath([int]$procId, [int]$TimeoutSec = 8) {
    $job = Start-Job -ScriptBlock {
        param($id)
        try {
            $p = Get-Process -Id $id -ErrorAction SilentlyContinue
            if ($p -and $p.Path) { return $p.Path }
        } catch {}
        try { return (Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue).ExecutablePath } catch {}
        return ""
    } -ArgumentList $procId
    if (Wait-Job $job -Timeout $TimeoutSec) {
        $img = Receive-Job $job
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        if ($img) { return [string]$img }
    } else {
        Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
    # Fallback: name only (no full path). Enough to see it is a python backend.
    try {
        $csv = tasklist /FI "PID eq $procId" /FO CSV /NH 2>$null
        if ($csv -and $csv -match '^"([^"]+)"') { return $Matches[1] }
    } catch {}
    return ""
}

$targets = @()   # gateways we may stop
$healthy = @()   # gateways answering HTTP
foreach ($port in ($ports | Sort-Object -Unique)) {
    $ownerPid = Get-PortOwnerPid $port
    if (-not $ownerPid) { Write-Info "Port ${port}: no listener."; continue }
    $img = Get-ProcImagePath $ownerPid
    $alive = Test-HttpAlive $port
    $isBundled = $img -and ($img -match 'kirocrew-backend\\python\.exe$')
    $fromCurrentInstall = $installDir -and $img -and ($img.StartsWith($installDir, [System.StringComparison]::OrdinalIgnoreCase))
    Write-Info ("Port {0}: pid {1} http={2} bundled={3} current-install={4}" -f $port, $ownerPid, $alive, $isBundled, $fromCurrentInstall)
    Write-Info ("            image: {0}" -f ($img)) 
    if ($alive) { $healthy += [pscustomobject]@{ Port=$port; Pid=$ownerPid; Image=$img; CurrentInstall=$fromCurrentInstall } }
    if ($isBundled -and -not $fromCurrentInstall) {
        # A bundled gateway that is NOT from the current install == superseded by upgrade.
        $targets += [pscustomobject]@{ Port=$port; Pid=$ownerPid; Image=$img; Reason="superseded install"; Http=$alive }
    } elseif ($isBundled -and $alive -and $Force) {
        $targets += [pscustomobject]@{ Port=$port; Pid=$ownerPid; Image=$img; Reason="forced"; Http=$alive }
    }
}

# --- 2. Symptom check: MCP stub fallbacks ----------------------------------
Write-Head "2. MCP stub-connect health"
if (Test-Path $stubLog) {
    $recent = Get-Content $stubLog -Tail 20 -ErrorAction SilentlyContinue |
        ForEach-Object { try { $_ | ConvertFrom-Json } catch {} } |
        Where-Object { $_ -and $_.reason -match 'WinError 2|connect failed' }
    if ($recent) {
        $last = $recent | Select-Object -Last 1
        Write-Warn ("Recent MCP connect failures present (last: {0} server={1})." -f $last.ts_iso, $last.server)
        Write-Info "This is the 'WinError 2 / pipe not found' symptom the recovery repairs."
    } else {
        Write-Ok "No recent WinError-2 stub failures in the tail."
    }
} else {
    Write-Info "No stub_fallback.jsonl yet."
}

# Are any kirocrew MCP pipes live? (proof the mcp-gatewayd came up)
$pipes = @([System.IO.Directory]::GetFiles("\\.\pipe\") | Where-Object { $_ -match 'kirocrew-mcp-' })
if ($pipes.Count) { Write-Ok "$($pipes.Count) kirocrew MCP named pipe(s) present." } else { Write-Warn "No \\.\pipe\kirocrew-mcp-* pipe present (daemon not serving)." }

# --- 3. Verdict ------------------------------------------------------------
Write-Head "3. Verdict"
if (-not $targets) {
    if ($healthy.Count -and ($healthy | Where-Object { $_.CurrentInstall })) {
        Write-Ok "A gateway from the current install is serving. Nothing superseded to reclaim."
    } else {
        Write-Info "No superseded gateway detected. If the app still won't start, re-run with -Force after confirming no wanted gateway is running, or check gateway.log."
    }
    if (-not $Force) { if ($Relaunch -and $Fix) { } else { exit 0 } }
}

$targets | ForEach-Object { Write-Bad ("Will stop: port {0} pid {1} ({2}, http={3})" -f $_.Port, $_.Pid, $_.Reason, $_.Http) }

if (-not $Fix) {
    Write-Host ""
    Write-Info "Diagnose-only. Re-run with -Fix (add -Relaunch to restart the app) to apply."
    exit 0
}

# --- 4. Backup state before changing anything ------------------------------
Write-Head "4. Backing up state (reversible)"
$backup = Join-Path $KiroCrewHome ("backups\gateway-recovery-" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Force -Path $backup | Out-Null
$stateFiles = @("gateway.lock","kiro_pids.txt","kiro_session_pids.txt","open_slots.json","app_backends.pids.json","mcp-gateway\gateway.sock.lock")
foreach ($f in $stateFiles) {
    $src = Join-Path $KiroCrewHome $f
    if (Test-Path $src) { try { Copy-Item $src $backup -Force -ErrorAction Stop } catch { Write-Info "skip backup of $f (locked): $($_.Exception.Message)" } }
}
if (Test-Path $runDir) { Copy-Item $runDir (Join-Path $backup "run") -Recurse -Force -ErrorAction SilentlyContinue }
Write-Ok "Backup: $backup"

# --- 5. Quit the desktop app so its supervisor does not fight recovery -----
Write-Head "5. Quitting the desktop app"
$desktop = Get-Process KiroCrew -ErrorAction SilentlyContinue
if ($desktop) {
    foreach ($d in $desktop) { $null = $d.CloseMainWindow() }
    Start-Sleep -Seconds 3
    $still = Get-Process KiroCrew -ErrorAction SilentlyContinue
    if ($still) { $still | Stop-Process -Force -ErrorAction SilentlyContinue }
    Write-Ok "Desktop app closed."
} else { Write-Info "Desktop app not running." }

# --- 6. Stop the superseded gateway tree(s) --------------------------------
Write-Head "6. Stopping superseded gateway tree(s)"
foreach ($t in $targets) {
    $proc = Get-Process -Id $t.Pid -ErrorAction SilentlyContinue
    if (-not $proc) { Write-Info "pid $($t.Pid) already gone."; continue }
    Write-Info "taskkill /PID $($t.Pid) /T /F  (port $($t.Port))"
    & taskkill.exe /PID $t.Pid /T /F *> $null
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $t.Pid -ErrorAction SilentlyContinue) { Write-Bad "pid $($t.Pid) survived." } else { Write-Ok "pid $($t.Pid) stopped." }
}

# --- 7. Clear stale lock / pid / run state ---------------------------------
Write-Head "7. Clearing stale lock/pid/run state"
$stale = @("gateway.lock","kiro_pids.txt","kiro_session_pids.txt","open_slots.json","app_backends.pids.json","mcp-gateway\gateway.sock.lock")
foreach ($f in $stale) {
    $p = Join-Path $KiroCrewHome $f
    if (Test-Path $p) { try { Remove-Item $p -Force -ErrorAction Stop; Write-Ok "removed $f" } catch { Write-Warn "could not remove $f ($($_.Exception.Message))" } }
}
if (Test-Path $runDir) {
    Get-ChildItem $runDir -Filter "gateway-*" -File -ErrorAction SilentlyContinue | ForEach-Object {
        try { Remove-Item $_.FullName -Force -ErrorAction Stop } catch {}
    }
    Write-Ok "cleared run\gateway-* markers"
}

# --- 8. Relaunch and verify ------------------------------------------------
if ($Relaunch) {
    Write-Head "8. Relaunching the desktop app"
    if ($installExe) {
        Start-Process $installExe
        Write-Info "Launched; waiting up to 40s for the gateway to bind..."
        $bound = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 2
            foreach ($port in ($ports | Sort-Object -Unique)) {
                if (Test-HttpAlive $port) { Write-Ok "Dashboard answering HTTP on port $port."; $bound = $true; break }
            }
            if ($bound) { break }
        }
        if (-not $bound) { Write-Warn "Gateway did not answer within the wait window; check $KiroCrewHome\gateway.log." }
    } else {
        Write-Warn "KiroCrew.exe not on PATH; start the app manually."
    }
}

Write-Head "Done"
Write-Ok "Recovery complete. Backup of prior state: $backup"

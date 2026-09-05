<#
.SYNOPSIS
    dev-engine — run the Kiro-Ception engine headless (no Kiro), on demand.

.DESCRIPTION
    A thin wrapper around scripts/debug-engine.py + the engine's localhost HTTP
    API, so you can spin the service up/down and query it directly without an
    editor in the loop. It always targets the isolated test instance defined in
    config.copilot-test.toml (its own cache_dir + engine_port), so it never
    touches a production index.

    Loopback (127.0.0.1) requests skip the peer-encryption path in the engine,
    so 'search'/'status'/'rescan' talk plain JSON over HTTP.

.PARAMETER Command
    start   Start the engine DETACHED in the background. Idempotent: refuses if
            an instance is already recorded/answering. Records BOTH the follower
            (debug-engine.py) and the engine_main child PID to .dev-engine.pid,
            logs to .dev-engine.out/err.log, returns at once.
    stop    Stop the instance THIS pid file recorded — precisely those PIDs
            (engine child + follower). If the engine PID wasn't captured, derives
            it by matching this instance's config. Does NOT sweep.
    down    Deliberate straggler SWEEP: kill every process whose commandline
            matches config.copilot-test.toml (followers, engines, uv wrappers),
            dead-or-alive cleanup. Scoped to the instance-unique config filename,
            so production 'kiro-ception-rearview' engines are never touched. Use
            this to clear accumulated orphans.
    restart stop then start.
    up      Start the engine and hold it open (FOREGROUND; Ctrl+C to stop).
    status  GET /status  — indexing progress + search readiness.
    config  GET /config  — instance identity + resolved paths.
    search  POST /search — run a query. Positional text after 'search' is the query.
    rescan  POST /rescan — pick up new/changed sessions.

.EXAMPLE
    ./scripts/dev-engine.ps1 start                 # background, returns immediately
    ./scripts/dev-engine.ps1 status
    ./scripts/dev-engine.ps1 search "reversing a list in python" -Source copilot
    ./scripts/dev-engine.ps1 stop                  # kills the recorded instance
    ./scripts/dev-engine.ps1 down                  # sweep any copilot-test stragglers
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "restart", "up", "status", "config", "search", "rescan", "down")]
    [string]$Command,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Query,

    [string]$Source = "",
    [int]$MaxResults = 5,
    [int]$Interval = 30
)

$ErrorActionPreference = "Stop"

# Repo root is the parent of this script's directory.
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ConfigPath = Join-Path $RepoRoot "config.copilot-test.toml"

if (-not (Test-Path $ConfigPath)) {
    Write-Error "Config not found: $ConfigPath"
    exit 1
}

# Read engine_port straight from the config so this wrapper stays in sync with it.
$Port = 19766
foreach ($line in Get-Content $ConfigPath) {
    if ($line -match '^\s*engine_port\s*=\s*(\d+)') {
        $Port = [int]$Matches[1]
        break
    }
}
$Base = "http://127.0.0.1:$Port"
$PidFile = Join-Path $RepoRoot ".dev-engine.pid"
$OutLog  = Join-Path $RepoRoot ".dev-engine.out.log"
$ErrLog  = Join-Path $RepoRoot ".dev-engine.err.log"
$DebugPy = Join-Path $RepoRoot "scripts/debug-engine.py"

function Start-Detached {
    # Idempotent: refuse to spawn a duplicate if a recorded instance is alive
    # OR if the port is already answering (catches an instance we didn't record).
    if (Test-Path $PidFile) {
        $rec = Get-Content $PidFile -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($rec -and $rec.follower -and (Get-Process -Id $rec.follower -ErrorAction SilentlyContinue)) {
            Write-Host "[dev-engine] already running (follower PID $($rec.follower)). Use 'restart' or 'stop'." -ForegroundColor Yellow
            return
        }
    }
    try {
        Invoke-RestMethod -Uri "$Base/status" -TimeoutSec 3 | Out-Null
        Write-Host "[dev-engine] port $Port already answering — an instance is up. Use 'down' to clear stragglers first." -ForegroundColor Yellow
        return
    } catch { }  # not answering -> safe to start

    # Fire-and-forget: `cmd /c start` spawns a fully independent process and
    # returns control to us IMMEDIATELY — it does not inherit our stdio pipe, so
    # the calling shell never waits on the child's (slow) model preload. This is
    # the fix for the "start appears to hang for ~30s" symptom: Start-Process
    # with redirected handles kept the parent waiting; `start` severs that.
    # Output is redirected to log files by the inner command.
    $inner = "uv run --directory `"$RepoRoot`" python `"$DebugPy`" --config `"$ConfigPath`" --interval $Interval > `"$OutLog`" 2> `"$ErrLog`""
    cmd /c "start `"dev-engine`" /b $inner"

    # Give the follower a beat to exist, then record it by config-match (we don't
    # get a PID back from `start`). Bounded to ~4s — NOT the engine preload.
    Start-Sleep -Milliseconds 800
    $follower = $null
    $fmatch = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'debug-engine' -and $_.CommandLine -match [regex]::Escape('config.copilot-test.toml') } |
        Select-Object -First 1
    if ($fmatch) { $follower = $fmatch.ProcessId }

    @{ follower = $follower; engine = $null; port = $Port } |
        ConvertTo-Json -Compress | Out-File -FilePath $PidFile -Encoding ascii

    $fDisp = if ($follower) { "$follower" } else { "(detached; derive at stop)" }
    Write-Host "[dev-engine] started background — follower PID $fDisp, port $Port" -ForegroundColor Green
    Write-Host "[dev-engine] engine child spawns after model preload (~30-90s). Poll: dev-engine status" -ForegroundColor Green
    Write-Host "[dev-engine] pid file: $PidFile  |  logs: $OutLog / $ErrLog  |  stop: dev-engine stop" -ForegroundColor Green
}

# Find the engine_main python process for THIS instance, matched by the
# instance-unique config filename. Never matches production engines, which use
# a different --config path.
function Get-EngineChildPid {
    $match = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'engine_main' -and $_.CommandLine -match [regex]::Escape('config.copilot-test.toml') } |
        Select-Object -First 1
    if ($match) { return $match.ProcessId }
    return $null
}

function Stop-Detached {
    if (-not (Test-Path $PidFile)) {
        Write-Host "[dev-engine] no .dev-engine.pid recorded. Use 'down' to sweep any stragglers." -ForegroundColor Yellow
        return
    }
    $rec = Get-Content $PidFile -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
    $targets = @()
    if ($rec.engine) { $targets += [int]$rec.engine }
    # If the engine PID wasn't captured at start, derive it now (scoped to this
    # instance's config), so 'stop' still takes the engine down — not a sweep.
    elseif ($true) { $d = Get-EngineChildPid; if ($d) { $targets += [int]$d } }
    if ($rec.follower) { $targets += [int]$rec.follower }

    # Stop engine first, then follower (order avoids a respawn race).
    foreach ($id in $targets) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Write-Host "[dev-engine] stopped PID $id ($($p.ProcessName))" -ForegroundColor Green }
        else { Write-Host "[dev-engine] PID $id already gone" -ForegroundColor Yellow }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    try { Invoke-RestMethod -Uri "$Base/status" -TimeoutSec 3 | Out-Null; Write-Host "[dev-engine] WARNING: port $Port still answering — run 'down' to sweep." -ForegroundColor Red }
    catch { Write-Host "[dev-engine] confirmed: port $Port no longer answering." -ForegroundColor Green }
}

# Deliberate straggler sweep: kill EVERY copilot-test process (follower or
# engine), dead-or-alive cleanup. Matched strictly by the instance-unique
# config filename so production 'kiro-ception-rearview' engines are never
# touched. This is the opt-in reaper — NOT the default 'stop'.
function Invoke-DownSweep {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape('config.copilot-test.toml') }
    if (-not $procs) { Write-Host "[dev-engine] down: no copilot-test processes found." -ForegroundColor Green }
    foreach ($p in $procs) {
        Write-Host "[dev-engine] down: killing PID $($p.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # Also kill the uv wrapper(s) that launched debug-engine for this instance.
    Get-CimInstance Win32_Process -Filter "Name='uv.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape('config.copilot-test.toml') } |
        ForEach-Object { Write-Host "[dev-engine] down: killing uv PID $($_.ProcessId)" -ForegroundColor Yellow; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    try { Invoke-RestMethod -Uri "$Base/status" -TimeoutSec 3 | Out-Null; Write-Host "[dev-engine] WARNING: port $Port STILL answering after sweep." -ForegroundColor Red }
    catch { Write-Host "[dev-engine] confirmed clean: port $Port no longer answering." -ForegroundColor Green }
}

function Invoke-EngineGet([string]$Path) {
    try {
        Invoke-RestMethod -Uri "$Base$Path" -Method Get -TimeoutSec 15 | ConvertTo-Json -Depth 12
    } catch {
        Write-Error "GET $Path failed — is the engine running? ('dev-engine start'). $_"
        exit 1
    }
}

function Invoke-EnginePost([string]$Path, [hashtable]$Body) {
    $json = ($Body | ConvertTo-Json -Depth 8 -Compress)
    try {
        Invoke-RestMethod -Uri "$Base$Path" -Method Post -Body $json `
            -ContentType "application/json" -TimeoutSec 60 | ConvertTo-Json -Depth 12
    } catch {
        Write-Error "POST $Path failed — is the engine running? ('dev-engine start'). $_"
        exit 1
    }
}

switch ($Command) {
    "start"   { Start-Detached }
    "stop"    { Stop-Detached }
    "down"    { Invoke-DownSweep }
    "restart" { Stop-Detached; Start-Sleep -Seconds 2; Start-Detached }
    "up" {
        Write-Host "[dev-engine] starting engine on port $Port (config: $ConfigPath)" -ForegroundColor Cyan
        Write-Host "[dev-engine] FOREGROUND — holds this terminal; Ctrl+C to stop. Use 'start' for background." -ForegroundColor Cyan
        & uv run --directory $RepoRoot python $DebugPy --config $ConfigPath --interval $Interval
    }
    "status" { Invoke-EngineGet "/status" }
    "config" { Invoke-EngineGet "/config" }
    "rescan" { Invoke-EnginePost "/rescan" @{} }
    "search" {
        $q = ($Query -join " ").Trim()
        if (-not $q) { Write-Error "Usage: dev-engine search `"<query>`" [-Source cli|ide|claude|copilot]"; exit 1 }
        $body = @{ query = $q; max_results = $MaxResults }
        if ($Source) { $body.source = $Source }
        Invoke-EnginePost "/search" $body
    }
}

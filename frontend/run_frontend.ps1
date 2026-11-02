# run_frontend.ps1 (pure ASCII)
# Launch the Banking Agent frontend without requiring Node on system PATH.
# Pure-ASCII on purpose: this machine's PowerShell 5.1 decodes .ps1 as GBK,
# so any hardcoded CJK path would corrupt. All paths are derived at runtime.

$ErrorActionPreference = 'Stop'

# --- resolve own directory (no CJK literal in source) ---
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- log everything to .run.log for post-mortem ---
$log = Join-Path $root ".run.log"
try { Start-Transcript -Path $log -Force -ErrorAction SilentlyContinue } catch {}

# --- global trap: never let the window vanish silently ---
trap {
    Write-Host "FATAL ERROR: $_"
    Write-Host "Full output saved to: $log"
    Stop-Transcript -ErrorAction Ignore
    Read-Host "Press Enter to exit"
    exit 1
}

# --- 1. locate the managed Node shipped with WorkBuddy ---
$node    = $null
$npmCli  = $null
$nodeDir = Join-Path $env:USERPROFILE ".workbuddy\binaries\node\versions"
if (Test-Path $nodeDir) {
    $verDir = Get-ChildItem -Directory $nodeDir | Sort-Object Name -Descending | Select-Object -First 1
    if ($verDir) {
        $candidate = Join-Path $verDir.FullName "node.exe"
        if (Test-Path $candidate) {
            $node   = $candidate
            $npmCli = Join-Path $verDir.FullName "node_modules\npm\bin\npm-cli.js"
        }
    }
}

# --- 2. fall back to system Node / npm on PATH ---
$useSystemNpm = $false
if (-not $node) {
    $sysNode = Get-Command node -ErrorAction SilentlyContinue
    $sysNpm  = Get-Command npm -ErrorAction SilentlyContinue
    if ($sysNode) { $node = $sysNode.Source }
    if ($sysNpm)  { $useSystemNpm = $true }
}

if (-not $node) {
    Write-Host "ERROR: Node.js not found."
    Write-Host "Install Node 22+ from https://nodejs.org and add it to PATH, then re-run."
    Read-Host "Press Enter to exit"
    exit 1
}

# --- 2b. inject Node dir into PATH for this session ---
# npm runs scripts (e.g. `vite`) via node_modules\.bin\*.cmd shims that call `node`,
# which must be resolvable on PATH. The managed Node is NOT on the system PATH when
# launched by double-clicking the .bat from Explorer, so we prepend it here.
# This only affects the current process (and its children), never the system PATH.
$nodeBin = Split-Path $node -Parent
if ($env:PATH -notlike "*$nodeBin*") {
    $env:PATH = "$nodeBin;$env:PATH"
}
Write-Host "Node dir on PATH: $nodeBin"

# --- 3. cd into the frontend folder ---
Set-Location $root
Write-Host "Using Node : $node"

# --- 4. install dependencies only if node_modules is missing ---
if (-not (Test-Path "node_modules")) {
    Write-Host "[1/2] Installing dependencies (first run only, may take a minute)..."
    if ($useSystemNpm) {
        & npm install 2>&1 | Out-Host
    } else {
        & $node $npmCli install 2>&1 | Out-Host
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "npm install failed (exit $LASTEXITCODE). Check your network and retry."
        Read-Host "Press Enter to exit"
        exit 1
    }
} else {
    Write-Host "[1/2] Dependencies already installed, skipping install."
}

# --- 5. start the Vite dev server (keeps this window open) ---
Write-Host "[2/2] Starting Vite dev server..."
Write-Host "Open  http://localhost:5173  in your browser."
Write-Host "Close this window (or Ctrl+C) to stop the server."
Write-Host "----------------------------------------------------------------"
if ($useSystemNpm) {
    & npm run dev 2>&1 | Out-Host
} else {
    & $node $npmCli run dev 2>&1 | Out-Host
}
Write-Host "Dev server exited with code $LASTEXITCODE."
Read-Host "Press Enter to close"

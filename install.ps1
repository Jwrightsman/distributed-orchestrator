# Mycelium — one-line node join (Windows)
#
#   $env:SWARM_SERVER="http://ORCHESTRATOR_IP:8000"; irm https://raw.githubusercontent.com/Jwrightsman/distributed-orchestrator/master/install.ps1 | iex
#
# Omit SWARM_SERVER to auto-discover an orchestrator on your LAN.
# Set SWARM_SECRET if the orchestrator requires a node secret.
#
# What it does: checks Python + Ollama, downloads the repo to
# ~\distributed-orchestrator, installs two Python packages (httpx, rich),
# then runs join.py — which pulls the model and starts working.

$ErrorActionPreference = "Stop"
$repoUrl = "https://github.com/Jwrightsman/distributed-orchestrator"
$dest = Join-Path $HOME "distributed-orchestrator"

Write-Host ""
Write-Host "distributed-orchestrator node setup" -ForegroundColor Green
Write-Host ""

# 1. Python
$python = $null
foreach ($candidate in @("py", "python")) {
    try {
        $v = & $candidate --version 2>$null
        if ($v -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) { $python = $candidate; break }
        }
    } catch {}
}
if (-not $python) {
    Write-Host "Python 3.10+ is required. Install it from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "(check 'Add python.exe to PATH' in the installer), then re-run this script."
    exit 1
}
Write-Host "  Python:  $(& $python --version)"

# 2. Ollama
try {
    $null = & ollama --version 2>$null
    Write-Host "  Ollama:  installed"
} catch {
    Write-Host "Ollama is required. Download it from https://ollama.com/download" -ForegroundColor Red
    Write-Host "Install it (one click), then re-run this script."
    exit 1
}

# 3. Get or update the repo
if (Test-Path (Join-Path $dest "join.py")) {
    Write-Host "  Repo:    already at $dest"
    try { git -C $dest pull --ff-only 2>$null | Out-Null; Write-Host "  Repo:    updated" } catch {}
} else {
    $hasGit = $false
    try { $null = git --version 2>$null; $hasGit = $true } catch {}
    if ($hasGit) {
        git clone --depth 1 $repoUrl $dest
    } else {
        Write-Host "  Repo:    downloading (no git found)..."
        $zip = Join-Path $env:TEMP "swarm-node.zip"
        Invoke-WebRequest "$repoUrl/archive/refs/heads/master.zip" -OutFile $zip
        Expand-Archive $zip -DestinationPath $env:TEMP -Force
        Move-Item (Join-Path $env:TEMP "distributed-orchestrator-master") $dest
        Remove-Item $zip
    }
    Write-Host "  Repo:    $dest"
}

# 4. Python deps (node needs just these two)
& $python -m pip install --quiet --disable-pip-version-check httpx rich
Write-Host "  Deps:    httpx, rich"

# 5. Join the network (join.py pulls the model and starts polling)
Write-Host ""
$joinArgs = @()
if ($env:SWARM_SERVER) { $joinArgs += $env:SWARM_SERVER }
if ($env:SWARM_SECRET) { $joinArgs += @("--secret", $env:SWARM_SECRET) }
Set-Location $dest
& $python join.py @joinArgs

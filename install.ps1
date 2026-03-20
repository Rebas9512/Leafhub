# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — Installer  (Windows PowerShell)
#
#  Run from the project root:
#    Set-ExecutionPolicy -Scope Process RemoteSigned
#    .\install.ps1
#
#  Parameters:
#    -Reinstall     Delete and recreate the .venv
#    -Uninstall     Remove PATH entry and the project venv
#    -Headless      Non-interactive / CI mode
# ──────────────────────────────────────────────────────────────────────────────
param(
    [switch]$Reinstall,
    [switch]$Uninstall,
    [switch]$Headless
)

$ErrorActionPreference = "Stop"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvDir    = Join-Path $ScriptDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"
$LeafhubExe = Join-Path $VenvDir "Scripts\leafhub.exe"
$ScriptsDir = Join-Path $VenvDir "Scripts"

# ANSI colours — PowerShell 7+ / Windows Terminal
$GREEN  = "`e[38;2;0;229;180m"
$YELLOW = "`e[38;2;255;176;32m"
$RED    = "`e[38;2;230;57;70m"
$MUTED  = "`e[38;2;110;120;148m"
$BOLD   = "`e[1m"
$NC     = "`e[0m"

function Write-Ok($msg)      { Microsoft.PowerShell.Utility\Write-Host "${GREEN}√${NC}  $msg" }
function Write-Info($msg)    { Microsoft.PowerShell.Utility\Write-Host "${MUTED}·${NC}  $msg" }
function Write-Warn($msg)    { Microsoft.PowerShell.Utility\Write-Host "${YELLOW}!${NC}  $msg" }
function Write-Section($msg) { Microsoft.PowerShell.Utility\Write-Host ""; Microsoft.PowerShell.Utility\Write-Host "${BOLD}── $msg ──${NC}" }
function Write-Fail($msg)    { Microsoft.PowerShell.Utility\Write-Host "${RED}x${NC}  $msg"; exit 1 }

# ── Execution policy ──────────────────────────────────────────────────────────
$policy = Get-ExecutionPolicy
if ($policy -eq "Restricted" -or $policy -eq "AllSigned") {
    try {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
        Write-Info "Execution policy set to RemoteSigned for this session."
    } catch {
        Write-Fail "Cannot set execution policy. Run as Administrator:`n  Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"
    }
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Microsoft.PowerShell.Utility\Write-Host ""
    Microsoft.PowerShell.Utility\Write-Host "${BOLD}  LeafHub — Uninstall${NC}"

    Write-Section "Removing Scripts dir from user PATH"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$ScriptsDir*") {
        $parts = $userPath.Split(";") | Where-Object { $_ -ne $ScriptsDir -and $_ -ne "" }
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
        Write-Ok "Removed $ScriptsDir from user PATH."
    } else {
        Write-Warn "Not found in user PATH — nothing to remove."
    }

    Write-Section "Removing venv"
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir
        Write-Ok "Removed: $VenvDir"
    } else {
        Write-Warn "Venv not found: $VenvDir"
    }

    Microsoft.PowerShell.Utility\Write-Host ""
    Microsoft.PowerShell.Utility\Write-Host "${BOLD}  Done.${NC}"
    Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}~/.leafhub/ (API keys, DB) was NOT removed.${NC}"
    Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}To delete your stored keys: Remove-Item -Recurse `$env:USERPROFILE\.leafhub${NC}"
    Microsoft.PowerShell.Utility\Write-Host ""
    exit 0
}

# ── Banner ────────────────────────────────────────────────────────────────────
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "${BOLD}  LeafHub — Installer${NC}"
Microsoft.PowerShell.Utility\Write-Host "${MUTED}  Project: $ScriptDir${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

# ── Step 1: Python 3.11+ ──────────────────────────────────────────────────────
Write-Section "Step 1 / 3  —  Python"

function Find-Python {
    foreach ($cmd in @("python3.13","python3.12","python3.11","python3","python")) {
        try {
            $result = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($result) {
                $parts = $result.Trim().Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) { return $cmd }
            }
        } catch {}
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Fail "Python 3.11+ not found.`n  Download from https://www.python.org/downloads/ (tick 'Add Python to PATH')"
}
$PyVer = & $Python -c "import sys; print(sys.version.split()[0])" 2>$null
Write-Ok "Python: $Python ($PyVer)"

# ── Step 2: Venv + install ────────────────────────────────────────────────────
Write-Section "Step 2 / 3  —  Virtual environment"

if (Test-Path $VenvPython) {
    if ($Reinstall) {
        Write-Info "Removing existing .venv (--reinstall) ..."
        Remove-Item -Recurse -Force $VenvDir
    } else {
        Write-Ok "Venv exists — reusing  (pass -Reinstall to force rebuild)"
    }
}

if (-not (Test-Path $VenvPython)) {
    Write-Info "Creating .venv ..."
    & $Python -m venv $VenvDir
    Write-Ok "Venv created."
}

Write-Info "Upgrading pip and setuptools ..."
& $VenvPython -m pip install --upgrade pip setuptools --quiet

Write-Info "Installing leafhub[manage] ..."
& $VenvPip install -e "$ScriptDir[manage]" --quiet
Write-Ok "Package installed."

# ── Step 3: PATH ──────────────────────────────────────────────────────────────
Write-Section "Step 3 / 3  —  PATH registration"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }

if ($userPath -notlike "*$ScriptsDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$ScriptsDir", "User")
    Write-Info "Added $ScriptsDir to user PATH (takes effect in new terminals)."
}
$env:Path = "$ScriptsDir;$env:Path"
Write-Ok "PATH updated."

# ── Done ──────────────────────────────────────────────────────────────────────
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "${BOLD}  LeafHub installed!${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

if ($env:Path -like "*$ScriptsDir*") {
    Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub provider add${NC}        # add an API key"
    Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub project create${NC}      # create a project"
    Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub manage${NC}              # start the Web UI (port 8765)"
    Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub --help${NC}              # full command reference"
} else {
    Microsoft.PowerShell.Utility\Write-Host "  Open a new terminal, then:"
    Microsoft.PowerShell.Utility\Write-Host "    ${GREEN}leafhub --help${NC}"
}
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}Data stored at: `$env:USERPROFILE\.leafhub\${NC}"
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}To uninstall:   .\install.ps1 -Uninstall${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

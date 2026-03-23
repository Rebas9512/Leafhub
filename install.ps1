# ------------------------------------------------------------------------------
#  LeafHub -- Windows One-liner Installer
#
#  irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1 | iex
#
#  To pass parameters use the scriptblock form:
#    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1))) -InstallDir C:\leafhub
#
#  Parameters:
#    -InstallDir <path>   Install directory  (default: $HOME\leafhub)
#  Environment variables:
#    LEAFHUB_DIR          Override the install directory
#    LEAFHUB_REPO_URL     Override the git clone URL
# ------------------------------------------------------------------------------
param(
    [string]$InstallDir = ""
)

$ErrorActionPreference = "Stop"
$DefaultInstallDir = Join-Path $env:USERPROFILE "leafhub"

# ANSI colours -- works on PowerShell 5.1+ and Windows Terminal
$ESC    = [char]0x1b
$GREEN  = "${ESC}[38;2;0;229;180m"
$YELLOW = "${ESC}[38;2;255;176;32m"
$RED    = "${ESC}[38;2;230;57;70m"
$MUTED  = "${ESC}[38;2;110;120;148m"
$BOLD   = "${ESC}[1m"
$NC     = "${ESC}[0m"

function Write-Ok($msg)      { Microsoft.PowerShell.Utility\Write-Host "${GREEN}+${NC}  $msg" }
function Write-Info($msg)    { Microsoft.PowerShell.Utility\Write-Host "${MUTED}.${NC}  $msg" }
function Write-Warn($msg)    { Microsoft.PowerShell.Utility\Write-Host "${YELLOW}!${NC}  $msg" }
function Write-Section($msg) { Microsoft.PowerShell.Utility\Write-Host ""; Microsoft.PowerShell.Utility\Write-Host "${BOLD}-- $msg --${NC}" }
function Write-Fail($msg)    { Microsoft.PowerShell.Utility\Write-Host "${RED}x${NC}  $msg"; exit 1 }
function Assert-ExitCode($msg) { if ($LASTEXITCODE -ne 0) { Write-Fail "$msg (exit code $LASTEXITCODE)" } }

function Test-DirHasEntries([string]$Dir) {
    if (-not (Test-Path $Dir -PathType Container)) { return $false }
    return $null -ne (Get-ChildItem -Force -LiteralPath $Dir | Select-Object -First 1)
}

# -- Resolve install directory -------------------------------------------------
if (-not $InstallDir) {
    if ($env:LEAFHUB_DIR) {
        $InstallDir = $env:LEAFHUB_DIR
    } else {
        $canPrompt = $true
        try { $canPrompt = -not [Console]::IsInputRedirected } catch { $canPrompt = $true }
        if ($canPrompt) {
            $raw = Read-Host "Install directory [$DefaultInstallDir]"
            $InstallDir = if ($raw) { $raw } else { $DefaultInstallDir }
        } else {
            $InstallDir = $DefaultInstallDir
        }
    }
}

$InstallDir = $InstallDir.Trim()
if ($InstallDir.StartsWith('~\')) { $InstallDir = Join-Path $env:USERPROFILE $InstallDir.Substring(2) }
elseif ($InstallDir -eq "~")     { $InstallDir = $env:USERPROFILE }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

# Redirect into a subdirectory if the target is non-empty and not a git repo
if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
    if ((Test-Path $InstallDir -PathType Container) -and (Test-DirHasEntries $InstallDir)) {
        $InstallDir = [IO.Path]::GetFullPath((Join-Path $InstallDir "leafhub"))
    }
}

$VenvDir    = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip    = Join-Path $VenvDir "Scripts\pip.exe"
$LeafhubExe = Join-Path $VenvDir "Scripts\leafhub.exe"
$ScriptsDir = Join-Path $VenvDir "Scripts"

# -- Banner --------------------------------------------------------------------
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "${BOLD}  LeafHub -- Installer${NC}"
Microsoft.PowerShell.Utility\Write-Host "${MUTED}  Install path: $InstallDir${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

# -- Execution policy ----------------------------------------------------------
$policy = Get-ExecutionPolicy
if ($policy -eq "Restricted" -or $policy -eq "AllSigned") {
    try {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
        Write-Info "Execution policy set to RemoteSigned for this session."
    } catch {
        Write-Fail "Cannot set execution policy.`n  Run as Administrator: Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"
    }
}

# -- Python 3.11+ --------------------------------------------------------------
Write-Section "Python"

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

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail "git is required.`n  Install: winget install Git.Git  or  https://git-scm.com"
}

# -- Clone / update ------------------------------------------------------------
Write-Section "Installing LeafHub"

$RepoUrl = if ($env:LEAFHUB_REPO_URL) { $env:LEAFHUB_REPO_URL } else { "https://github.com/Rebas9512/Leafhub.git" }

$hasGit = Test-Path (Join-Path $InstallDir ".git")
if (-not $hasGit -and -not (Test-Path $InstallDir)) {
    Write-Info "Cloning into $InstallDir ..."
    git clone --depth=1 $RepoUrl $InstallDir --quiet
    Assert-ExitCode "git clone failed"
    Write-Ok "Cloned."
} else {
    if (-not $hasGit) {
        Write-Info "Directory exists -- initialising git..."
        git init $InstallDir --quiet
        Assert-ExitCode "git init failed"
        git -C $InstallDir remote add origin $RepoUrl 2>$null
    } else {
        Write-Info "Existing installation found -- syncing to latest..."
    }
    git -C $InstallDir fetch origin --depth=1 --quiet
    Assert-ExitCode "git fetch failed"
    $branch = (git -C $InstallDir symbolic-ref refs/remotes/origin/HEAD 2>$null) -replace '.*/','';
    if (-not $branch) { $branch = "main" }
    git -C $InstallDir reset --hard "origin/$branch" --quiet
    Assert-ExitCode "git reset failed"
    git -C $InstallDir clean -fdx --quiet 2>$null
    Write-Ok "Synced to latest ($branch)."
}

# -- Virtual environment + install ---------------------------------------------
Write-Section "Virtual environment"

if (-not (Test-Path $VenvPython)) {
    Write-Info "Creating .venv ..."
    & $Python -m venv $VenvDir
    Assert-ExitCode "Failed to create virtual environment"
    Write-Ok "Venv created."
} else {
    Write-Ok "Venv exists -- reusing."
}

Write-Info "Upgrading pip and setuptools ..."
& $VenvPython -m pip install --upgrade pip setuptools --quiet
Assert-ExitCode "pip upgrade failed"

Write-Info "Installing leafhub[manage] ..."
& $VenvPip install -e "$InstallDir[manage]" --quiet
Assert-ExitCode "Package install failed"
Write-Ok "Package installed."

# -- PATH ----------------------------------------------------------------------
Write-Section "PATH"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }

if ($userPath -notlike "*$ScriptsDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$ScriptsDir", "User")
    Write-Info "Added $ScriptsDir to user PATH (takes effect in new terminals)."
}
$env:Path = "$ScriptsDir;$env:Path"
Write-Ok "PATH updated."

# -- Done ----------------------------------------------------------------------
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "${BOLD}  LeafHub installed!${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}If the command is not recognised, open a new terminal first.${NC}"
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub --help${NC}              # verify install"
Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub provider add${NC}        # add an API key"
Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub project create${NC}      # create a project"
Microsoft.PowerShell.Utility\Write-Host "  ${GREEN}leafhub manage${NC}              # start the Web UI"
Microsoft.PowerShell.Utility\Write-Host ""
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}Install dir:  $InstallDir${NC}"
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}Data stored:  `$env:USERPROFILE\.leafhub\${NC}"
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}To update:    git -C `"$InstallDir`" pull${NC}"
Microsoft.PowerShell.Utility\Write-Host "  ${MUTED}To uninstall: Remove-Item -Recurse `"$InstallDir`"${NC}"
Microsoft.PowerShell.Utility\Write-Host ""

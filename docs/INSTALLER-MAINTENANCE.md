# Leaf Projects -- Installer & Distribution Maintenance Guide

Cross-project reference for maintaining the install scripts (`install.cmd`, `install.ps1`, `install.sh`) and CI pipelines across LeafHub, LeafScan, Trileaf, and future Leaf projects.

This document was authored after a full Windows real-machine validation pass on 2026-03-23. Every constraint and pitfall below is backed by a real failure observed during testing.

---

## Table of Contents

1. [Distribution Architecture](#1-distribution-architecture)
2. [install.ps1 Stage-by-Stage Reference](#2-installps1-stage-by-stage-reference)
3. [LeafHub Integration for Child Projects](#3-leafhub-integration-for-child-projects)
4. [PowerShell 5.1 Constraints](#4-powershell-51-constraints)
5. [Git Operations on Windows](#5-git-operations-on-windows)
6. [Python / pip Gotchas](#6-python--pip-gotchas)
7. [CI Pipeline Standard](#7-ci-pipeline-standard)
8. [New Project Checklist](#8-new-project-checklist)
9. [Session Log: 2026-03-23](#9-session-log-2026-03-23)

---

## 1. Distribution Architecture

### One-liner Entry Points

| Platform | Command | Flow |
|----------|---------|------|
| Windows CMD | `curl -fsSL .../install.cmd -o install.cmd && install.cmd && del install.cmd` | CMD -> install.cmd -> powershell.exe -File install.ps1 -> PATH refresh |
| Windows PowerShell | `irm .../install.ps1 \| iex` | Direct PS execution (no CMD wrapper, no -File encoding issue) |
| macOS / Linux | `curl -fsSL .../install.sh \| bash` | bash -> install.sh -> setup.sh (if delegating) |

### File Responsibilities

```
install.cmd   (Windows CMD bootstrap -- thin wrapper)
  |
  |-- 1. Downloads install.ps1 to %TEMP% via Invoke-WebRequest
  |-- 2. Runs: powershell -NoProfile -ExecutionPolicy Bypass -File <temp>.ps1
  |-- 3. Deletes temp file
  |-- 4. On success: refreshes PATH from registry (endlocal + reg query)
  |      This makes the CLI usable in the SAME CMD session.
  |
  +-- Exit

install.ps1   (Windows main installer -- all logic lives here)
  |
  |-- Stage 1: Resolve install directory
  |-- Stage 2: Clone / sync repo
  |-- Stage 3: Find Python, create venv, pip install
  |-- Stage 4: LeafHub integration (child projects only)
  |-- Stage 5: Project-specific deps (e.g. Playwright)
  |-- Stage 6: PATH registration
  |-- Stage 7: Done message
  |
  +-- Exit

install.sh    (macOS / Linux equivalent)
  |
  |-- Similar stages to install.ps1
  |-- May delegate to setup.sh for project-specific steps
  |
  +-- Exit
```

### Key Design Principle

`install.cmd` is a **thin, stable wrapper** that rarely changes. All logic lives in `install.ps1` which is downloaded fresh from GitHub on every run. This means install.ps1 fixes take effect immediately without users re-downloading install.cmd.

---

## 2. install.ps1 Stage-by-Stage Reference

### Preamble: Helpers & Constants

```powershell
$ErrorActionPreference = "Stop"   # Catches PS cmdlet errors (NOT native commands)

$ESC = [char]0x1b                 # ANSI escape -- PS 5.1 compatible (NOT `e)
$GREEN = "${ESC}[38;2;0;229;180m" # All colors built from $ESC

function Assert-ExitCode($msg) {  # Check $LASTEXITCODE after every native command
    if ($LASTEXITCODE -ne 0) { Write-Fail "$msg (exit code $LASTEXITCODE)" }
}
```

### Stage 1: Resolve Install Directory

```
Input: user prompt / env var / default (~\<project>)
  |
  +-- Path is a FILE? -> Remove-Item -Force (prevents git init failure)
  +-- Path has .git?  -> Skip redirect (treat as existing install)
  +-- Path is non-empty dir without .git? -> Redirect to <path>\<project> subdirectory
  +-- Path doesn't exist? -> Will be created by git clone
```

**Why the file check:** A zero-byte file can be left by failed downloads or interrupted installs. `git init` / `git clone` both fail with "cannot mkdir: File exists" if the path is a file.

### Stage 2: Clone / Sync Repo

Three-way decision:

```
Path doesn't exist at all?
  -> git clone --depth=1 (fastest path)

Path exists with .git?
  -> git fetch origin --depth=1
  -> git reset --hard origin/<branch>
  -> git clean -fd (NOT -fdx -- preserves .leafhub, .venv)

Path exists without .git?
  -> git -C <path> init (NOT git init <path> -- avoids mkdir error)
  -> git remote add origin <url>
  -> git fetch + reset --hard + clean -fd
```

**Why `git -C <path> init` not `git init <path>`:** The latter tries to `mkdir` which fails if the directory already exists on some git versions.

**Why `-fd` not `-fdx`:** The `-x` flag deletes gitignored files. User data like `.leafhub` (project registration) and `.venv` would be destroyed on every re-install.

### Stage 3: Python / Venv / pip

```
Find Python 3.11+ -> Create .venv -> Upgrade pip -> pip install -e <project>[extras]
```

Each external command is followed by `Assert-ExitCode`. The venv is reused if it already exists.

### Stage 4: LeafHub Integration

**See [Section 3](#3-leafhub-integration-for-child-projects) for full detail.**

### Stage 5: Project-Specific Dependencies

Example (LeafScan):
```powershell
& $VenvPython -m playwright install chromium
Assert-ExitCode "Playwright install failed"
```

### Stage 6: PATH Registration

```powershell
# Persist to user PATH (survives reboots)
[Environment]::SetEnvironmentVariable("Path", "$userPath;$ScriptsDir", "User")

# Also set in current PS session (for irm | iex users)
$env:Path = "$ScriptsDir;$env:Path"
```

The `install.cmd` wrapper additionally refreshes PATH in the parent CMD session:
```cmd
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PATH=%%b;%PATH%"
```

### Stage 7: Done Message

Show hint text with correct CLI commands. Include "open a new terminal" fallback message.

---

## 3. LeafHub Integration for Child Projects

This is the most complex part of the installer for projects that depend on LeafHub (e.g., LeafScan, Trileaf). It manages two separate Python environments and coordinates between them.

### Why Two Environments

```
Child project venv (e.g. ~/leafscan/.venv/)
  |-- Has: child project code + leafhub base package (from pip)
  |-- Can:  leafhub register, leafhub provider add/list (DB operations)
  |-- CANNOT: leafhub manage (Web UI needs ui/ directory + fastapi + npm)
  |
  |   pip install "leafhub @ git+..." installs leafhub as a PACKAGE into
  |   site-packages. The ui/ directory (Vue frontend) is NOT included in
  |   the package -- it only exists in the git repo checkout.

Standalone LeafHub install (~/leafhub/)
  |-- Has: full git clone with ui/, .venv with leafhub[manage] (fastapi, uvicorn)
  |-- Can:  EVERYTHING including leafhub manage (Web UI)
  |-- Lives at: ~/leafhub/.venv/Scripts/leafhub.exe (Windows)
  |             ~/leafhub/.venv/bin/leafhub (Unix)
```

### Decision Flow

```
                      Has .leafhub dotfile?
                      (project already registered)
                            |
                     yes ---+--- no
                      |           |
                skip LeafHub   Find standalone leafhub
                section        at ~/leafhub/.venv/Scripts/leafhub.exe
                                   |
                            found -+- not found
                              |           |
                      $SystemLeafhub   Run leafhub one-liner installer
                        = found path     irm .../install.ps1 | iex
                              |           |
                              |      Re-check candidates
                              |           |
                              +-----+-----+
                                    |
                         $LeafhubCmd = $SystemLeafhub ?? $VenvLeafhub
                                    |
                         Register project (--headless)
                         leafhub register <name> --path <dir> --alias <alias>
                                    |
                              Interactive? ----no----> done
                                    |
                                   yes
                                    |
                        $SystemLeafhub available?
                              |            |
                             yes          no
                              |            |
                      Show 3 options:   Show 2 options:
                      [1] Web UI        [1] Terminal
                      [2] Terminal      [s] Skip
                      [s] Skip
                              |
                        (if Web UI chosen)
                              |
                      Start-Process $SystemLeafhub manage --no-browser
                      Start-Process "http://localhost:8765"
                      Wait for Enter
                      Stop-Process manage server
                      Re-register --headless (to bind new provider)
```

### Key Variables

| Variable | Points to | Can run manage? | Notes |
|----------|-----------|----------------|-------|
| `$VenvLeafhub` | `<project>/.venv/Scripts/leafhub.exe` | NO | pip package, no ui/, no fastapi |
| `$SystemLeafhub` | `~/leafhub/.venv/Scripts/leafhub.exe` | YES | Full clone with ui/, npm, fastapi |
| `$LeafhubCmd` | `$SystemLeafhub` if available, else `$VenvLeafhub` | Depends | Used for register/provider operations |

**Rule: Always use `$SystemLeafhub` for `manage`. Use `$LeafhubCmd` for everything else.**

### Starting the Web UI (Windows-safe)

```powershell
# Start-Process is reliable on Windows (avoids python -m, subprocess, __main__.py issues)
$manageProc = Start-Process -FilePath $SystemLeafhub -ArgumentList "manage","--no-browser" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3          # Wait for server to start
Start-Process "http://localhost:8765"  # Open browser
Read-Host "`n  Press Enter when done"
Stop-Process -Id $manageProc.Id -Force -ErrorAction SilentlyContinue
```

**Why NOT use leafhub register's built-in Web UI launch:**
- It uses `subprocess.Popen` internally which has `python -m leafhub` / `__main__.py` issues
- pip-cached code may have old subprocess logic
- `Start-Process` in PowerShell is more reliable and we control the binary path directly

### Template: LeafHub Section for New Child Projects

Copy this block into new child project `install.ps1` and replace `leafscan`/`llm`:

```powershell
# -- LeafHub setup -------------------------------------------------------------
$VenvLeafhub = Join-Path $ScriptsDir "leafhub.exe"
if (-not (Test-Path $VenvLeafhub)) { $VenvLeafhub = Join-Path $ScriptsDir "leafhub" }

$SystemLeafhub = $null
$candidates = @(
    (Join-Path $env:USERPROFILE "leafhub\.venv\Scripts\leafhub.exe"),
    (Join-Path $env:USERPROFILE "leafhub\.venv\Scripts\leafhub")
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $SystemLeafhub = $c; break }
}

$dotLeafhub = Join-Path $InstallDir ".leafhub"
$needsSetup = -not (Test-Path $dotLeafhub)

if ($needsSetup) {
    Write-Host ""
    Write-Host "${BOLD}-- LeafHub --${NC}"

    if (-not $SystemLeafhub) {
        Write-Info "Installing LeafHub (required for API key management)..."
        Write-Host "  ${MUTED}This provides the Web UI for configuring API providers.${NC}"
        Write-Host ""
        try {
            $leafhubInstallUrl = "https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1"
            & ([scriptblock]::Create((Invoke-RestMethod $leafhubInstallUrl)))
        } catch {
            Write-Host "  ${MUTED}LeafHub auto-install failed. Install manually:${NC}"
            Write-Host "  ${MUTED}  irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1 | iex${NC}"
        }
        foreach ($c in $candidates) {
            if (Test-Path $c) { $SystemLeafhub = $c; break }
        }
    }

    $LeafhubCmd = if ($SystemLeafhub) { $SystemLeafhub } else { $VenvLeafhub }

    Write-Info "Registering <PROJECT> project..."
    & $LeafhubCmd register <PROJECT_NAME> --path $InstallDir --alias <ALIAS> --headless 2>$null

    $canPrompt = $false
    try { $canPrompt = [Console]::KeyAvailable -ne $null -and -not [Console]::IsInputRedirected } catch {}

    if ($canPrompt -and $SystemLeafhub) {
        # ... Web UI + Terminal + Skip prompt (see LeafScan install.ps1 for full code)
    } elseif ($canPrompt) {
        # ... Terminal + Skip prompt (no Web UI available)
    }

    Write-Ok "LeafHub setup complete."
}
```

Replace:
- `<PROJECT_NAME>` -> project slug (e.g., `leafscan`, `trileaf`)
- `<ALIAS>` -> leafhub binding alias (e.g., `llm`, `default`)

### Shared Vault, Separate Venvs

All Leaf projects share one LeafHub vault (`~/.leafhub/`). The vault stores:
- Providers (API keys, encrypted)
- Projects (name, path, bindings)

When a user installs both LeafScan and Trileaf:
```
~/.leafhub/              <-- shared vault (one SQLite DB)
  |-- providers: [OpenAI, Anthropic, ...]
  |-- projects:
  |     leafscan -> C:\Users\user\leafscan (alias: llm -> OpenAI)
  |     trileaf  -> C:\Users\user\trileaf  (alias: default -> Anthropic)

~/leafhub/               <-- standalone LeafHub installation (full clone)
  |-- .venv/             <-- leafhub[manage] + fastapi + uvicorn
  |-- ui/                <-- Vue frontend (npm build)
  |-- src/leafhub/       <-- source code

~/leafscan/              <-- child project
  |-- .venv/             <-- leafscan + leafhub (base, pip package)
  |-- .leafhub           <-- dotfile linking to vault

~/trileaf/               <-- another child project
  |-- .venv/             <-- trileaf + leafhub (base, pip package)
  |-- .leafhub           <-- dotfile linking to vault
```

The `leafhub register --headless` command:
1. Creates or re-links the project in the shared vault
2. Writes a `.leafhub` dotfile in the project directory
3. Auto-binds a provider alias if providers exist

---

## 4. PowerShell 5.1 Constraints

### 4.1 Encoding: Pure ASCII Only (CRITICAL)

`install.cmd` invokes `powershell.exe` (5.1) which reads `-File` scripts as **Windows-1252 (ANSI)** when no BOM is present.

UTF-8 multi-byte sequences containing bytes `0x91`-`0x94` map to **quote characters** in Win-1252, destroying string parsing:

| Character | UTF-8 Bytes | Dangerous Byte | Win-1252 Meaning |
|-----------|-------------|---------------|-----------------|
| `--` (em dash U+2014) | `E2 80 94` | `0x94` | `"` right double quote |
| `--` (en dash U+2013) | `E2 80 93` | `0x93` | `"` left double quote |
| `-` (box drawing U+2500) | `E2 94 80` | `0x94` | `"` right double quote |

**Rule: All `.ps1` files must contain only ASCII (bytes 0x00-0x7F).**

Replacements: `--` -> `--`, `-` -> `-`, `+` -> `+`, `.` -> `.`, `x` -> `x`

CI check:
```bash
grep -P '[^\x00-\x7F]' install.ps1 && exit 1
```

### 4.2 ANSI Escape Sequences

`` `e `` is PS 7+ only. Use `[char]0x1b`:

```powershell
$ESC = [char]0x1b
$GREEN = "${ESC}[38;2;0;229;180m"
```

### 4.3 $ErrorActionPreference vs Native Commands

`$ErrorActionPreference = "Stop"` does NOT catch external command failures. Always use:

```powershell
git clone ... --quiet
Assert-ExitCode "git clone failed"
```

### 4.4 Native Command Stderr

PS 5.1 + `$ErrorActionPreference = "Stop"` converts native command stderr into a **terminating error** that `2>$null` and even `try/catch` cannot fully suppress. This specifically affects commands that output Unicode to stderr (e.g., `leafhub provider list` with table-drawing characters).

**Rule: Do not capture or check output of native commands that may emit non-ASCII. Check exit codes only, or avoid the call.**

---

## 5. Git Operations on Windows

### 5.1 Three-Way Clone/Sync Strategy

See [Stage 2](#stage-2-clone--sync-repo) for the full decision tree.

### 5.2 `git init` Pitfall

`git init <path>` calls `mkdir(path)` internally. If the path already exists, some git versions fail with "cannot mkdir: File exists". Always use:

```powershell
git -C $InstallDir init --quiet   # cd into dir first, then init
```

### 5.3 `git clean` Flags

| Flag | Effect | Use? |
|------|--------|------|
| `-f` | Remove untracked files | YES |
| `-d` | Remove untracked directories | YES |
| `-x` | Also remove gitignored files | **NO** -- destroys .leafhub, .venv |

### 5.4 File at Install Path

A zero-byte file (from interrupted downloads, etc.) at the install path causes both `git clone` and `git init` to fail. Check before any git operation:

```powershell
if ((Test-Path $InstallDir) -and -not (Test-Path $InstallDir -PathType Container)) {
    Remove-Item -Force $InstallDir
}
```

---

## 6. Python / pip Gotchas

### 6.1 npm Subprocess on Windows

`npm` is `npm.cmd` on Windows. `subprocess.run(["npm", ...])` fails with `[WinError 2]`:

```python
_win = sys.platform == "win32"
subprocess.run(["npm", "install"], cwd=ui_dir, check=True, shell=_win)
```

### 6.2 pip Wheel Caching with Git URLs

`pip install "pkg @ git+https://..."` caches wheels keyed by the git URL. A fresh venv may get stale code:

```powershell
& $VenvPip install "leafhub[manage] @ git+https://..." --upgrade --no-cache-dir --quiet
```

### 6.3 `python -m <package>` Requires __main__.py

If spawning a package as a subprocess via `python -m <pkg>`, ensure `<pkg>/__main__.py` exists. Prefer using the installed entry point binary (`shutil.which("leafhub")`) over `python -m`.

---

## 7. CI Pipeline Standard

### Required Jobs (all Leaf projects)

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.11", "3.12"]   # adjust per project
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install and test
        run: |
          pip install -e ".[dev]"
          python -m pytest tests/ -v --tb=short

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Python syntax check
        run: find . -name '*.py' -not -path './.venv/*' | xargs python -m py_compile
      - name: Shell script syntax
        run: |
          for f in install.sh setup.sh; do
            [ -f "$f" ] && bash -n "$f"
          done
      - name: PS1 ASCII enforcement
        run: |
          for f in install.ps1 setup.ps1; do
            [ -f "$f" ] || continue
            if grep -Pq '[^\x00-\x7F]' "$f"; then
              echo "ERROR: $f contains non-ASCII characters"
              exit 1
            fi
          done

  ps1-syntax:
    name: PowerShell syntax check
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - name: Validate PS1 files
        shell: pwsh
        run: |
          foreach ($file in @("install.ps1", "setup.ps1")) {
            if (-not (Test-Path $file)) { continue }
            $errors = $null
            $null = [System.Management.Automation.Language.Parser]::ParseFile(
              "$PWD/$file", [ref]$null, [ref]$errors
            )
            if ($errors.Count -gt 0) {
              $errors | ForEach-Object { Write-Error $_.Message }
              exit 1
            }
            Write-Host "$file syntax OK"
          }
```

### What CI Does NOT Catch

| Risk | Why CI misses it | Mitigation |
|------|-----------------|------------|
| Win-1252 encoding corruption | PS parser runs in pwsh (7+), not powershell (5.1) | ASCII enforcement in lint job |
| `$ErrorActionPreference` + native stderr | Syntax-level check only | Manual testing on PS 5.1 |
| File lock issues (antivirus, Explorer) | CI has clean environment | Robust git init+fetch+reset strategy |
| pip wheel caching | CI uses fresh runners | `--no-cache-dir` in install scripts |
| CDN cache delays | CI doesn't test install one-liner from GitHub | Wait 5 min after push before testing |

---

## 8. New Project Checklist

### Step 1: Copy Template Files

From an existing project (e.g., LeafScan):
- `install.cmd`
- `install.ps1`
- `install.sh`
- `setup.sh` (if using delegated setup)

### Step 2: Search-Replace

| Find | Replace with |
|------|-------------|
| `leafscan` | `newproject` (lowercase slug) |
| `LeafScan` | `NewProject` (display name) |
| `Rebas9512/Leafscan` | `Rebas9512/NewProject` |
| `LEAFSCAN_DIR` | `NEWPROJECT_DIR` |
| `LEAFSCAN_REPO_URL` | `NEWPROJECT_REPO_URL` |
| LeafHub alias `llm` | Project's alias (e.g., `default`) |
| Playwright install step | Project-specific deps |
| CLI hint commands | Actual CLI commands |

### Step 3: Verify ASCII

```bash
grep -P '[^\x00-\x7F]' install.ps1 setup.ps1 && echo "FAIL: non-ASCII found" || echo "OK"
```

### Step 4: Add CI

Copy the `ps1-syntax` job and ASCII enforcement. Adapt the test matrix.

### Step 5: Test Matrix

| Scenario | What to verify |
|----------|---------------|
| Fresh install (CMD) | Full flow works, CLI usable in same terminal |
| Fresh install (irm \| iex) | Full flow works |
| Re-install over existing | .leafhub preserved, no setup re-prompt |
| Install with file at path | File removed, clone succeeds |
| Install without leafhub | Auto-install triggered, provider setup offered |
| Install with leafhub already installed | Standalone used for Web UI, no re-install |
| Headless/CI mode | Non-interactive, no prompts |

---

## 9. Session Log: 2026-03-23

### Issues Found & Fixed

| # | Issue | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | PS1 parse errors (TerminatorExpectedAtEndOfString) | `` `e `` ANSI escape (PS 7+ only) | Replace with `$ESC = [char]0x1b` |
| 2 | PS1 parse errors persist after backtick-e fix | Em dash UTF-8 byte 0x94 = `"` in Win-1252 | Make all PS1 files pure ASCII |
| 3 | CLI not recognised after install | CMD PATH not refreshed | `reg query` PATH refresh in install.cmd |
| 4 | npm subprocess fails ([WinError 2]) | npm is npm.cmd on Windows | `shell=True` on win32 |
| 5 | start_new_session fails on Windows | POSIX-only parameter | `CREATE_NEW_PROCESS_GROUP` on win32 |
| 6 | Leafscan install.ps1 delegates to setup.sh | No native Windows setup path | Rewrote to handle venv/pip natively |
| 7 | git clone fails, script continues | $ErrorActionPreference ignores native cmds | Assert-ExitCode after all external commands |
| 8 | Remove-Item can't delete locked directory | Windows file locks (antivirus/IDE) | Replace delete+clone with git init+fetch+reset |
| 9 | git init fails ("cannot mkdir") | Path is a file, not directory | Test-Path -PathType Container check + git -C init |
| 10 | leafhub manage fails from pip install | ui/ directory only in git clone | Use standalone leafhub for Web UI |
| 11 | python -m leafhub fails | No __main__.py | Added __main__.py + shutil.which fallback |
| 12 | No module named 'fastapi' in register | Venv leafhub = base package only | Use standalone leafhub for all operations |
| 13 | charmap codec error on provider list | PS 5.1 can't encode Unicode stderr | Removed provider list verification |
| 14 | .leafhub deleted on re-install | git clean -fdx removes gitignored files | Changed to git clean -fd |
| 15 | leafhub uninstall crashes | Missing `import os` | Added import |
| 16 | Setup prompt repeats every re-install | .leafhub destroyed by git clean -fdx | Fixed by #14 |
| 17 | pip installs cached old leafhub | pip wheel cache by git URL | --upgrade --no-cache-dir |

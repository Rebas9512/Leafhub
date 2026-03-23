# Leaf Projects -- Installer & Distribution Maintenance Guide

Cross-project reference for maintaining install scripts and CI pipelines across LeafHub, LeafScan, Trileaf, and future Leaf projects.

Authored after full real-machine validation on Windows (CMD + PowerShell) and macOS/Linux on 2026-03-23. Every constraint below is backed by a real failure observed during testing.

---

## Table of Contents

- **Part 1** [Universal Standards](#part-1-universal-standards) -- applies to ALL platforms
- **Part 2** [CMD-Specific (install.cmd)](#part-2-cmd-specific-installcmd) -- Windows CMD wrapper
- **Part 3** [PowerShell-Specific (install.ps1)](#part-3-powershell-specific-installps1) -- Windows installer logic
- **Part 4** [Terminal-Specific (install.sh / setup.sh)](#part-4-terminal-specific-installsh--setupsh) -- macOS / Linux
- **Part 5** [LeafHub Integration for Child Projects](#part-5-leafhub-integration-for-child-projects)
- **Part 6** [CI Pipeline Standard](#part-6-ci-pipeline-standard)
- **Part 7** [New Project Checklist](#part-7-new-project-checklist)
- **Appendix** [Session Log: 2026-03-23](#appendix-session-log-2026-03-23)

---

# Part 1: Universal Standards

These rules apply to ALL install scripts regardless of platform.

## 1.1 Distribution Architecture

```
User (Windows CMD)
  curl ... install.cmd -o install.cmd && install.cmd
    -> install.cmd downloads install.ps1 -> powershell.exe -File install.ps1

User (Windows PowerShell)
  irm .../install.ps1 | iex
    -> direct PS execution (no CMD wrapper)

User (macOS / Linux)
  curl ... install.sh | bash
    -> install.sh (may delegate to setup.sh)
```

### File Roles

| File | Platform | Role |
|------|----------|------|
| `install.cmd` | Windows CMD | Thin bootstrap: downloads PS1, runs it, refreshes PATH |
| `install.ps1` | Windows PS 5.1+ | Full installer logic (must work in both PS 5.1 and 7+) |
| `install.sh` | macOS / Linux | Full installer logic (bash). May delegate to `setup.sh` |
| `setup.sh` | macOS / Linux | Project-specific setup (venv, deps, CLI registration) |
| `setup.ps1` | Windows | Project-specific setup (Trileaf uses this pattern) |

## 1.2 Three-Way Git Clone/Sync Strategy

Every installer must handle three directory states. This is the **single most important pattern** -- it was the source of 5+ bugs on Windows.

```
Path doesn't exist at all?
  -> git clone --depth=1 (fastest)

Path exists with .git?
  -> git fetch origin --depth=1
  -> determine branch (symbolic-ref, fallback "main")
  -> git reset --hard origin/<branch>
  -> git clean -fd (NOT -fdx)

Path exists without .git?
  -> git -C <path> init (NOT git init <path>)
  -> git remote add origin <url>
  -> git fetch + reset --hard + clean -fd
```

### Rules

- **`git -C <path> init`** not `git init <path>`: the latter tries mkdir, fails if dir exists
- **`git clean -fd`** not `-fdx`: the `-x` flag deletes gitignored files (`.leafhub`, `.venv`)
- **File-at-path guard**: always check if the install path is a file (not directory) and remove it before any git operation. Zero-byte files left by interrupted downloads cause "cannot mkdir: File exists"

## 1.3 Exit Code Checking After External Commands

Neither `set -euo pipefail` (bash) nor `$ErrorActionPreference = "Stop"` (PowerShell) reliably catches external command failures in all scenarios.

**Rule: Every call to `git`, `python`, `pip`, `npm` must have an explicit exit code check.**

PowerShell:
```powershell
git clone --depth=1 $url $dir --quiet
Assert-ExitCode "git clone failed"
```

Bash:
```bash
git clone --depth=1 "$url" "$dir" --quiet \
    || fail "git clone failed."
```

## 1.4 Idempotent Re-install

Running the installer on an existing installation must:
1. Sync to latest code (fetch + reset)
2. Preserve user configuration (`.leafhub` dotfile, `.venv`)
3. Not re-prompt for completed setup steps (check `.leafhub` existence)
4. Not delete user data (`git clean -fd`, not `-fdx`)

## 1.5 PATH Registration

All platforms must:
1. **Persist PATH** (survives reboots): registry on Windows, shell RC on Unix
2. **Update current session**: so the CLI works immediately after install
3. **Show fallback message**: "If not recognised, open a new terminal"

## 1.6 ASCII-Only Installer Scripts

Both `.ps1` and `.sh` files must be pure ASCII for maximum portability:
- PS 5.1 misinterprets UTF-8 as Windows-1252 (em dash byte = double quote)
- Some terminals garble Unicode symbols

Safe replacements: `--` for em dash, `-` for box drawing, `+` for checkmark, `.` for middle dot, `x` for cross

## 1.7 pip Caching with Git URLs

`pip install "pkg @ git+https://..."` aggressively caches wheels. Use `--upgrade --no-cache-dir` when freshness matters.

---

# Part 2: CMD-Specific (install.cmd)

The `.cmd` file is a **thin, stable wrapper** that rarely changes. All logic lives in `install.ps1`.

## 2.1 Structure

```cmd
@echo off
setlocal

set "SCRIPT_URL=https://raw.githubusercontent.com/.../install.ps1"
set "SCRIPT_PATH=%TEMP%\<project>-install-%RANDOM%%RANDOM%.ps1"

rem Download PS1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { Invoke-WebRequest -UseBasicParsing '%SCRIPT_URL%' -OutFile '%SCRIPT_PATH%' } catch { Write-Host $_; exit 1 }"

rem Execute PS1
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_PATH%" %*
set "EXITCODE=%ERRORLEVEL%"

rem Cleanup + PATH refresh
del "%SCRIPT_PATH%" >nul 2>&1
if %EXITCODE% equ 0 (
    endlocal
    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PATH=%%b;%PATH%"
) else (
    endlocal
)
exit /b %EXITCODE%
```

## 2.2 PATH Refresh

After `install.ps1` writes to the user PATH via `[Environment]::SetEnvironmentVariable`, the parent CMD session still has the old PATH. The `reg query` line reads the updated value from the registry into the current CMD session.

**Without this, the CLI is not usable until a new terminal is opened.**

## 2.3 Temp File Naming

Use `%RANDOM%%RANDOM%` in the temp filename to avoid collisions when multiple installs run concurrently.

## 2.4 ExecutionPolicy

`-ExecutionPolicy Bypass` is passed to `powershell.exe` to avoid issues with restricted execution policies. The PS1 script additionally handles this internally (see 3.12).

---

# Part 3: PowerShell-Specific (install.ps1)

All `.ps1` files must run under both `powershell.exe` (5.1) and `pwsh` (7+).

## 3.1 Encoding: Pure ASCII Only (CRITICAL)

`powershell.exe -File` reads scripts as **Windows-1252 (ANSI)** when no BOM is present. UTF-8 multi-byte sequences containing bytes `0x91`-`0x94` map to quote characters, destroying ALL string parsing:

| Character | UTF-8 Bytes | Dangerous Byte | Win-1252 Meaning |
|-----------|-------------|---------------|-----------------|
| em dash (U+2014) | `E2 80 94` | `0x94` | `"` right double quote |
| en dash (U+2013) | `E2 80 93` | `0x93` | `"` left double quote |
| box drawing (U+2500) | `E2 94 80` | `0x94` | `"` right double quote |

## 3.2 ANSI Escape Sequences

`` `e `` is PS 7+ only. Use `[char]0x1b`:

```powershell
$ESC = [char]0x1b
$GREEN = "${ESC}[38;2;0;229;180m"
```

Use `${ESC}` (with curly braces) to avoid ambiguity with `$E`.

## 3.3 Standard Helper Functions

```powershell
$ESC = [char]0x1b
$GREEN = "${ESC}[38;2;0;229;180m"; $RED = "${ESC}[38;2;230;57;70m"
$MUTED = "${ESC}[38;2;110;120;148m"; $BOLD = "${ESC}[1m"; $NC = "${ESC}[0m"

function Write-Ok($msg)   { Write-Host "${GREEN}+${NC}  $msg" }
function Write-Info($msg) { Write-Host "${MUTED}.${NC}  $msg" }
function Write-Fail($msg) { Write-Host "${RED}x${NC}  $msg"; exit 1 }

function Assert-ExitCode($msg) {
    if ($LASTEXITCODE -ne 0) { Write-Fail "$msg (exit code $LASTEXITCODE)" }
}
```

**LeafHub note:** Uses `Microsoft.PowerShell.Utility\Write-Host` (fully qualified) to avoid conflicts if a child project redefines `Write-Host`.

## 3.4 $ErrorActionPreference vs Native Commands

`$ErrorActionPreference = "Stop"` only catches **PowerShell cmdlet** errors, NOT external commands.

**Rule:** Every `git`, `python`, `pip`, `npm` call must be followed by `Assert-ExitCode`.

## 3.5 Native Command Stderr as Terminating Error

PS 5.1 + `$ErrorActionPreference = "Stop"` converts native command stderr into a **terminating error** that `2>$null` and even `try/catch` cannot fully suppress.

**Rules:**
1. Never capture native command output into variables when it may write to stderr
2. Never parse native command output -- check `$LASTEXITCODE` only
3. For command existence checks, use `Get-Command -ErrorAction SilentlyContinue`

## 3.6 Interactive Input Detection

```powershell
$canPrompt = $false
try { $canPrompt = -not [Console]::IsInputRedirected } catch {}
```

Returns `$false` for `irm | iex` (stdin is the script), CI, and headless modes. Combine with `$Headless` switch:

```powershell
if ($canPrompt -and -not $Headless) {
    $raw = Read-Host "Install directory [$default]"
}
```

## 3.7 Variable Naming

| Pattern | Example | Usage |
|---------|---------|-------|
| `$PascalCase` | `$InstallDir` | All script-level variables |
| `${ESC}` | `${ESC}[38;2;...` | ANSI escape (curly braces required) |
| `$env:VAR` | `$env:USERPROFILE` | Environment variables |
| `Verb-Noun` functions | `Write-Ok`, `Find-Python` | PS naming convention |

## 3.8 String Quoting

| Syntax | Behaviour | Use when |
|--------|-----------|----------|
| `"double"` | Interpolates `$var` and `` `n `` | Contains variables |
| `'single'` | Literal | Contains `$` literally, Python code |
| `` `n `` | Newline | Multi-line error messages |
| `` `" `` | Escaped quote | Path in output text |

Python code in PS double-quoted strings is safe: `f'{sys.version_info.major}'` -- PS only interpolates `$`.

## 3.9 Path Handling

```powershell
$InstallDir = [IO.Path]::GetFullPath($InstallDir)    # normalise

# ~ expansion (PS doesn't expand ~ in -File mode)
if ($InstallDir -eq "~") { $InstallDir = $env:USERPROFILE }
elseif ($InstallDir.StartsWith("~\")) {
    $InstallDir = Join-Path $env:USERPROFILE $InstallDir.Substring(2)
}

# Always use Join-Path (never string concatenation with \)
$VenvDir = Join-Path $InstallDir ".venv"
```

## 3.10 Process Management

```powershell
# Start-Process is the ONLY reliable way on Windows
$proc = Start-Process -FilePath $exe -ArgumentList "arg1","arg2" `
    -PassThru -WindowStyle Hidden
Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
```

**Never use** `subprocess.Popen` from Python (has `__main__.py`, pip cache issues), `Start-Job` (separate session), or `&` (not backgrounding in PS).

## 3.11 User PATH Registration

```powershell
# Read current
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")

# Persist (registry, survives reboots)
[Environment]::SetEnvironmentVariable("Path", "$userPath;$ScriptsDir", "User")

# Current session
$env:Path = "$ScriptsDir;$env:Path"
```

CMD session refresh is handled by `install.cmd` (see 2.2).

## 3.12 Execution Policy

```powershell
$policy = Get-ExecutionPolicy
if ($policy -eq "Restricted" -or $policy -eq "AllSigned") {
    try {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process -Force
    } catch {
        Write-Fail "Cannot set execution policy."
    }
}
```

`-Scope Process` only affects the current session (no admin needed). `install.cmd` also passes `-ExecutionPolicy Bypass`.

## 3.13 `-File` vs `iex` Differences

| Behaviour | `-File` (CMD path) | `irm \| iex` (PS direct) |
|-----------|-------------------|--------------------------|
| Encoding | Win-1252 (no BOM) / UTF-8 (BOM) | UTF-8 (HTTP) |
| `$PSScriptRoot` | Set | Empty |
| `param()` block | Parsed, args from CLI | Ignored (piped string) |
| `[Console]::IsInputRedirected` | `$false` (can prompt) | `$true` (stdin = script) |

For params with `irm`, users must use scriptblock form:
```powershell
& ([scriptblock]::Create((irm https://...install.ps1))) -InstallDir C:\custom
```

---

# Part 4: Terminal-Specific (install.sh / setup.sh)

Bash installer scripts for macOS and Linux.

## 4.1 Shebang and Strict Mode

```bash
#!/usr/bin/env bash
set -euo pipefail
```

- `set -e`: exit on error (but unreliable with pipes/subshells -- see 4.3)
- `set -u`: error on undefined variables
- `set -o pipefail`: pipeline fails if any command fails
- **`#!/usr/bin/env bash`** not `#!/bin/bash`: the latter doesn't exist on some systems (NixOS, some containers)

## 4.2 Standard Helper Functions

```bash
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
    BOLD='\033[1m'; GREEN='\033[38;2;0;229;180m'
    RED='\033[38;2;230;57;70m'; MUTED='\033[38;2;110;120;148m'; NC='\033[0m'
else
    BOLD='' GREEN='' RED='' MUTED='' NC=''
fi

ok()   { echo -e "${GREEN}+${NC}  $*"; }
info() { echo -e "${MUTED}.${NC}  $*"; }
fail() { echo -e "${RED}x${NC}  $*" >&2; exit 1; }
```

**Key differences from PS:**
- Colors use `\033[` (octal ESC), not `[char]0x1b`
- `NO_COLOR` env var support (https://no-color.org)
- Terminal detection via `[[ -t 1 ]]` (is stdout a tty?)

## 4.3 Exit Code Checking

`set -e` does NOT catch failures in:
- Commands before `||` or `&&`
- Commands in `if` conditions
- Subshells in some contexts

**Rule: Use explicit `|| fail "..."` after every critical external command.**

```bash
git clone --depth=1 "$url" "$dir" --quiet \
    || fail "git clone failed."

"$PYTHON" -m venv "$VENV_DIR" \
    || fail "Failed to create virtual environment."

"$VENV_PIP" install -e "$dir[extras]" --quiet \
    || fail "Package install failed."
```

## 4.4 macOS vs Linux Differences

| Feature | macOS | Linux |
|---------|-------|-------|
| Default shell | zsh (10.15+) | bash |
| `readlink -f` | Not available (BSD) | Works (GNU) |
| Python 3 | `python3` (Homebrew/Xcode) | `python3` (system) |
| `~/.local/bin` | Not in PATH by default | Usually in PATH |
| RC files | `~/.zprofile` or `~/.zshrc` | `~/.bashrc` |

### readlink -f Workaround

`readlink -f` is GNU-specific. On macOS it fails. Use with `|| true` fallback:

```bash
target="$(readlink -f "$link" 2>/dev/null || true)"
```

Or use a portable alternative:
```bash
normalise_path() {
    local p="$1"
    # Handle ~ prefix
    if [[ "$p" == "~/"* ]]; then p="$HOME/${p:2}"; fi
    if [[ "$p" == "~" ]]; then p="$HOME"; fi
    # Resolve to absolute path
    mkdir -p "$(dirname "$p")" 2>/dev/null || true
    cd "$(dirname "$p")" && echo "$(pwd)/$(basename "$p")"
}
```

### Shell RC File Detection

```bash
detect_rc_file() {
    local shell_name="$(basename "${SHELL:-bash}")"
    case "$shell_name" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash)
            if [[ "$(uname)" == "Darwin" ]]; then echo "$HOME/.bash_profile"
            elif [[ -f "$HOME/.bashrc" ]]; then echo "$HOME/.bashrc"
            else echo "$HOME/.bash_profile"
            fi ;;
        *)    echo "$HOME/.bashrc" ;;
    esac
}
```

## 4.5 PATH Registration on Unix

```bash
BIN_DIR="$HOME/.local/bin"

ensure_local_bin_on_path() {
    mkdir -p "$BIN_DIR"
    export PATH="$BIN_DIR:$PATH"      # current session
    hash -r 2>/dev/null || true

    # Check if already in any RC file
    for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$rc" ]] && grep -qF '.local/bin' "$rc" 2>/dev/null; then
            return 0  # already persisted
        fi
    done

    # Persist to detected RC file
    local target="$(detect_rc_file)"
    printf '\n# Added by %s installer\nexport PATH="$HOME/.local/bin:$PATH"\n' "$PROJECT" >> "$target"
}
```

**Duplicate prevention:** Check ALL RC files before appending (user may have added it to a different file).

## 4.6 Interactive Detection

```bash
can_prompt() {
    [[ -t 0 ]] && [[ -t 1 ]]   # stdin AND stdout are terminals
}

# For piped scripts (curl | bash), stdin is the script, not the terminal
# Use /dev/tty for user input:
if can_prompt; then
    IFS= read -r answer < /dev/tty
fi
```

**`curl | bash`** redirects stdin to the script content. To prompt the user, read from `/dev/tty` explicitly.

## 4.7 Platform Detection

```bash
OS="unknown"
if   [[ "$OSTYPE" == "darwin"* ]];                             then OS="macos"
elif [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]]; then OS="wsl"
elif [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "linux"* ]]; then OS="linux"
fi
```

WSL detection matters: WSL can run bash but some operations (like xdg-open, systemd) behave differently.

## 4.8 Python Version Detection

```bash
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        command -v "$cmd" >/dev/null 2>&1 || continue
        ver="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        [[ -z "$ver" ]] && continue
        maj="${ver%%.*}"; min="${ver##*.}"
        [[ "$maj" -ge 3 && "$min" -ge 11 ]] && echo "$cmd" && return 0
    done
    return 1
}
```

Check specific versions first (3.13, 3.12, 3.11) before generic `python3`. This avoids Python 3.9/3.10 on older Ubuntu/macOS.

## 4.9 Venv Paths (Unix vs Windows)

| Item | Unix | Windows |
|------|------|---------|
| Python | `.venv/bin/python` | `.venv/Scripts/python.exe` |
| pip | `.venv/bin/pip` | `.venv/Scripts/pip.exe` |
| Entry points | `.venv/bin/<name>` | `.venv/Scripts/<name>.exe` |
| PATH dir | `.venv/bin` | `.venv/Scripts` |

## 4.10 Symlink-Based CLI Registration

Unix uses symlinks in `~/.local/bin/` instead of PATH-per-venv:

```bash
ln -sf "$VENV_DIR/bin/$PROJECT" "$HOME/.local/bin/$PROJECT"
```

Check before overwriting:
```bash
if [[ -L "$link" ]]; then
    existing="$(readlink -f "$link" 2>/dev/null || true)"
    if [[ "$existing" == "$target" ]]; then
        ok "CLI already registered"
        return
    fi
fi
ln -sf "$target" "$link"
```

## 4.11 `curl | bash` and stdin Redirection (CRITICAL)

When a script runs via `curl ... | bash`, **stdin is the script content**, not the user's terminal. This has two major consequences:

1. **`sys.stdin.isatty()` returns `False` in Python subprocesses** -- any Python CLI (like `leafhub register`) that checks `isatty()` will think it's in headless mode and skip interactive prompts (provider setup, etc.)

2. **`read` in bash reads from the pipe** (the script), not the user -- use `/dev/tty`

**Rule: The parent shell script must handle ALL interactive prompts itself, not delegate to Python subprocesses.**

```bash
# WRONG -- leafhub register sees non-tty stdin, skips provider setup
"$LEAFHUB_BIN" register "$PROJECT" --path "$DIR" --alias llm

# CORRECT -- register headless, then prompt from the shell script
"$LEAFHUB_BIN" register "$PROJECT" --path "$DIR" --alias llm --headless

# Check if providers need setup
if [[ "$_has_providers" == "false" ]]; then
    printf "  Choice [1]: "
    IFS= read -r _choice < /dev/tty    # read from terminal, not pipe

    if [[ "$_choice" == "1" ]]; then
        "$LEAFHUB_BIN" manage --no-browser &    # background process
        _manage_pid=$!
        sleep 3
        open "http://localhost:8765"             # macOS
        printf "\n  Press Enter when done... "
        IFS= read -r _ < /dev/tty
        kill "$_manage_pid" 2>/dev/null
        # Re-register to pick up new provider
        "$LEAFHUB_BIN" register "$PROJECT" --path "$DIR" --alias llm --headless
    elif [[ "$_choice" == "2" ]]; then
        "$LEAFHUB_BIN" provider add < /dev/tty   # redirect tty to provider add
    fi
fi
```

### Browser Opening (Cross-Platform)

```bash
if command -v open >/dev/null 2>&1; then open "$url"            # macOS
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"  # Linux
elif command -v wslview >/dev/null 2>&1; then wslview "$url"     # WSL
fi
```

### Background Process Management

```bash
"$LEAFHUB_BIN" manage --no-browser &
_manage_pid=$!
sleep 3                                    # wait for server to start
# ... user does their thing ...
kill "$_manage_pid" 2>/dev/null || true    # graceful stop
wait "$_manage_pid" 2>/dev/null || true    # reap zombie
```

## 4.12 stdout Buffering

Python may block-buffer stdout on some terminals (especially Windows PowerShell). Long operations (LLM calls) can appear to hang because the "Done" message stays in the buffer.

**Rule: Use `flush=True` on all progress `print()` calls:**

```python
print("[project] Generating report ...", flush=True)
```

Or set unbuffered globally:
```python
import sys
sys.stdout.reconfigure(line_buffering=True)
```

---

# Part 5: LeafHub Integration for Child Projects

## 5.1 Why Two LeafHub Environments

```
Child project venv (~/leafscan/.venv/)
  |-- Has: project code + leafhub BASE package (pip install)
  |-- Can:  register, provider add/list (DB operations)
  |-- CANNOT: leafhub manage (needs ui/ directory + fastapi + npm)

Standalone LeafHub (~/leafhub/)
  |-- Has: full git clone with ui/, .venv with leafhub[manage]
  |-- Can:  EVERYTHING including Web UI
  |-- Binary: ~/leafhub/.venv/Scripts/leafhub.exe (Win)
  |           ~/leafhub/.venv/bin/leafhub (Unix)
```

**pip install does NOT include `ui/`** -- only a full git clone has the Vue frontend.

## 5.2 Decision Flow

```
Has .leafhub dotfile? ---yes---> Skip LeafHub section
         |
         no
         |
Find standalone leafhub at ~/leafhub/.venv/...
         |
    found / not found
         |         |
  $SystemLeafhub   Run leafhub one-liner installer
         |         (auto-install, then re-check)
         |              |
         +------+-------+
                |
   $LeafhubCmd = $SystemLeafhub ?? $VenvLeafhub
                |
   Register project (--headless)
                |
   Interactive? -> Show provider setup prompt
                   [1] Web UI (uses $SystemLeafhub manage)
                   [2] Terminal (uses $LeafhubCmd provider add)
                   [s] Skip
```

## 5.3 Key Variables

| Variable | Points to | Can run manage? |
|----------|-----------|----------------|
| `$VenvLeafhub` | `<project>/.venv/.../leafhub` | NO (no ui/, no fastapi) |
| `$SystemLeafhub` | `~/leafhub/.venv/.../leafhub` | YES (full clone) |
| `$LeafhubCmd` | Best available (prefer system) | Depends |

**Rule: Use `$SystemLeafhub` for `manage`. Use `$LeafhubCmd` for everything else.**

## 5.4 Shared Vault

All projects share `~/.leafhub/` (one SQLite DB):
```
~/.leafhub/          shared vault
~/leafhub/           standalone install (full clone + Web UI)
~/leafscan/          child project (.leafhub dotfile links to vault)
~/trileaf/           child project (.leafhub dotfile links to vault)
```

## 5.5 Template (PS1)

See the full template in [Leafscan/install.ps1](../../Leafscan/install.ps1) lines 161-267. Replace:
- `leafscan` -> project slug
- `llm` -> leafhub alias

## 5.6 Template (Bash)

LeafHub integration in bash `setup.sh` uses a three-tier resolution:

```bash
# 1. Try system leafhub binary
# 2. Try leafhub_dist/register.sh (distributed copy)
# 3. Curl from GitHub (first-time bootstrap)
```

See [Leafscan/setup.sh](../../Leafscan/setup.sh) lines 241-277.

---

# Part 6: CI Pipeline Standard

## 6.1 Required Jobs

```yaml
jobs:
  test:
    # Matrix: ubuntu-latest, macos-latest, windows-latest
    # Matrix: Python 3.11, 3.12
    steps: [checkout, setup-python, pip install, pytest]

  lint:
    runs-on: ubuntu-latest
    steps:
      - Python syntax check (py_compile)
      - Shell script syntax (bash -n)
      - PS1 ASCII enforcement (grep non-ASCII)

  ps1-syntax:
    runs-on: windows-latest
    steps:
      - PowerShell parser validation
```

## 6.2 PS1 Syntax Check

```yaml
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

## 6.3 ASCII Enforcement

```yaml
- name: PS1 ASCII enforcement
  run: |
    for f in install.ps1 setup.ps1; do
      [ -f "$f" ] || continue
      if grep -Pq '[^\x00-\x7F]' "$f"; then
        echo "ERROR: $f contains non-ASCII characters"
        grep -Pn '[^\x00-\x7F]' "$f"
        exit 1
      fi
    done
```

## 6.4 What CI Does NOT Catch

| Risk | Why | Mitigation |
|------|-----|------------|
| Win-1252 encoding | PS parser runs in pwsh (7+) | ASCII enforcement |
| Native stderr + ErrorActionPreference | Syntax check only | Manual testing |
| File locks (antivirus) | CI has clean env | git init+fetch+reset strategy |
| pip cache | CI uses fresh runners | `--no-cache-dir` in scripts |
| CDN cache | CI doesn't test one-liner | Wait 5 min after push |

---

# Part 7: New Project Checklist

## 7.1 Copy Template Files

From LeafScan: `install.cmd`, `install.ps1`, `install.sh`, `setup.sh`

## 7.2 Search-Replace

| Find | Replace |
|------|---------|
| `leafscan` | `newproject` |
| `LeafScan` | `NewProject` |
| `Rebas9512/Leafscan` | `Rebas9512/NewProject` |
| `LEAFSCAN_DIR` | `NEWPROJECT_DIR` |
| alias `llm` | project's alias |
| Playwright step | project-specific deps |
| CLI hint commands | actual commands |

## 7.3 Verify

```bash
# ASCII check
grep -P '[^\x00-\x7F]' install.ps1 setup.ps1 && echo "FAIL" || echo "OK"

# Shell syntax
bash -n install.sh && bash -n setup.sh

# Python syntax
python -m py_compile <project>/cli.py
```

## 7.4 Test Matrix

| Scenario | Verify |
|----------|--------|
| Fresh install (CMD) | Full flow, CLI in same terminal |
| Fresh install (irm \| iex) | Full flow |
| Fresh install (curl \| bash) | Full flow; provider prompt via /dev/tty; Web UI starts |
| Re-install over existing | .leafhub preserved, no re-prompt |
| File at install path | Removed, install succeeds |
| Without leafhub | Auto-install triggered |
| With leafhub already | Web UI works |
| Headless / CI | No prompts |

---

# Appendix: Session Log 2026-03-23

| # | Issue | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | PS1 parse errors | `` `e `` ANSI (PS 7+ only) | `$ESC = [char]0x1b` |
| 2 | PS1 parse errors persist | Em dash byte 0x94 = `"` in Win-1252 | Pure ASCII |
| 3 | CLI not recognised | CMD PATH stale | `reg query` refresh |
| 4 | npm fails [WinError 2] | npm = npm.cmd on Windows | `shell=True` on win32 |
| 5 | start_new_session fails | POSIX-only param | `CREATE_NEW_PROCESS_GROUP` |
| 6 | install.ps1 -> setup.sh | No native Windows path | Rewrote for Windows |
| 7 | git clone ignored failure | ErrorActionPreference miss | `Assert-ExitCode` |
| 8 | Remove-Item silently fails | Windows file locks | git init+fetch+reset |
| 9 | git init mkdir error | Path is file, not dir | File-at-path guard |
| 10 | leafhub manage from pip | No ui/ in pip package | Standalone leafhub |
| 11 | python -m leafhub fails | No __main__.py | Added + shutil.which |
| 12 | No module fastapi | Venv = base pkg only | Use standalone binary |
| 13 | charmap codec error | PS 5.1 + Unicode stderr | Remove verification |
| 14 | .leafhub deleted | git clean -fdx | Changed to -fd |
| 15 | leafhub uninstall crash | Missing import os | Added import |
| 16 | Setup repeats | .leafhub destroyed | Fixed by #14 |
| 17 | pip cached old code | Wheel cache by URL | --no-cache-dir |
| 18 | macOS: provider setup skipped | `curl\|bash` stdin = pipe, `sys.stdin.isatty()` = False in leafhub | setup.sh handles prompts itself via `/dev/tty` |
| 19 | macOS: wrong CLI hint | `leafscan run` doesn't exist | Changed to `leafscan scan <url>` |
| 20 | macOS: stdout buffered after LLM call | Python block-buffers on some terminals | `flush=True` on all print() |

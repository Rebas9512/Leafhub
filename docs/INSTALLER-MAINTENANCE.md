# Leaf Projects -- Installer & Distribution Maintenance Guide

Cross-project reference for maintaining the install scripts (`install.cmd`, `install.ps1`, `install.sh`) and CI pipelines across LeafHub, LeafScan, Trileaf, and future Leaf projects.

---

## Distribution Architecture

```
User (Windows CMD)
  curl ... install.cmd -o install.cmd && install.cmd
    |
    v
install.cmd  (batch bootstrap)
    |-- Downloads install.ps1 via Invoke-WebRequest
    |-- Runs: powershell -File install.ps1
    |-- On success: refreshes PATH from registry
    v
install.ps1  (main installer logic)
    |-- Clone / sync repo
    |-- Find Python, create venv
    |-- pip install project + deps
    |-- [project-specific: LeafHub setup, Playwright, etc.]
    |-- Update user PATH
    v
Done -- CLI usable immediately

User (PowerShell)
  irm .../install.ps1 | iex       # direct, no CMD wrapper

User (macOS / Linux)
  curl ... install.sh | bash       # shell equivalent
```

### File Roles

| File | Platform | Role |
|------|----------|------|
| `install.cmd` | Windows CMD | Thin bootstrap: downloads `install.ps1`, runs it via `powershell.exe`, refreshes PATH after |
| `install.ps1` | Windows (PS 5.1+) | Full installer logic. Must work under both `powershell.exe -File` (5.1) and `pwsh` / `irm \| iex` (7+) |
| `install.sh` | macOS / Linux | Full installer logic (bash). Delegates to `setup.sh` or handles setup inline |

---

## Known Constraints & Pitfalls

### 1. PowerShell 5.1 Encoding (CRITICAL)

**`install.cmd` invokes `powershell.exe` which is PowerShell 5.1.**
PS 5.1 reads files without BOM as **Windows-1252 (ANSI)**, not UTF-8.

**Rule: All `.ps1` files must be pure ASCII.**

| UTF-8 Character | Byte Sequence | Win-1252 Interpretation | Impact |
|---|---|---|---|
| `--` (em dash U+2014) | `E2 80 94` | `a` `euro` **`"`** | **String terminator -- breaks all parsing** |
| `--` (en dash U+2013) | `E2 80 93` | `a` `euro` **`"`** | **Left double quote -- same** |
| `-` (box drawing U+2500) | `E2 94 80` | `a` **`"`** `euro` | **Right double quote -- same** |
| `+` (checkmark U+221A) | `E2 88 9A` | `a` `^` `s` | Garbled but safe |
| `.` (middle dot U+00B7) | `C2 B7` | `A` `.` | Garbled but safe |

**Replacements used:**
- `--` (em/en dash) -> `--`
- `-` (box drawing) -> `-`
- `+` (checkmark) -> `+`
- `.` (middle dot) -> `.`
- `x` (cross mark) -> `x`

### 2. PowerShell 5.1 ANSI Escapes

**`` `e `` (backtick-e) is PowerShell 7+ only.**
PS 5.1 interprets `` `e `` as literal `e`, corrupting ANSI color codes and causing cascade parse errors.

```powershell
# WRONG (PS 7+ only)
$GREEN = "`e[38;2;0;229;180m"

# CORRECT (PS 5.1+)
$ESC = [char]0x1b
$GREEN = "${ESC}[38;2;0;229;180m"
```

### 3. External Command Exit Codes

`$ErrorActionPreference = "Stop"` only catches **PowerShell cmdlet** errors, NOT external command failures (`git`, `python`, `pip`).

```powershell
# WRONG -- git failure is silently ignored
git clone --depth=1 $url $dir --quiet
Write-Ok "Cloned."  # runs even if git failed

# CORRECT
git clone --depth=1 $url $dir --quiet
Assert-ExitCode "git clone failed"
Write-Ok "Cloned."
```

Standard helper:
```powershell
function Assert-ExitCode($msg) {
    if ($LASTEXITCODE -ne 0) { Write-Fail "$msg (exit code $LASTEXITCODE)" }
}
```

### 4. Native Command Stderr + ErrorActionPreference

PS 5.1 with `$ErrorActionPreference = "Stop"` converts ANY native command stderr to a **terminating error** -- even with `2>$null` and `try/catch`. This breaks commands that write informational messages to stderr (e.g., `leafhub provider list` with Unicode table output).

**Rule: Never capture native command output when it may contain non-ASCII. Redirect to `>$null` or avoid entirely.**

### 5. git clone vs Existing Directories

Windows has many edge cases with existing install directories:
- Files locked by antivirus/Explorer/IDE -> `Remove-Item` fails silently
- File (not directory) at the install path -> `git init` fails with "cannot mkdir"
- Shallow clone `.git` may behave differently with `Test-Path`

**Robust three-way strategy:**
```powershell
$hasGit = Test-Path (Join-Path $InstallDir ".git")
if (-not $hasGit -and -not (Test-Path $InstallDir)) {
    # Fresh install -> git clone
} else {
    # Dir exists (any state) -> git init + fetch + reset --hard
    if (-not $hasGit) {
        git -C $InstallDir init --quiet          # NOT: git init $InstallDir
        git -C $InstallDir remote add origin $url 2>$null
    }
    git -C $InstallDir fetch origin --depth=1 --quiet
    git -C $InstallDir reset --hard "origin/$branch" --quiet
    git -C $InstallDir clean -fd --quiet 2>$null  # -fd NOT -fdx
}
```

Key points:
- `git init <dir>` tries to mkdir -> fails if dir exists. Use `git -C <dir> init`
- `git clean -fd` (not `-fdx`): preserve gitignored files (`.leafhub`, `.venv`)
- File at install path: check with `Test-Path -PathType Container` and remove if file
- Never depend on `Remove-Item -Recurse` for directories (file locks on Windows)

### 6. PATH Refresh in CMD

After `install.ps1` writes to user PATH, the parent CMD session still has the old PATH. The `install.cmd` refreshes it:

```cmd
rem Refresh PATH from registry so CLI is usable immediately
if %EXITCODE% equ 0 (
    endlocal
    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "PATH=%%b;%PATH%"
)
```

For `irm | iex` (PowerShell direct), `install.ps1` sets `$env:Path` in the current session. Add a hint message for users who may open a new terminal:
```
If the command is not recognised, open a new terminal first.
```

### 7. npm/Node Subprocess on Windows

`subprocess.run(["npm", "install"])` fails on Windows because `npm` is `npm.cmd` (a batch file). External commands that are batch wrappers need `shell=True`:

```python
_win = sys.platform == "win32"
subprocess.run(["npm", "install"], cwd=ui_dir, check=True, shell=_win)
```

### 8. LeafHub Web UI from pip Package

`leafhub manage` requires the `ui/` directory (Vue frontend) which only exists in a **full git clone**. A `pip install leafhub` package does NOT include `ui/`.

**For child projects (leafscan, trileaf):**
- Use the standalone leafhub installation (`~/leafhub/.venv/Scripts/leafhub`) for `manage`
- Use either standalone or venv leafhub for `register`, `provider add`, etc.
- If no standalone leafhub exists, auto-install via the leafhub one-liner
- Fall back to terminal mode if Web UI is unavailable

### 9. GitHub raw.githubusercontent.com CDN Caching

CDN caches files for ~5 minutes (varies by region). After pushing:
- Wait 2-5 minutes before testing, OR
- Use `curl -H "Cache-Control: no-cache"` to bypass edge cache
- Windows `Invoke-WebRequest` may also cache; `install.cmd` uses a unique temp filename

### 10. pip Caching with Git URLs

`pip install "pkg @ git+https://..."` aggressively caches built wheels. A fresh venv + same git URL may install stale code.

**When freshness matters:**
```powershell
& $VenvPip install "leafhub[manage] @ git+https://..." --upgrade --no-cache-dir --quiet
```

---

## CI Pipeline Checklist

### Required Jobs (all projects)

```yaml
jobs:
  test:
    # Matrix: ubuntu-latest, macos-latest, windows-latest
    # Matrix: Python 3.11, 3.12 (or project minimum)
    steps:
      - checkout
      - setup-python
      - pip install + run tests

  lint:
    # ubuntu-latest only
    steps:
      - Python syntax check (py_compile)
      - Shell script syntax (bash -n install.sh, bash -n setup.sh)

  ps1-syntax:
    # windows-latest only
    steps:
      - PowerShell parser validation for ALL .ps1 files
```

### PS1 Syntax Check Template

```yaml
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

**Note:** The PS parser validates syntax but NOT runtime behavior. It will NOT catch:
- `$ErrorActionPreference` interactions with native commands
- Missing `os` imports inside functions
- Encoding issues (those are byte-level, not syntax)

### Recommended Additional Checks

- **ASCII enforcement for PS1 files** (add to lint job):
  ```bash
  if grep -Pq '[^\x00-\x7F]' install.ps1; then
    echo "ERROR: install.ps1 contains non-ASCII characters"
    exit 1
  fi
  ```

- **Shell script executable bit** (for install.sh, setup.sh):
  ```bash
  mode=$(git ls-files -s setup.sh | awk '{print $1}')
  [ "$mode" = "100755" ] || exit 1
  ```

---

## New Project Checklist

When creating a new Leaf project with install scripts:

1. **Copy template files** from an existing project (e.g., LeafScan):
   - `install.cmd`, `install.ps1`, `install.sh` (+ `setup.sh` if needed)

2. **Search-replace** project-specific values:
   - Project name (lowercase): `leafscan` -> `newproject`
   - GitHub repo URL: `Rebas9512/Leafscan` -> `Rebas9512/NewProject`
   - Python version requirement
   - Package extras (e.g., `[leafhub]`, `[manage]`)
   - CLI commands in hint text

3. **Verify all PS1 files are pure ASCII:**
   ```bash
   grep -P '[^\x00-\x7F]' install.ps1 && echo "FAIL" || echo "OK"
   ```

4. **Add CI jobs** -- copy the `ps1-syntax` job and adapt the test matrix

5. **Test the full install flow** on a clean Windows machine:
   - CMD: `curl ... install.cmd -o install.cmd && install.cmd && del install.cmd`
   - PowerShell: `irm ... | iex`
   - Verify CLI works in the same terminal (PATH refresh)
   - Verify re-install over existing directory works
   - Verify `.leafhub` / config files survive re-install

---

## Session Log: 2026-03-23 Windows Install Fixes

### Issues Found & Fixed

| # | Issue | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | PS1 parse errors (`TerminatorExpectedAtEndOfString`) | `` `e `` ANSI escape (PS 7+ only) | Replace with `$ESC = [char]0x1b` |
| 2 | PS1 parse errors persist after `e fix | Em dash `--` UTF-8 byte `0x94` = `"` in Win-1252 | Make all PS1 files pure ASCII |
| 3 | `leafscan` not recognised after install | CMD PATH not refreshed after PS1 sets user PATH | `reg query` PATH refresh in `install.cmd` |
| 4 | `npm` subprocess fails (`[WinError 2]`) | `npm` is `npm.cmd` on Windows | Add `shell=True` on `sys.platform == "win32"` |
| 5 | `start_new_session` fails on Windows | POSIX-only parameter | Use `CREATE_NEW_PROCESS_GROUP` on Windows |
| 6 | Leafscan `install.ps1` delegates to `setup.sh` | No native Windows setup path | Rewrote to handle venv/pip/playwright natively |
| 7 | `git clone` fails on existing directory | `$ErrorActionPreference` doesn't catch external cmd | Added `Assert-ExitCode` after all external commands |
| 8 | `git clone` fails, `Remove-Item` can't help | Windows file locks prevent deletion | Replaced delete+clone with `git init` + `fetch` + `reset --hard` |
| 9 | `git init <dir>` fails ("cannot mkdir") | Path exists as file, not directory | Check `Test-Path -PathType Container`, remove file if needed; use `git -C <dir> init` |
| 10 | `leafhub manage` fails from pip install | `ui/` directory only in git clone, not pip package | Use standalone leafhub installation for Web UI |
| 11 | `python -m leafhub` fails | No `__main__.py` | Added `__main__.py` + use `shutil.which("leafhub")` |
| 12 | `No module named 'fastapi'` in register | Venv leafhub is base package only | Use standalone leafhub binary for all operations |
| 13 | `charmap` codec error on provider list | PS 5.1 can't encode Unicode table output from leafhub | Removed provider list verification entirely |
| 14 | `.leafhub` deleted on re-install | `git clean -fdx` removes gitignored files | Changed to `git clean -fd` (no `-x`) |
| 15 | `leafhub uninstall` crashes | `os` not imported in `_remove_leafhub_self` | Added `import os` |
| 16 | Setup prompt repeats on every re-install | `.leafhub` deleted by `git clean -fdx` | Fixed by #14 + check `.leafhub` existence |
| 17 | pip installs cached old leafhub | pip wheel cache keyed by git URL | Added `--upgrade --no-cache-dir` |

# LeafHub

[![CI](https://github.com/Rebas9512/Leafhub/actions/workflows/ci.yml/badge.svg)](https://github.com/Rebas9512/Leafhub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A local encrypted API key vault for LLM projects. Store provider credentials once, reference them by alias across all your projects — no plaintext keys in `.env` files, no manual copy-paste across repos.

Projects **auto-detect** their credentials on startup via a `.leafhub` dotfile that LeafHub writes when you link a directory. No token management in application code.

---

## Overview

LeafHub manages the gap between your LLM provider credentials and your application code. Each project gets a Bearer token; your code calls `hub.get_key("openai")` and gets back the decrypted API key at runtime. Keys are AES-256-GCM encrypted on disk; the master key lives in the system keychain when available.

When you link a project directory in the Manage UI or CLI, LeafHub writes a `.leafhub` dotfile into that directory. On next startup the SDK finds it automatically — no token in your code, no `.env` entry needed.

```
Manage UI / CLI                  LeafHub                    Your Project
      │                              │                            │
      │  leafhub register my-app     │                            │
      │────────────────────────────► │                            │
      │                              │  write .leafhub (chmod 600)│
      │                              │ ──────────────────────────►│
      │                              │  (new project) distribute  │
      │                              │  leafhub_dist/ module      │
      │                              │ ──────────────────────────►│
      │                              │                            │
      │                              │     Next startup           │
      │                              │◄───────────────────────────│
      │                              │  detect() → open_sdk()     │
      │                              │  → reads .leafhub token    │
      │                              │  → returns API key         │
      │                              │ ──────────────────────────►│
```

---

## Getting Started

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | `brew install python@3.12` · `sudo apt install python3.12 python3.12-venv` · [python.org](https://www.python.org/downloads/) |
| Git | any | Required — the installer clones the repo |
| Node.js | 18+ | Optional — only needed to rebuild the Web UI from source |

---

### macOS / Linux / WSL

```bash
curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh | bash
```

The installer prompts for an install directory (default: `~/leafhub`), clones the repo, creates a virtual environment, and registers `leafhub` on your PATH.

**Options** (set before the pipe):

| Variable | Effect |
|---|---|
| `LEAFHUB_DIR=~/tools/leafhub` | Custom install directory |
| `NO_COLOR=1` | Disable colour output |

```bash
LEAFHUB_DIR=~/tools/leafhub curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh | bash
```

---

### Windows — PowerShell

```powershell
irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1 | iex
```

To pass options, use the scriptblock form:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1))) -InstallDir C:\leafhub
```

---

### Windows — CMD

```cmd
curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.cmd -o install.cmd && install.cmd && del install.cmd
```

`install.cmd` downloads `install.ps1` from GitHub and executes it via PowerShell.

---

### What gets installed

| Location | Contents |
|---|---|
| `~/leafhub/` | Source code (cloned repo, configurable via `LEAFHUB_DIR`) |
| `~/leafhub/.venv/` | Isolated Python environment with all dependencies |
| `~/.local/bin/leafhub` | CLI symlink (macOS / Linux / WSL) |
| `~/leafhub/.venv\Scripts\leafhub.exe` on user `PATH` | CLI entry point (Windows) |
| `~/.leafhub/` | Encrypted key store (`providers.enc`), SQLite DB, master key |

---

### After install

Open a **new terminal** (PATH update takes effect on next launch), then:

| Command | What it does |
|---|---|
| `leafhub --help` | Verify the install and see all commands |
| `leafhub provider add` | Register your first API key |
| `leafhub project create my-app` | Create a project and get a one-time token |
| `leafhub manage` | Start the Web UI at `http://localhost:8765` |

---

### Update

```bash
git -C ~/leafhub pull
```

The editable install (`pip install -e`) means updates take effect immediately — no reinstall needed.

---

### Manual install (pip)

If you prefer full control over the environment:

```bash
git clone https://github.com/Rebas9512/Leafhub.git
cd Leafhub
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[manage]'
```

To also build the Web UI from source (pre-built UI ships in `ui/dist/`):

```bash
cd ui && npm install && npm run build && cd ..
```

---

### `setup.sh` — developer / CI usage

`setup.sh` runs from a cloned repo and gives more explicit control. Use it for CI or local development:

```bash
./setup.sh [flags]
```

| Flag | Description |
|---|---|
| `--reinstall` | Delete and recreate `.venv` (force clean install) |
| `--headless` | Non-interactive / CI mode — no prompts |
| `--doctor` | Run environment diagnostics only, then exit |
| `--uninstall` | Remove the CLI symlink, PATH entries, and `.venv` |

---

### Uninstall

**macOS / Linux / WSL:**

```bash
rm ~/.local/bin/leafhub
rm -rf ~/leafhub/               # remove install dir
rm -rf ~/.leafhub/              # also delete stored keys and DB (optional)
```

**Windows:**

```powershell
# Remove install dir and PATH entry — replace path if you chose a custom dir
$scripts = "$env:USERPROFILE\leafhub\.venv\Scripts"
[Environment]::SetEnvironmentVariable("Path",
  ([Environment]::GetEnvironmentVariable("Path","User") -split ";" |
   Where-Object { $_ -ne $scripts }) -join ";", "User")
Remove-Item -Recurse "$env:USERPROFILE\leafhub"
Remove-Item -Recurse $env:USERPROFILE\.leafhub   # also delete stored keys (optional)
```

`~/.leafhub/` (your encrypted keys and project tokens) is **not** removed unless you delete it manually.

---

## Abstract Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         cli.py                              │
│  provider / project / manage / register / shell-helper      │
│   register  <name> --path <dir>  [--headless]               │
│   project create   --yes --if-not-exists                    │
│   provider list    --json                                    │
│   status           --json                                    │
└───────────┬─────────────────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │               probe.py                        │
    │   LeafHub auto-detection (stdlib only)        │
    │   detect() → ProbeResult                      │
    │   Distributed as leafhub_dist/probe.py        │
    └───────┬──────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │                  sdk.py                       │
    │   LeafHub — runtime key access                │
    │   get_key / get_config / from_directory()     │
    └───────┬──────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │               Core Layer                      │
    │   store.py         │   crypto.py              │
    │   (SQLite CRUD)    │   (AES-256-GCM)          │
    │   db.py            │   errors.py              │
    │   (schema, WAL)    │   (typed exceptions)     │
    └───────┬──────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │          Manage Layer (optional)              │
    │   server.py        │   auth.py                │
    │   (FastAPI app)    │   (admin token gate)     │
    │   providers.py     │   projects.py            │
    │   (probe on create)│   (smart distribution)   │
    └───────┬──────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │          Vue 3 + Vite Web UI                  │
    │   provider presets │ connectivity test        │
    │   project linking  │ token display modal      │
    │   same-name badge  │ delete cleanup display   │
    └──────────────────────────────────────────────┘
```

---

## Module Reference

### `src/leafhub/` — Python Package

| File | Responsibility |
|------|----------------|
| `cli.py` | argparse CLI. Subcommands: `provider add/list/show/delete`, `project create/link/list/show/token/bind/unbind/delete`, `register`, `shell-helper`, `manage`, `status`. `register` includes a CLI detection step: after linking, it scans `.venv/bin/` for project executables not yet in `~/.local/bin/` and prompts to register them (auto-registers in headless/non-TTY mode). After `project create` and `project link`, an interactive binding wizard prompts the user to bind an existing provider or add a new one inline. Silently skipped in non-TTY environments. `manage` automatically frees the target port if already in use before starting the server. |
| `sdk.py` | `LeafHub` — runtime client for application code. Resolves the hub directory, verifies the project token, decrypts API keys, builds provider-specific auth headers. `from_directory()` auto-loads from a `.leafhub` dotfile. |
| `probe.py` | Stdlib-only auto-detection module. `detect()` searches for a `.leafhub` dotfile, probes the manage server port, checks for the CLI binary and SDK. Returns a `ProbeResult` with convenience properties (`ready`, `can_link`, `open_sdk()`). Distributed as `leafhub_dist/probe.py` when a project is linked for the first time. |
| `register.sh` | Shell integration module (see **Project Integration Standard** below). Bundled into the Python package so `leafhub shell-helper` can output it without a network call. |
| `errors.py` | Typed exception hierarchy: `LeafHubError`, `StorageNotFoundError`, `InvalidTokenError`, `AliasNotBoundError`, `DecryptionError`. |

---

### `src/leafhub/core/` — Foundation Layer

| File | Responsibility |
|------|----------------|
| `db.py` | Opens the SQLite connection, runs schema migrations idempotently, applies WAL mode and PRAGMA tuning. |
| `crypto.py` | AES-256-GCM encryption for provider API keys stored in `providers.enc`. Master key resolved from env → system keychain → `~/.leafhub/.masterkey`, auto-generated on first run. PBKDF2-SHA256 (600,000 iterations) for key derivation. Fresh salt and nonce per write. |
| `store.py` | `SyncStore` — all CRUD operations against SQLite. Manages providers, projects (same-name allowed), project tokens (SHA-256 hash only), model alias bindings. |

---

### `src/leafhub/manage/` — Web Management Layer (optional)

Requires `pip install 'leafhub[manage]'`.

| File | Responsibility |
|------|----------------|
| `server.py` | FastAPI app factory. Lifespan hooks load the master key and open SQLite. Serves the compiled Vue UI from `ui/dist/`. Exposes `GET /health` and `GET /admin/status`. |
| `auth.py` | Admin token middleware. Reads `LEAFHUB_ADMIN_TOKEN` from environment; all `/admin/*` routes require a matching Bearer token with constant-time comparison. Per-IP sliding-window rate limiter (5 failures → 5-minute lockout). |
| `providers.py` | CRUD routes for providers. **Connectivity probe on create**: `POST /admin/providers` makes a GET request to the provider's endpoint before saving. Returns HTTP 422 with a diagnostic message if unreachable. |
| `projects.py` | CRUD routes for projects. Token lifecycle: create (plaintext shown once), rotate, revoke. `POST /admin/projects/{id}/link` and `POST /admin/projects` (with `path`) rotate/create the token, write a `.leafhub` dotfile (chmod 600), auto-distribute the `leafhub_dist/` integration module to new project directories (v2 standard, 2026-03-21), and **auto-register project CLI tools**: any executable in `.venv/bin/` that is not a standard Python/pip tool and not yet symlinked in `~/.local/bin/` gets a symlink created automatically. Response includes `"cli_registered": [...]` when symlinks were created. Directories that already contain `leafhub_dist/` (or root-level `register.sh` for v1 projects) are treated as already integrated — only the dotfile is updated. `DELETE /admin/projects/{id}` performs a full clean-up: removes `.leafhub` and `leafhub_dist/` from the linked directory (also removes legacy v1 files `leafhub_probe.py` and `register.sh` if present), removes CLI symlinks in `~/.local/bin/` pointing into the project, and strips the project's venv PATH entries from shell RC files (macOS/Linux) or the User PATH registry key (Windows). Returns `{"deleted": true, "files_removed": [...], "registration_removed": [...]}`. |

---

## Project Integration Standard

This section documents the standard way to integrate LeafHub into a new project. The goal is minimum user effort: one `setup.sh` step handles everything — install, registration, and provider binding.

### The pattern: `leafhub_dist/register.sh`

On first registration, LeafHub writes a `leafhub_dist/` directory into the project root. This directory contains the `leafhub_setup_project()` shell function and the Python detection module — everything the project needs to integrate with LeafHub, with no runtime network dependency.

**Checklist for a new project (do once):**

| Step | Where | What |
|---|---|---|
| 1. Declare pip dependency | `pyproject.toml` | Add `leafhub` to optional-dependencies (see below) |
| 2. Install in venv | `setup.sh` | `"$VENV_PIP" install -e "$SCRIPT_DIR[leafhub]" --quiet` |
| 3. Source registration block | `setup.sh` | 4-tier block below — runs `leafhub register` |
| 4. Add credential resolution | startup code | `detect()` → `open_sdk()` → `hub.get_key("alias")` |

```toml
# pyproject.toml — add leafhub as an optional dependency
[project.optional-dependencies]
leafhub = ["leafhub @ git+https://github.com/Rebas9512/Leafhub.git"]
```

**Integration block for your project's `setup.sh` (v2 standard, 2026-03-21):**

```bash
# ── LeafHub integration ───────────────────────────────────────────────────────
# Resolution order — stops at the first successful source:
#   1. leafhub shell-helper        — system PATH binary (fast path, offline)
#   2. $VENV_DIR/bin/leafhub       — pip-installed in venv (optional; omit if
#                                    leafhub is not a pip dep of your project)
#   3. leafhub_dist/register.sh    — local distributed copy (offline fallback)
#   4. GitHub curl                 — first-time bootstrap, network required
#
# NOTE: `eval "$(cmd)"` is NOT used — eval "" always exits 0, making the
# fallback unreachable when the binary is absent from PATH.
_lh_content=""
if _lh_content="$(leafhub shell-helper 2>/dev/null)" && [[ -n "$_lh_content" ]]; then
    eval "$_lh_content"
elif [[ -f "$SCRIPT_DIR/leafhub_dist/register.sh" ]]; then
    source "$SCRIPT_DIR/leafhub_dist/register.sh"
else
    _TMP_REG="$(mktemp)"
    if ! curl -fsSL \
            https://raw.githubusercontent.com/Rebas9512/Leafhub/main/register.sh \
            -o "$_TMP_REG" 2>/dev/null; then
        rm -f "$_TMP_REG"
        fail "Could not fetch LeafHub installer."
    fi
    source "$_TMP_REG"
    rm -f "$_TMP_REG"
fi
unset _lh_content
leafhub_setup_project "my-project-name" "$SCRIPT_DIR" "my-alias" \
    || fail "LeafHub registration failed."
```

**How the sourcing works:**

| Scenario | Resolution path |
|---|---|
| LeafHub installed in system PATH | Tier 1: `leafhub shell-helper` outputs `register.sh` content — no network call |
| leafhub pip-installed in venv only | Tier 2 (optional): `$VENV_DIR/bin/leafhub shell-helper` — no network call |
| Already registered (leafhub not in PATH) | Tier 3: `leafhub_dist/register.sh` sourced directly — no network call |
| Clean install, no LeafHub anywhere | Tier 4: `curl` fetches `register.sh` from GitHub; `_leafhub_ensure()` installs LeafHub |
| No internet, no LeafHub at all | All tiers fail → `leafhub_setup_project` is undefined → your `fail` trap fires |

**First link vs re-link — file distribution (v2 standard, 2026-03-21):**

| Situation | Files written |
|---|---|
| First time linking a directory (no `leafhub_dist/` present) | `.leafhub` + `leafhub_dist/__init__.py` + `leafhub_dist/probe.py` + `leafhub_dist/register.sh` |
| Re-linking / token rotation (`leafhub_dist/` already exists) | `.leafhub` only — the `leafhub_dist/` directory is not overwritten |
| v1 project (root-level `register.sh` present, no `leafhub_dist/`) | `.leafhub` only — treated as already integrated; v2 layout on next explicit re-registration |

The presence of `leafhub_dist/` in the project directory is the v2 integration marker. Once a project has been set up, re-running `leafhub register` or rotating the token only refreshes the dotfile.

**What `leafhub_setup_project "my-project" "$SCRIPT_DIR"` does:**

```
1. Detect leafhub binary in PATH
   └─ If missing → curl + run LeafHub installer → reload PATH
   └─ If install fails → return 1  (fatal)

2. leafhub register my-project --path $SCRIPT_DIR
   ├─ a) Create project (or re-link if already exists — idempotent)
   │      Writes .leafhub token file (chmod 600) into $SCRIPT_DIR
   │      Auto-adds .leafhub to .gitignore
   │
   ├─ b) CLI registration (if .venv/bin/ contains project executables)
   │      Scans $SCRIPT_DIR/.venv/bin/ for project CLI tools
   │      (skips python*, pip*, activate, wheel, etc.)
   │      Interactive: asks once before creating ~/.local/bin/ symlinks
   │      Headless / non-TTY: registers automatically
   │      Already registered → skipped
   │
   ├─ c) Provider setup (if no providers configured)
   │      Interactive: opens wizard to add provider name, URL, API key, model
   │      Headless: prints reminder and continues without binding
   │
   ├─ d) Auto-bind provider
   │      1 provider  → bound automatically under the requested alias
   │      N providers → user picks one interactively
   │      Already bound → skipped
   │
   └─ e) Distribute leafhub_dist/ (new projects only, v2 standard 2026-03-21)
          If leafhub_dist/ is NOT already present in $SCRIPT_DIR:
            → write leafhub_dist/__init__.py  (Python package entrypoint)
            → write leafhub_dist/probe.py     (stdlib-only runtime detection)
            → write leafhub_dist/register.sh  (shell integration module)
          If leafhub_dist/ IS present → already integrated, skip (dotfile only)
          Legacy: root-level register.sh (v1) also counts as already integrated
```

**Three things to change per project:**

| What | Convention | Example |
|---|---|---|
| Project name | Lowercase slug matching repo name | `"trileaf"`, `"my-toolkit"` |
| Path | Directory containing `setup.sh` | `"$SCRIPT_DIR"` |
| Alias | Binding alias your runtime code uses in `hub.get_key("<alias>")` | `"rewrite"`, `"chat"`, `"default"` |

### Runtime credential resolution

After setup, the project resolves credentials at startup using the probe. Two requirements:

1. **`leafhub` pip package installed** — `open_sdk()` needs it; declare it in `pyproject.toml` and install it in `setup.sh` (see integration block above).
2. **Project root on `sys.path`** — editable installs only expose named packages; add the root explicitly before the fallback import.

```python
import sys
from pathlib import Path

# Editable installs only expose named packages — add project root so
# leafhub_dist is importable when the leafhub pip package is absent.
_root = str(Path(__file__).resolve().parent)  # adjust .parent depth as needed
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from leafhub.probe import detect        # pip package (preferred)
except ImportError:
    from leafhub_dist.probe import detect   # local distributed fallback

found = detect()                            # fast, never raises, < 1 s

if found.ready:
    hub     = found.open_sdk()              # requires leafhub pip package
    api_key = hub.get_key("my-alias")       # decrypted key string from vault
    cfg     = hub.get_config("my-alias")    # base_url, model, auth_mode, ...
    # provider-specific SDK clients:
    client  = hub.openai("my-alias")        # openai.OpenAI(api_key=..., base_url=...)
    client  = hub.anthropic("my-alias")     # anthropic.Anthropic(api_key=...)
else:
    # Not linked — fall back to env vars or show setup instructions
    api_key = os.environ.get("MY_API_KEY")
```

### Headless / CI usage

Set `LEAFHUB_HEADLESS=1` before calling `leafhub_setup_project` to skip all interactive prompts:

```bash
# In setup.sh, when --headless is passed:
[[ "$HEADLESS" == "true" ]] && export LEAFHUB_HEADLESS=1
leafhub_setup_project "my-project" "$SCRIPT_DIR"
```

In headless mode:
- If providers are already configured, binding proceeds automatically.
- If no providers are configured, a reminder is printed and binding is skipped.
- The project is still linked (`.leafhub` is written) so it can be configured later.

### Scripted usage (`--json`, `--yes`, `--if-not-exists`)

For programmatic use from CI scripts or other tools:

```bash
# Check if providers are configured (machine-readable)
leafhub status --json
# → {"providers": 2, "projects": 3, "bound_projects": 2, "ready": true}

# List configured providers as JSON
leafhub provider list --json
# → [{"name": "OpenAI", "base_url": "https://api.openai.com/v1", ...}, ...]

# Create a project non-interactively (skip binding wizard)
leafhub project create my-app --path /abs/path --yes

# Create or re-link without prompts (idempotent, safe to run repeatedly)
leafhub project create my-app --path /abs/path --if-not-exists

# Full registration flow non-interactively
leafhub register my-app --path /abs/path --headless
```

---

## Project Linking & Auto-Detection

The central workflow that removes token management from application code.

### How it works

1. **Create a project** in the Manage UI or CLI — you receive a one-time token.
2. **Link a directory** — LeafHub writes:
   - `.leafhub` — a JSON file with the project token (chmod 600, auto-added to `.gitignore`)
   - `leafhub_dist/` — integration module distributed automatically on first link (v2 standard, 2026-03-21): contains `__init__.py`, `probe.py`, and `register.sh`. Skipped on re-link if `leafhub_dist/` (or legacy `register.sh`) already exists.
3. **Bind providers** — LeafHub prompts you to bind an existing provider (or add a new one) immediately after create/link. You can add multiple aliases in one session.
4. **On next startup** — call `detect()` or `LeafHub.from_directory()`. Both walk up the directory tree looking for `.leafhub`, just like git looks for `.git`.
5. **Delete a project** — when deleted via the CLI or Web UI, LeafHub performs a full clean-up:
   - Removes `.leafhub` and `leafhub_dist/` from the linked directory; also removes legacy v1 files (`leafhub_probe.py`, `register.sh`) if present
   - Removes CLI symlinks in `~/.local/bin/` whose resolved target lives inside the project (macOS/Linux)
   - Strips the project's `.venv/bin` PATH entries from `~/.zshrc`, `~/.bashrc`, etc. (macOS/Linux) or from the User PATH registry key (Windows)
   - The CLI and Web UI both report exactly what was removed so you can verify the machine is clean.

### `leafhub_dist/` — the distributed integration module

On first registration, LeafHub writes a `leafhub_dist/` directory into the project root. This directory is the v2 integration module (standard 2026-03-21):

| File | Purpose |
|---|---|
| `leafhub_dist/__init__.py` | Makes the directory importable as a Python package; re-exports `detect`, `register`, `ProbeResult` |
| `leafhub_dist/probe.py` | Stdlib-only auto-detection module — the runtime credential resolver |
| `leafhub_dist/register.sh` | Shell integration module — `leafhub_setup_project()` for use in `setup.sh` |

The directory is:
- **`detect()` is stdlib-only** — finds the `.leafhub` file without any pip package; safe to call even if `leafhub` is not installed
- **`open_sdk()` requires `leafhub` pip** — internally imports `leafhub.sdk`; your project must declare `leafhub` as a pip dependency (see below)
- **Offline-capable** — `setup.sh` can source `leafhub_dist/register.sh` without a network call after the first registration
- **Named `leafhub_dist/`** (not `leafhub/`) — avoids shadowing the installed `leafhub` pip package on `sys.path`
- **Never overwritten on re-registration** — re-link or token rotation only updates `.leafhub`

Do not edit the files inside `leafhub_dist/` manually — they are managed by LeafHub and can be refreshed via `leafhub register <project>`.

**pip dependency requirement**

Every project that calls `open_sdk()` at runtime must declare `leafhub` as a pip dependency. Add to your project's `pyproject.toml` and `setup.sh`:

```toml
# pyproject.toml
[project.optional-dependencies]
leafhub = ["leafhub @ git+https://github.com/Rebas9512/Leafhub.git"]
```

```bash
# setup.sh — after venv creation and main deps install
"$VENV_PIP" install -e "$SCRIPT_DIR[leafhub]" --quiet
```

**`sys.path` note for editable installs**

Python editable installs (e.g. `pip install -e .`) only expose named packages declared in `pyproject.toml` — they do not add the project root to `sys.path`. Since `leafhub_dist/` lives at the project root, the fallback import `from leafhub_dist.probe import detect` will fail unless the root is on `sys.path`. Add it explicitly before the import:

```python
import sys
from pathlib import Path
_root = str(Path(__file__).resolve().parent)  # adjust .parent depth as needed
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from leafhub.probe import detect        # pip package (preferred)
except ImportError:
    from leafhub_dist.probe import detect   # local distributed fallback

found = detect()          # searches from cwd by default

if found.ready:
    # .leafhub found with valid token — open SDK directly
    hub = found.open_sdk()                  # requires leafhub pip package
    key = hub.get_key("my-alias")          # raw API key string
    client = hub.openai("my-alias")        # openai.OpenAI instance
    client = hub.anthropic("my-alias")     # anthropic.Anthropic instance

elif found.server_running:
    print(f"Open {found.manage_url} and link this directory.")

elif found.can_link:
    print("LeafHub installed but project not linked yet.")

else:
    # Fall back to manual config
    key = os.environ["OPENAI_API_KEY"]
```

### `ProbeResult` fields

| Field / Property | Type | Description |
|---|---|---|
| `dotfile_path` | `Path \| None` | Absolute path to `.leafhub` if found |
| `dotfile_data` | `dict \| None` | Parsed JSON from the dotfile |
| `server_url` | `str \| None` | `"http://127.0.0.1:<port>"` if server is up |
| `server_running` | `bool` | True when manage server answered the port probe |
| `cli_path` | `str \| None` | Absolute path to `leafhub` binary or None |
| `sdk_importable` | `bool` | True when `import leafhub` succeeds |
| `.ready` | property | Dotfile found and contains a non-empty token |
| `.can_link` | property | At least one LeafHub component is available |
| `.cli_available` | property | Shorthand for `cli_path is not None` |
| `.manage_url` | property | Detected server URL, or `http://127.0.0.1:8765` |
| `.project_name` | property | Value of `"project"` in the dotfile, or None |
| `.open_sdk()` | method | Return a ready `LeafHub` instance from the dotfile token |

### Typical patterns

**Pattern 1 — Silent startup credential resolution:**
```python
from leafhub.probe import detect

found = detect()
if found.ready:
    hub = found.open_sdk()
    api_key = hub.get_key("default")
else:
    api_key = os.environ.get("FALLBACK_API_KEY", "")
```

**Pattern 2 — With setup guidance:**
```python
try:
    from leafhub.probe import detect
except ImportError:
    from leafhub_dist.probe import detect   # local distributed fallback

found = detect()
if found.ready:
    print(f"Credentials loaded from LeafHub (project: '{found.project_name}').")
elif found.server_running:
    print(f"Open {found.manage_url} → link this directory to auto-configure.")
else:
    print("Run: leafhub register my-project  to set up credentials.")
```

**Pattern 3 — Zero-dependency inline snippet** (no file needed):
```python
import json, socket
from pathlib import Path

def lh_detect(project_dir=None, port=8765):
    start = Path(project_dir or Path.cwd()).resolve()
    dotfile = next(
        (d / ".leafhub" for d in [start, *start.parents]
         if (d / ".leafhub").is_file()), None,
    )
    data = None
    if dotfile:
        try:
            data = json.loads(dotfile.read_text(encoding="utf-8"))
            if not isinstance(data, dict): data = None
        except Exception: pass
    running = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0): running = True
    except OSError: pass
    return {
        "ready":          data is not None and bool((data or {}).get("token")),
        "dotfile_data":   data,
        "server_running": running,
        "server_url":     f"http://127.0.0.1:{port}" if running else None,
    }
```

### `.leafhub` dotfile format

```json
{
  "version":   1,
  "project":   "my-project",
  "token":     "lh-proj-<32 hex chars>",
  "linked_at": "2025-01-15T10:30:00+00:00"
}
```

The file is written atomically (tempfile → fchmod 600 → os.replace) and auto-added to `.gitignore`. Never commit it.

---

## Same-Name Projects

Multiple projects with the same name are allowed as long as each has an independent token. This supports patterns like:

- **Single project, multiple agents** — each agent instance has its own token scope
- **Dev / staging / prod** named identically — distinguished by token prefix in the UI

In the Manage UI, duplicate-name cards show a `duplicate` badge and the token prefix is highlighted to tell them apart.

```bash
leafhub project create my-agent            # token A
leafhub project create my-agent            # token B — both valid, independent
```

---

## Provider Connectivity Validation

When you create a provider, LeafHub probes the endpoint before saving the configuration. If the probe fails, the provider is **not saved** and you receive a 422 with a diagnostic message.

| HTTP response from provider | LeafHub verdict |
|---|---|
| 2xx | Connected |
| 401 / 403 | Auth failure — check API key |
| 404 | Wrong base URL |
| 429 | Rate-limited — key valid, endpoint reachable |
| Other 4xx / 5xx | Endpoint reachable (server-side issue) |
| Network error / timeout | Not reachable |

The probe runs once at creation. Subsequent edits (PUT) do not re-probe.

---

## CLI Reference

```bash
# Provider management
leafhub provider add    --name "OpenAI" --key "sk-..." --base-url https://api.openai.com/v1
leafhub provider list
leafhub provider list   --json                                    # machine-readable JSON array
leafhub provider show   --name "OpenAI"
leafhub provider delete --name "OpenAI"

# Project management
leafhub project create  my-project                               # token shown once
leafhub project create  my-project --path /abs/path              # link immediately + write .leafhub
leafhub project create  my-project --path /abs/path --yes        # skip binding wizard
leafhub project create  my-project --path /abs/path --if-not-exists  # idempotent re-link

leafhub project link    my-project --path /abs/path              # link existing project (rotates token)

leafhub project list
leafhub project show    my-project
leafhub project token   my-project                               # rotate token
leafhub project bind    my-project --alias chat --provider "OpenAI"
leafhub project bind    my-project --alias chat --provider "OpenAI" --model gpt-4o
leafhub project unbind  my-project --alias chat
leafhub project delete  my-project

# Registration (install-script integration)
leafhub register my-project                                       # full flow: create → provider → bind
leafhub register my-project --path /abs/path
leafhub register my-project --path /abs/path --headless           # CI / non-interactive
leafhub register my-project --path /abs/path --alias rewrite      # custom binding alias

# Shell integration
leafhub shell-helper                                              # output register.sh for eval

# System
leafhub status                                                    # storage summary (human)
leafhub status --json                                             # {"providers": N, "projects": N, "bound_projects": N, "ready": bool}
leafhub manage                                                    # start web UI on :8765
leafhub manage --port 9000
leafhub manage --rebuild                                          # force-rebuild Vue UI
leafhub manage --dev                                              # dev mode: Vite + FastAPI hot-reload

# Cleanup / removal
leafhub clean                                                     # remove all providers + projects (2× confirm)
leafhub uninstall                                                 # clean + remove LeafHub itself (2× confirm)
```

### Interactive provider binding

After `project create`, `project link`, or `register`, the CLI offers an interactive binding wizard if stdin is a terminal:

```
Bind a provider to this project?
Available providers:
  [1] OpenAI  (openai-completions)
  [2] Anthropic  (anthropic-messages)
  [n] Add a new provider
  [s] Skip  (run later: leafhub project bind my-project --alias <alias> --provider <name>)

Choice: 1
  Alias for 'OpenAI' (e.g. 'chat', 'openai'): chat
✓ Bound alias 'chat' → 'OpenAI' in project 'my-project'.
  Add another binding? [y/N]: n
```

- If **no providers exist**, you are prompted to add one inline before binding.
- In CI / non-interactive shells (stdin not a TTY, or `--headless`), the wizard is skipped silently.

### `leafhub clean` — wipe all data

Removes all stored providers and projects. For each linked project directory, also removes the project artefacts (`.leafhub`, `leafhub_dist/`, and legacy v1 files `leafhub_probe.py`/`register.sh` if present) and any CLI registrations (symlinks in `~/.local/bin/`, shell PATH entries) that the project's installer created.

The LeafHub installation itself is **not** removed — only the vault contents are cleared.

Requires two explicit `[y/N]` confirmations.

```
$ leafhub clean

This will permanently remove:
  3 provider(s) : OpenAI, Anthropic, Ollama
  2 project(s)  : my-app, my-toolkit
  Project artefacts (.leafhub, leafhub_dist/) from 2 linked directories
  CLI registrations (symlinks + shell PATH entries) for those projects

Remove all providers and projects? [y/N] y
This cannot be undone. Confirm again [y/N] y
  removed /home/user/my-app/.leafhub
  ...
✓ Clean complete.
```

### `leafhub uninstall` — full removal

Runs `clean` (no re-prompting) then removes LeafHub itself:

1. Removes the `~/.local/bin/leafhub` CLI symlink (POSIX) or the User PATH entry (Windows)
2. Strips the `# >>> leafhub PATH >>>` block from all shell RC files
3. Removes `~/.leafhub/` (encrypted keys + DB)
4. Removes the LeafHub install directory

Requires two explicit `[y/N]` confirmations. The second prompt explicitly states that all stored API keys will be deleted.

```
$ leafhub uninstall

LeafHub uninstall will:
  1. Remove N provider(s) and N project(s)
  2. Remove the leafhub CLI symlink and PATH entries from shell RC files
  3. Remove the data directory    : /home/user/.leafhub
  4. Remove the install directory : /home/user/leafhub

Uninstall LeafHub completely? [y/N] y
This will delete all stored API keys and cannot be undone. Confirm [y/N] y

── Phase 1: removing projects and providers ──
  ...
── Phase 2: removing LeafHub installation ──
  removed /home/user/.local/bin/leafhub
  removed leafhub PATH block from ~/.zshrc
  removed /home/user/.leafhub
  removed /home/user/leafhub

✓ LeafHub uninstalled.
  Open a new terminal to reload your shell environment.
```

---

## SDK Reference

```python
from leafhub import LeafHub

# Option A: explicit token (from env or secret manager)
hub = LeafHub(token="lh-proj-...")

# Option B: auto-detect from .leafhub dotfile (no token in code)
hub = LeafHub.from_directory()           # searches from cwd
hub = LeafHub.from_directory("/path/to/project")

# Option C: via ProbeResult (from detect())
from leafhub.probe import detect
found = detect()
if found.ready:
    hub = found.open_sdk()

# Key access
key     = hub.get_key("chat")            # → "sk-..."
cfg     = hub.get_config("chat")         # → ProviderConfig(base_url, model, auth_mode, ...)
headers = hub.build_headers("chat")      # → {"Authorization": "Bearer sk-..."} or {"x-api-key": ...}
aliases = hub.list_aliases()             # → ["chat", "embed", "local"]

# Drop-in SDK clients
client = hub.openai("chat")             # → openai.OpenAI(api_key=..., base_url=...)
client = hub.anthropic("chat")          # → anthropic.Anthropic(api_key=...)

# Context manager
with LeafHub.from_directory() as hub:
    key = hub.get_key("chat")
```

---

## Admin API

All `/admin/*` endpoints require `Authorization: Bearer <token>` when `LEAFHUB_ADMIN_TOKEN` is set.

```
# Providers
GET    /admin/providers
POST   /admin/providers           body: {label, api_format, base_url, default_model, api_key, ...}
                                  → probes endpoint before saving; 422 if unreachable
GET    /admin/providers/{id}
PUT    /admin/providers/{id}
DELETE /admin/providers/{id}

# Projects
GET    /admin/projects
POST   /admin/projects            body: {name, bindings?, path?}
GET    /admin/projects/{id}
PUT    /admin/projects/{id}
DELETE /admin/projects/{id}      → full clean-up: removes .leafhub + leafhub_dist/,
                                   CLI symlinks in ~/.local/bin/, and venv PATH entries
                                   from shell RC files (or Windows User PATH registry).
                                   Also removes legacy v1 files (leafhub_probe.py, register.sh)
                                   if present. Response: {deleted, files_removed, registration_removed}
POST   /admin/projects/{id}/rotate-token
POST   /admin/projects/{id}/deactivate
POST   /admin/projects/{id}/activate
POST   /admin/projects/{id}/link  body: {path, alias?}
                                  → rotates token, writes .leafhub; distributes leafhub_dist/
                                    if project is not already integrated (first link only).
                                    alias field (v2): if provided, auto-binds first provider
                                    under that alias immediately after linking.

# System
GET    /health
GET    /admin/status
GET    /admin/docs                (Swagger UI)
```

---

## Design Philosophy

**Zero-config auto-detection.** When a directory is linked, LeafHub writes a `.leafhub` dotfile and, for new projects, distributes a `leafhub_dist/` integration module. Projects detect their own credentials on startup without any token in the codebase. Detection walks up the directory tree like git.

**Standardized integration via `leafhub_dist/`.** On first registration, LeafHub writes a `leafhub_dist/` directory into the project root containing `probe.py` (detection module), `register.sh` (shell integration), and `__init__.py` (Python package entrypoint). `probe.detect()` is stdlib-only — no pip needed; `found.open_sdk()` requires the `leafhub` pip package, declared as an optional dependency in the project's `pyproject.toml` and installed by `setup.sh`. Any project's `setup.sh` sources `leafhub_dist/register.sh` as an offline fallback — no curl in the common case after the first registration. Named `leafhub_dist/` (not `leafhub/`) to avoid shadowing the installed pip package on `sys.path`.

**Integration module distributed, not fetched.** On first link, LeafHub writes `leafhub_dist/` to the project root. If `leafhub_dist/` (or the legacy v1 root-level `register.sh`) already exists, the project is treated as already integrated — only the `.leafhub` dotfile is updated, so no user-customised files are overwritten. On subsequent links (token rotation or re-link), only `.leafhub` is refreshed.

**Keys never at rest in plaintext.** Provider API keys are AES-256-GCM encrypted on disk (`providers.enc`). The master key is stored in the system keychain when available; otherwise in a restricted file (chmod 600). The raw key is never logged or returned after creation.

**Token shown once.** Project Bearer tokens are stored as SHA-256 hashes only. The raw token is returned exactly once at creation (or written directly to `.leafhub` when linking). There is no recovery path — rotate if lost.

**Validated before saved.** Provider configurations are connectivity-probed before the first DB write. A bad API key or wrong base URL is caught at configuration time, not at runtime.

**Same name, independent identity.** Multiple projects can share a name. Each project is identified by its token hash, not its name. This enables multi-agent and multi-environment patterns without inventing artificial naming schemes.

**Clean deletion at every level.** When a project is deleted (CLI or Web UI), LeafHub removes all its artefacts: `.leafhub`, `leafhub_dist/` (and legacy v1 files if present), CLI symlinks in `~/.local/bin/`, and venv PATH entries from shell RC files or the Windows User PATH registry. `leafhub clean` extends this to all projects at once, wiping the vault contents while leaving the installation in place. `leafhub uninstall` goes further: after cleaning all data it removes its own CLI symlink, PATH entries, data directory, and source tree — leaving the machine in a fully clean state. Both `clean` and `uninstall` require two explicit confirmations and print every item removed.

**Loopback-only management server.** `leafhub manage` binds to `127.0.0.1` only. Not designed to be network-exposed; the loopback bind is the primary security boundary in dev mode.

**No runtime network dependency.** The SDK is pure local I/O — file reads and SQLite queries only. No HTTP calls, no daemon required.

---

## Use Cases

- **Stop putting API keys in `.env` files** — store once in the vault, reference by alias in code.
- **Zero-touch onboarding** — link a directory once from the UI; the project auto-detects credentials on every subsequent startup without any manual step.
- **Multiple projects, one credential store** — each project gets its own token and alias namespace without duplicating provider keys.
- **Key rotation without code changes** — update the key in LeafHub; all projects reading that provider see the new key immediately.
- **Single project, multiple agents** — create multiple same-name projects, each with an independent token scope.
- **Standardized new-project setup** — any future project sources the `leafhub_dist/register.sh` integration block in its `setup.sh` and gets full credential management for free.
- **Local Ollama + cloud fallback** — register both; switch bindings in the Web UI without touching application code.

---

## Project Structure

```
Leafhub/
│
├── pyproject.toml               # Project metadata and dependencies (PEP 517, src layout)
├── install.sh                   # macOS / Linux / WSL one-liner installer
├── setup.sh                     # Unix manual setup: --reinstall / --uninstall / --doctor
├── install.ps1                  # Windows PowerShell installer
├── install.cmd                  # Windows CMD bootstrap → PowerShell
├── register.sh                  # Shell integration module (see Project Integration Standard)
│
├── src/
│   └── leafhub/                 # Python package (importable as `leafhub`)
│       ├── __init__.py
│       ├── cli.py               # argparse CLI (provider / project / register / shell-helper / manage)
│       ├── sdk.py               # LeafHub — runtime key access, from_directory()
│       ├── probe.py             # Auto-detection (stdlib only); distributed as leafhub_dist/probe.py
│       ├── register.sh          # Bundled copy of register.sh for `leafhub shell-helper`
│       ├── errors.py            # Typed exception hierarchy
│       │
│       ├── core/
│       │   ├── db.py            # SQLite connection, schema + structural migrations, WAL tuning
│       │   ├── crypto.py        # AES-256-GCM encryption, master key resolution chain
│       │   └── store.py         # SyncStore: provider/project/token/binding CRUD
│       │
│       └── manage/              # Web management layer (requires leafhub[manage])
│           ├── server.py        # FastAPI app factory, lifespan, SPA static serving
│           ├── auth.py          # LEAFHUB_ADMIN_TOKEN gate, per-IP rate limiter
│           ├── providers.py     # Provider CRUD + connectivity probe on create
│           └── projects.py      # Project CRUD, link endpoint, .leafhub + leafhub_dist/ distribution
│
├── ui/                          # Vue 3 + Vite web management interface
│   ├── src/
│   │   ├── api.js               # Admin HTTP client (fetch wrapper, localStorage token)
│   │   ├── presets.js           # Provider preset definitions and auth mode inference
│   │   ├── App.vue              # Root layout: sidebar navigation + router outlet
│   │   └── views/
│   │       ├── ProvidersView.vue  # Provider CRUD, connectivity test UX, probe banner
│   │       └── ProjectsView.vue   # Project CRUD, link modal, same-name badge, delete cleanup display
│   └── ...
│
└── scripts/
    └── check_env.py             # Environment diagnostic tool
```

---

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `LEAFHUB_ADMIN_TOKEN` | *(unset)* | Admin API bearer token. If unset, admin endpoints are unprotected (dev/loopback mode). |
| `LEAFHUB_MASTER_KEY` | *(auto-generated)* | Base64-encoded 32-byte master key for provider key encryption. Generate with: `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` |
| `LEAFHUB_HUB_DIR` | `~/.leafhub/` | Override the storage directory. |

---

## Security Notes

**API key input.** `leafhub provider add --key` accepts the API key as an argument for scripted use, but passing secrets as CLI arguments exposes them in the process list and shell history. Omit `--key` to be prompted interactively — the key will not be echoed to the terminal.

**Master key validation.** `LEAFHUB_MASTER_KEY` must be a base64 string that decodes to exactly 32 bytes. LeafHub rejects malformed values at startup with a descriptive error rather than failing silently later.

**Rate limiting.** The admin API enforces a per-client sliding-window rate limit (5 failures in 5 minutes → 5-minute lockout). Only the real transport address is used — the `X-Forwarded-For` header is intentionally ignored to prevent local processes from spoofing their IP.

**Atomic credential write.** Provider records and their encrypted API keys are written atomically — if either write fails, the entire operation is rolled back so the store never contains a provider with a missing key.

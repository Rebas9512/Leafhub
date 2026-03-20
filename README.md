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
      │  Link /path/to/myproject     │                            │
      │────────────────────────────► │                            │
      │                              │  write .leafhub (chmod 600)│
      │                              │ ──────────────────────────►│
      │                              │  copy leafhub_probe.py     │
      │                              │ ──────────────────────────►│
      │                              │                            │
      │                              │     Next startup           │
      │                              │◄───────────────────────────│
      │                              │  LeafHub.from_directory()  │
      │                              │  → reads .leafhub token    │
      │                              │  → returns API key         │
      │                              │ ──────────────────────────►│
```

---

## Abstract Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         cli.py                              │
│          CLI Entry Point — provider / project / manage      │
│   project create [--path] [--no-probe]                      │
│   project link   --path   [--no-probe]                      │
└───────────┬─────────────────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │               probe.py                        │
    │   LeafHub auto-detection (stdlib only)        │
    │   detect() → ProbeResult                      │
    │   Distributed as leafhub_probe.py on link     │
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
    │   (probe on create)│   (link + probe copy)    │
    └───────┬──────────────────────────────────────┘
            │
    ┌───────▼──────────────────────────────────────┐
    │          Vue 3 + Vite Web UI                  │
    │   provider presets │ connectivity test        │
    │   project linking  │ probe copy checkbox      │
    │   same-name badge  │ token display modal      │
    └──────────────────────────────────────────────┘
```

---

## Module Reference

### `src/leafhub/` — Python Package

| File | Responsibility |
|------|----------------|
| `cli.py` | argparse CLI. Subcommands: `provider add/list/show/delete`, `project create/link/list/show/token/bind/unbind/delete`, `manage`. |
| `sdk.py` | `LeafHub` — runtime client for application code. Resolves the hub directory, verifies the project token, decrypts API keys, builds provider-specific auth headers. `from_directory()` auto-loads from a `.leafhub` dotfile. |
| `probe.py` | Stdlib-only auto-detection module. `detect()` searches for a `.leafhub` dotfile, probes the manage server port, checks for the CLI binary and SDK. Returns a `ProbeResult` with convenience properties (`ready`, `can_link`, `open_sdk()`). Distributed as `leafhub_probe.py` when a project is linked. |
| `errors.py` | Typed exception hierarchy: `LeafHubError`, `StorageNotFoundError`, `InvalidTokenError`, `AliasNotBoundError`, `DecryptionError`. |

---

### `src/leafhub/core/` — Foundation Layer

| File | Responsibility |
|------|----------------|
| `db.py` | Opens the SQLite connection, runs schema migrations idempotently, applies WAL mode and PRAGMA tuning. Migrations include ADD COLUMN (new columns) and table-recreation (removing the historical UNIQUE constraint on `projects.name`). |
| `crypto.py` | AES-256-GCM encryption for provider API keys stored in `providers.enc`. Master key resolved from env → system keychain → `~/.leafhub/.masterkey`, auto-generated on first run. PBKDF2-SHA256 (600,000 iterations) for key derivation. Fresh salt and nonce per write. |
| `store.py` | `SyncStore` — all CRUD operations against SQLite. Manages providers, projects (same-name allowed), project tokens (SHA-256 hash only), model alias bindings. |

---

### `src/leafhub/manage/` — Web Management Layer (optional)

Requires `pip install 'leafhub[manage]'`.

| File | Responsibility |
|------|----------------|
| `server.py` | FastAPI app factory. Lifespan hooks load the master key and open SQLite. Serves the compiled Vue UI from `ui/dist/`. Exposes `GET /health` and `GET /admin/status`. |
| `auth.py` | Admin token middleware. Reads `LEAFHUB_ADMIN_TOKEN` from environment; all `/admin/*` routes require a matching Bearer token with constant-time comparison. Per-IP sliding-window rate limiter (5 failures → 5-minute lockout). |
| `providers.py` | CRUD routes for providers. **Connectivity probe on create**: `POST /admin/providers` makes a GET request to the provider's endpoint before saving. Returns HTTP 422 with a diagnostic message if unreachable. `provider_type` and `api_format` are immutable after creation. |
| `projects.py` | CRUD routes for projects. Token lifecycle: create (plaintext shown once), rotate, revoke. `POST /admin/projects/{id}/link` rotates the token, writes a `.leafhub` dotfile (chmod 600), and optionally copies `leafhub_probe.py` to the project root. Same-name projects are allowed — each has an independent token. |

---

### `ui/` — Web Management Interface

Built with Vue 3 + Vite. Served as static files from `ui/dist/` by the FastAPI server.

| File | Responsibility |
|------|----------------|
| `src/api.js` | HTTP client wrapping `fetch`. Admin token from `localStorage`. |
| `src/presets.js` | Provider preset definitions (OpenAI, Anthropic, Ollama, etc.) with default URLs, models, and auth mode inference. |
| `src/views/ProvidersView.vue` | Provider management UI. "Create & Test Connection" button — shows "Testing connectivity…" banner while the server-side probe runs. Probe errors displayed inline. |
| `src/views/ProjectsView.vue` | Project management UI. Same-name projects get a "duplicate" badge with the token prefix highlighted. Link Directory modal includes a "Copy `leafhub_probe.py`" checkbox (default checked). |

---

## Project Linking & Auto-Detection

The central workflow that removes token management from application code.

### How it works

1. **Create a project** in the Manage UI or CLI — you receive a one-time token.
2. **Link a directory** — LeafHub writes:
   - `.leafhub` — a JSON file with the project token (chmod 600, auto-added to `.gitignore`)
   - `leafhub_probe.py` — a standalone detection module you can integrate directly (optional, default on)
3. **On next startup** — call `detect()` or `LeafHub.from_directory()`. Both walk up the directory tree looking for `.leafhub`, just like git looks for `.git`.

### `leafhub_probe.py` — the distributed detection file

When you link a project, LeafHub copies its `probe.py` into your project root as `leafhub_probe.py`. This file:

- Has **zero runtime dependencies** (stdlib only)
- Can be imported without installing `leafhub`
- Serves as **inline documentation** — read it to understand the detection protocol and adapt it to your pipeline

```python
# Option A: installed package
from leafhub.probe import detect

# Option B: standalone copy in project root (zero deps)
from leafhub_probe import detect

found = detect()          # searches from cwd by default

if found.ready:
    # .leafhub found with valid token — open SDK directly
    hub = found.open_sdk()
    key = hub.get_key("chat")              # raw API key string
    client = hub.openai("chat")            # openai.OpenAI instance
    client = hub.anthropic("chat")         # anthropic.Anthropic instance

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

**Pattern 1 — Onboarding wizard:**
```python
from leafhub_probe import detect

found = detect()
if found.ready:
    print(f"Already linked as '{found.project_name}' — skipping setup.")
elif found.server_running:
    print(f"Open {found.manage_url} → link this directory to auto-configure.")
else:
    # show manual setup instructions
```

**Pattern 2 — Silent fallback in pipelines:**
```python
from leafhub_probe import detect

_found = detect()   # fast, never raises, < 1 s

def get_api_key(alias="chat"):
    if _found.ready:
        try:
            return _found.open_sdk().get_key(alias)
        except Exception:
            pass
    return os.environ.get("OPENAI_API_KEY")
```

**Pattern 3 — Zero-dependency inline snippet** (no file needed):
```python
import importlib.util, json, shutil, socket
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

The probe runs once at creation. Subsequent edits (PUT) do not re-probe — you own validation after that.

---

## Quick Start

**Requirements:** Python 3.10+

### Automated install (macOS / Linux / WSL)

```bash
bash install.sh
```

### Automated install (Windows)

```powershell
.\install.ps1
```

Or double-click `install.cmd`.

### Manual install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[manage]'

# Build the Web UI
cd ui && npm install && npm run build && cd ..
```

---

## CLI Reference

```bash
# Provider management
leafhub provider add    --name "OpenAI" --key "sk-..." --base-url https://api.openai.com/v1
leafhub provider list
leafhub provider show   --name "OpenAI"
leafhub provider delete --name "OpenAI"

# Project management
leafhub project create  my-project                        # token shown once
leafhub project create  my-project --path /abs/path       # link immediately + write .leafhub
leafhub project create  my-project --path /abs/path --no-probe  # skip probe copy

leafhub project link    my-project --path /abs/path       # link existing project (rotates token)
leafhub project link    my-project --path /abs/path --no-probe  # skip probe copy

leafhub project list
leafhub project show    my-project
leafhub project token   my-project                        # rotate token
leafhub project bind    my-project --alias chat --provider "OpenAI"
leafhub project bind    my-project --alias chat --provider "OpenAI" --model gpt-4o
leafhub project unbind  my-project --alias chat
leafhub project delete  my-project

# System
leafhub status                                            # storage summary
leafhub manage                                            # start web UI on :8765
leafhub manage --port 9000
leafhub manage --rebuild                                  # force-rebuild Vue UI before starting (picks up UI code changes)
leafhub manage --dev                                      # dev mode: hot-reload Vite + FastAPI backend side-by-side
```

### `--no-probe` flag

Both `project create --path` and `project link` copy `leafhub_probe.py` to the project root by default. Pass `--no-probe` to skip this if you already have the file or don't want it.

```bash
leafhub project link my-project --path ./my-project --no-probe
```

---

## SDK Reference

```python
from leafhub import LeafHub

# Option A: explicit token (from env or .env file)
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
POST   /admin/projects            body: {name, bindings?, path?, copy_probe?}
GET    /admin/projects/{id}
PUT    /admin/projects/{id}
DELETE /admin/projects/{id}
POST   /admin/projects/{id}/rotate-token
POST   /admin/projects/{id}/deactivate
POST   /admin/projects/{id}/activate
POST   /admin/projects/{id}/link  body: {path, copy_probe?}
                                  → rotates token, writes .leafhub, copies leafhub_probe.py

# System
GET    /health
GET    /admin/status
GET    /admin/docs                (Swagger UI)
```

### `POST /admin/providers` — connectivity probe

The server makes a lightweight GET to the provider before persisting anything:

```json
// 422 response when probe fails
{
  "detail": "Provider connectivity check failed: Authentication failed — check your API key (HTTP 401)"
}
```

### `POST /admin/projects/{id}/link` — link endpoint

```json
// Request
{ "path": "/abs/path/to/project", "copy_probe": true }

// Response
{
  "linked":     true,
  "path":       "/abs/path/to/project",
  "dotfile":    "/abs/path/to/project/.leafhub",
  "probe_copy": "/abs/path/to/project/leafhub_probe.py",
  "project":    { ... },
  "message":    "Project 'my-project' linked to /abs/path/to/project. ..."
}
```

---

## Design Philosophy

**Zero-config auto-detection.** When a directory is linked, LeafHub writes a `.leafhub` dotfile and distributes `leafhub_probe.py`. Projects detect their own credentials on startup without any token in the codebase. Detection walks up the directory tree like git.

**Probe distributed, not fetched.** `leafhub_probe.py` is copied to the project root at link time. It is a standalone stdlib-only file — readable as documentation, adaptable for any pipeline. No network call or `pip install` needed to use it.

**Keys never at rest in plaintext.** Provider API keys are AES-256-GCM encrypted on disk (`providers.enc`). The master key is stored in the system keychain when available; otherwise in a restricted file (chmod 600). The raw key is never logged or returned after creation.

**Token shown once.** Project Bearer tokens are stored as SHA-256 hashes only. The raw token is returned exactly once at creation (or written directly to `.leafhub` when linking — never shown in the response). There is no recovery path — rotate if lost.

**Validated before saved.** Provider configurations are connectivity-probed before the first DB write. A bad API key or wrong base URL is caught at configuration time, not at 3 AM when a pipeline job fails.

**Same name, independent identity.** Multiple projects can share a name. Each project is identified by its token hash, not its name. This enables multi-agent and multi-environment patterns without inventing artificial naming schemes.

**Loopback-only management server.** `leafhub manage` binds to `127.0.0.1` only. Not designed to be network-exposed; the loopback bind is the primary security boundary in dev mode.

**No runtime network dependency.** The SDK is pure local I/O — file reads and SQLite queries only. No HTTP calls, no daemon required. Applications that only read keys have zero network footprint.

---

## Use Cases

- **Stop putting API keys in `.env` files** — store once in the vault, reference by alias in code.
- **Zero-touch onboarding** — link a directory once from the UI; the project auto-detects credentials on every subsequent startup without any manual step.
- **Multiple projects, one credential store** — each project gets its own token and alias namespace without duplicating provider keys.
- **Key rotation without code changes** — update the key in LeafHub; all projects reading that provider see the new key immediately.
- **Single project, multiple agents** — create multiple same-name projects, each with an independent token scope.
- **Local Ollama + cloud fallback** — register both; switch bindings in the Web UI without touching application code.
- **Embed detection in any pipeline** — copy the 20-line `lh_detect()` snippet into any onboarding script; no package installation needed.

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
│
├── src/
│   └── leafhub/                 # Python package (importable as `leafhub`)
│       ├── __init__.py
│       ├── cli.py               # argparse CLI (provider / project create+link / manage)
│       ├── sdk.py               # LeafHub — runtime key access, from_directory()
│       ├── probe.py             # Auto-detection (stdlib only); distributed as leafhub_probe.py
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
│           └── projects.py      # Project CRUD, link endpoint, .leafhub + probe copy
│
├── ui/                          # Vue 3 + Vite web management interface
│   ├── src/
│   │   ├── api.js               # Admin HTTP client (fetch wrapper, localStorage token)
│   │   ├── presets.js           # Provider preset definitions and auth mode inference
│   │   ├── App.vue              # Root layout: sidebar navigation + router outlet
│   │   └── views/
│   │       ├── ProvidersView.vue  # Provider CRUD, connectivity test UX, probe banner
│   │       └── ProjectsView.vue   # Project CRUD, link modal, same-name badge, probe checkbox
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
| `LEAFHUB_MASTER_KEY` | *(auto-generated)* | Base64-encoded 32-byte master key for provider key encryption. Must decode to exactly 32 bytes. Generate with: `python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"` |
| `LEAFHUB_HUB_DIR` | `~/.leafhub/` | Override the storage directory. |

---

## Security Notes

**API key input.** `leafhub provider add --key` accepts the API key as an argument for scripted use, but passing secrets as CLI arguments exposes them in the process list and shell history. Omit `--key` to be prompted interactively — the key will not be echoed to the terminal.

**Master key validation.** `LEAFHUB_MASTER_KEY` must be a base64 string that decodes to exactly 32 bytes. LeafHub rejects malformed values at startup with a descriptive error rather than failing silently later.

**Rate limiting.** The admin API enforces a per-client sliding-window rate limit (5 failures in 5 minutes → 5-minute lockout). Only the real transport address is used — the `X-Forwarded-For` header is intentionally ignored to prevent local processes from spoofing their IP.

**Atomic credential write.** Provider records and their encrypted API keys are written atomically — if either write fails, the entire operation is rolled back so the store never contains a provider with a missing key.

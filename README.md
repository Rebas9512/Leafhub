# LeafHub

[![CI](https://github.com/Rebas9512/Leafhub/actions/workflows/ci.yml/badge.svg)](https://github.com/Rebas9512/Leafhub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A local encrypted API key vault for LLM projects. Store provider credentials once, reference them by alias across all your projects — no plaintext keys in `.env` files, no manual copy-paste across repos.

Projects **auto-detect** their credentials on startup via a `.leafhub` dotfile that LeafHub writes when you link a directory. No token management in application code.

---

## Install

**macOS / Linux / WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1 | iex
```

**Windows (CMD)**

```cmd
curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.cmd -o install.cmd && install.cmd && del install.cmd
```

The installer prompts for a directory (default: `~/leafhub`), clones the repo, creates a virtual environment, and registers `leafhub` on your PATH. Open a new terminal after install.

---

## Quick setup

**1. Add your first provider**

```bash
leafhub provider add
```

Prompts for provider name, base URL, API key, and default model. Any OpenAI-compatible endpoint works (OpenAI, Anthropic, Groq, Ollama, etc.).

**Or sign in with your ChatGPT subscription (no API key needed):**

```bash
leafhub provider login --name codex
```

Opens a browser for OpenAI OAuth — usage goes through your ChatGPT Plus/Pro quota, not API credits. Tokens auto-refresh on every SDK call.

Or use the Web UI:

```bash
leafhub manage    # opens http://localhost:8765
```

**2. Link a project directory**

```bash
leafhub register my-app --path /path/to/my-app --alias rewrite
```

This writes a `.leafhub` dotfile into the project directory and binds the provider under the alias `rewrite`. Your application code calls `hub.get_key("rewrite")` to retrieve the key at runtime — no key in any config file.

**3. Verify**

```bash
leafhub project show my-app
# Should show:
#   Bindings:
#     rewrite  → ProviderName  (model: ...)
```

---

## Using LeafHub in runtime code

After linking a project, the SDK auto-detects the `.leafhub` dotfile. The recommended import pattern tries the installed pip package first, then falls back to the distributed copy:

```python
try:
    from leafhub.probe import detect      # pip package (preferred)
except ImportError:
    from leafhub_dist.probe import detect # distributed copy (fallback)

result = detect()
hub = result.open_sdk()
api_key = hub.get_key("rewrite")          # decrypts and returns the key
cfg    = hub.get_config("rewrite")        # base_url, model, auth_mode, ...
```

The `leafhub_dist/` directory is written into your project root at registration time — no network dependency at runtime. For the full runtime template including env-var fallback and startup injection, see `leafhub_dist/LEAFHUB.md` in any registered project.

---

## Key rotation and provider switching

Update keys or switch providers any time in the Web UI or CLI — application code requires no changes:

```bash
leafhub manage    # edit providers in the UI
# or CLI:
leafhub project bind my-app --alias rewrite --provider "Anthropic"
```

---

## CLI reference

| Command | What it does |
|---------|-------------|
| `leafhub provider add` | Register a new API provider (API key) |
| `leafhub provider login --name <label>` | Add an OpenAI Codex OAuth provider (ChatGPT subscription) |
| `leafhub provider list` | List configured providers |
| `leafhub register <name> --path <dir> --alias <alias>` | Link a project directory |
| `leafhub project show <name>` | Show project status and bindings |
| `leafhub project bind <name> --alias <alias> --provider <name>` | Bind a provider alias |
| `leafhub manage` | Start the Web UI at http://localhost:8765 |
| `leafhub status` | Overall vault health check |
| `leafhub uninstall` | Full interactive removal |

---

## Uninstall

```bash
leafhub uninstall
```

Interactive two-step removal: removes all project artefacts (`.leafhub`, `leafhub_dist/`, CLI symlinks), then removes LeafHub itself (`~/.leafhub/`, install directory, PATH entries).

---

## Architecture

```
Manage UI / CLI                  LeafHub                    Your Project
      │                              │                            │
      │  leafhub register my-app     │                            │
      │────────────────────────────► │                            │
      │                              │  write .leafhub (chmod 600)│
      │                              │ ──────────────────────────►│
      │                              │  distribute leafhub_dist/  │
      │                              │ ──────────────────────────►│
      │                              │                            │
      │                              │     Next startup           │
      │                              │◄───────────────────────────│
      │                              │  detect() → open_sdk()     │
      │                              │  → reads .leafhub token    │
      │                              │  → returns API key         │
      │                              │ ──────────────────────────►│
```

Keys are AES-256-GCM encrypted on disk. The master key lives in the system keychain when available, otherwise in `~/.leafhub/.masterkey`.

### Supported providers

| API format | Auth mode | Examples |
|------------|-----------|----------|
| `openai-completions` | `bearer` | OpenAI, Groq, vLLM, any OpenAI-compatible |
| `openai-responses` | `bearer` / `openai-oauth` | OpenAI Responses API, ChatGPT Codex endpoint |
| `anthropic-messages` | `x-api-key` | Anthropic, MiniMax (Anthropic-compatible) |
| `ollama` | `none` | Local Ollama instance |

OAuth providers (`openai-oauth`) store a refresh token instead of a static API key. The SDK transparently refreshes access tokens on every call — application code sees a standard Bearer token.

### What gets installed

| Location | Contents |
|---|---|
| `~/leafhub/` | Source code (configurable via `LEAFHUB_DIR`) |
| `~/leafhub/.venv/` | Isolated Python environment |
| `~/.local/bin/leafhub` | CLI symlink (macOS / Linux / WSL) |
| `~/.leafhub/` | Encrypted key store, SQLite DB, master key |

---

## Project integration standard

When you run `leafhub register` for the first time, LeafHub writes a `leafhub_dist/` directory into your project root containing everything needed to integrate:

| File | Purpose |
|------|---------|
| `register.sh` | Shell function (`leafhub_setup_project`) for setup scripts |
| `probe.py` | Stdlib-only runtime detection — `detect()` → `open_sdk()` → `get_key()` |
| `LEAFHUB.md` | **Full integration reference** — setup block, Python templates, troubleshooting |
| `setup_template.sh` | Ready-to-use `setup.sh` starting point (copy → rename → change 2 lines) |

**The fastest way to integrate a new project:**

```bash
# After leafhub register writes leafhub_dist/ into your project:
cp leafhub_dist/setup_template.sh setup.sh
chmod +x setup.sh
# Edit the two CUSTOMIZE lines at the top of setup.sh, then run it.
```

**Manual integration** — add this block to your existing `setup.sh` after the venv step:

```bash
# ── LeafHub integration ───────────────────────────────────────────────────────
# Resolution order (stops at first success):
#   1. leafhub shell-helper        — system PATH binary (fast, offline)
#   2. $VENV_DIR/bin/leafhub       — pip-installed in venv (offline fallback)
#   3. leafhub_dist/register.sh    — local distributed copy (offline fallback)
#   4. GitHub curl                 — first-time bootstrap, network required
_lh_content=""
if _lh_content="$(leafhub shell-helper 2>/dev/null)" && [[ -n "$_lh_content" ]]; then
    eval "$_lh_content"
elif [[ -x "$VENV_DIR/bin/leafhub" ]] \
    && _lh_content="$("$VENV_DIR/bin/leafhub" shell-helper 2>/dev/null)" \
    && [[ -n "$_lh_content" ]]; then
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

leafhub_setup_project "my-project" "$SCRIPT_DIR" "my-alias" \
    || fail "LeafHub registration failed."
```

Also declare `leafhub` as an optional pip dependency:

```toml
# pyproject.toml
[project.optional-dependencies]
leafhub = ["leafhub @ git+https://github.com/Rebas9512/Leafhub.git"]
```

**Alias consistency** — the alias passed to `leafhub_setup_project` must exactly match what your runtime code calls in `hub.get_key("<alias>")`. A mismatch is the most common cause of `credentials: none`.

For the complete reference — Python runtime templates, CLI setup command pattern, environment variables, and troubleshooting — open `leafhub_dist/LEAFHUB.md` in your project after registration.

---

## Module reference

### `src/leafhub/`

| File | Responsibility |
|------|----------------|
| `cli.py` | CLI entry point. Subcommands: `provider`, `project`, `register`, `manage`, `status`. |
| `sdk.py` | Runtime key access. `get_key()`, `get_config()`, `from_directory()`. |
| `probe.py` | Stdlib-only auto-detection. `detect()` finds `.leafhub` and returns `open_sdk()`. Distributed as `leafhub_dist/probe.py`. |
| `errors.py` | Typed exceptions: `LeafHubError`, `InvalidTokenError`, `AliasNotBoundError`, etc. |

### `src/leafhub/core/`

| File | Responsibility |
|------|----------------|
| `db.py` | SQLite connection, schema migrations, WAL mode. |
| `crypto.py` | AES-256-GCM encryption. PBKDF2-SHA256 key derivation (600,000 iterations). |
| `store.py` | CRUD operations for providers, projects, tokens, and alias bindings. |
| `oauth.py` | OpenAI Codex OAuth 2.0 Authorization Code + PKCE flow. Token exchange and refresh. |

### `src/leafhub/manage/` (optional — `pip install 'leafhub[manage]'`)

| File | Responsibility |
|------|----------------|
| `server.py` | FastAPI app. Serves the compiled Vue UI from `ui/dist/`. |
| `auth.py` | Admin token middleware with per-IP rate limiting. |
| `providers.py` | Provider CRUD. Connectivity probe on create. OAuth PKCE flow endpoints. |
| `projects.py` | Project CRUD. Token lifecycle, `.leafhub` distribution, `leafhub_dist/` module distribution (`register.sh`, `probe.py`, `LEAFHUB.md`, `setup_template.sh`), CLI auto-registration, full cleanup on delete. |

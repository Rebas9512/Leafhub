# LeafHub Integration

This directory (`leafhub_dist/`) is written into your project root the first time you run `leafhub register`. It contains everything needed to integrate with LeafHub — offline-capable at both setup time and runtime.

| File | Purpose |
|------|---------|
| `__init__.py` | Package marker — re-exports `detect`, `register`, `ProbeResult` |
| `register.sh` | Shell function for setup scripts (`leafhub_setup_project`) |
| `probe.py` | Stdlib-only runtime detection (`detect()` → `open_sdk()`) |
| `setup_template.sh` | Ready-to-use `setup.sh` starting point for new projects |
| `LEAFHUB.md` | This file — full protocol reference |

Do not edit these files manually. They are refreshed by LeafHub on re-registration:
```bash
leafhub register <project-name> --path <dir> --alias <alias>
```

---

## Quick integration checklist

Three things to wire up in a new project:

| Step | Where | What |
|------|-------|------|
| 1 | `setup.sh` | Source the LeafHub block and call `leafhub_setup_project` |
| 2 | `pyproject.toml` | Declare `leafhub` as an optional dependency |
| 3 | Runtime startup code | `detect()` → `open_sdk()` → `hub.get_key("<alias>")` |

The alias you pass to `leafhub_setup_project` in step 1 **must exactly match** the alias you pass to `hub.get_key()` in step 3. A mismatch is the most common cause of `credentials: none`.

---

## Step 1 — setup.sh integration block

Add this block to your `setup.sh` after the venv and pip install steps. The only line to change is the `leafhub_setup_project` call at the bottom.

```bash
# ── LeafHub integration ───────────────────────────────────────────────────────
# Resolution order — stops at first successful source:
#   1. leafhub shell-helper   — system PATH binary (fast, offline)
#   2. leafhub_dist/register.sh — local distributed copy (offline fallback)
#   3. GitHub curl            — first-time bootstrap, network required
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

[[ "${HEADLESS:-false}" == "true" ]] && export LEAFHUB_HEADLESS=1

# ── CUSTOMIZE: set your project name and alias ────────────────────────────────
leafhub_setup_project "my-project" "$SCRIPT_DIR" "my-alias" \
    || fail "LeafHub registration failed."
# ─────────────────────────────────────────────────────────────────────────────
```

**Three things to set per project:**

| Parameter | Convention | Example |
|-----------|-----------|---------|
| `name` | Lowercase slug matching repo name | `"trileaf"`, `"my-api"` |
| `path` | Directory containing `setup.sh` | `"$SCRIPT_DIR"` |
| `alias` | Must match `hub.get_key("<alias>")` in runtime code | `"rewrite"`, `"chat"`, `"default"` |

**Headless / CI mode:** Set `LEAFHUB_HEADLESS=1` before calling `leafhub_setup_project` to skip all interactive prompts.

**If your project uses LeafHub as a pip dep** (calls `open_sdk()` at runtime), also add this before the LeafHub block:
```bash
"$VENV_PIP" install -e "$SCRIPT_DIR[leafhub]" --quiet
```

> Alternatively, copy `leafhub_dist/setup_template.sh` to `setup.sh` — it includes this block and all the standard boilerplate pre-wired.

---

## Step 2 — pyproject.toml

```toml
[project.optional-dependencies]
leafhub = ["leafhub @ git+https://github.com/Rebas9512/Leafhub.git"]
```

Install in your setup.sh venv step:
```bash
"$VENV_PIP" install -e "$SCRIPT_DIR[leafhub]" --quiet
```

---

## Step 3 — Runtime credential resolution

### Supported provider types

LeafHub supports both API-key-based and OAuth-based providers. The SDK handles both transparently:

| Provider type | Auth mode | How keys are stored |
|---------------|-----------|---------------------|
| OpenAI / Anthropic / Ollama | `bearer` / `x-api-key` / `none` | Static API key, AES-256-GCM encrypted |
| OpenAI Codex (ChatGPT subscription) | `openai-oauth` | OAuth refresh token; access tokens auto-refreshed on every `get_config()` call |

Application code does not need to distinguish between these — `get_config()` always returns a standard `bearer` auth mode with a valid access token, regardless of the underlying provider type.

### OAuth / Codex integration notes

If your project calls the OpenAI Codex endpoint (ChatGPT subscription), be aware of several differences from the standard OpenAI Chat Completions API. These are the issues most likely to cause silent failures.

#### 1. `api_format` must be `openai-responses`, not `openai-completions`

The Codex endpoint uses the [Responses API](https://platform.openai.com/docs/api-reference/responses), which is a different protocol from Chat Completions. When `leafhub provider login` creates the provider, `api_format` is set to `openai-responses` automatically. If you see `404` errors or the URL contains `.../codex/responses/chat/completions`, the provider was created with the wrong format. Fix it:

```bash
leafhub provider show codex          # check api_format
# If it shows openai-completions, re-create:
leafhub provider delete codex
leafhub provider login --name codex
```

#### 2. Codex endpoint protocol differs from Chat Completions

The Responses API requires a different payload shape. If your code builds API requests manually (instead of using `get_config()` + a standard HTTP client), you must handle this:

| Field | Chat Completions | Codex / Responses API |
|-------|------------------|-----------------------|
| Messages | `messages: [{role, content}]` | `input: [{role, content}]` |
| System prompt | `messages[0].role = "system"` | `instructions: "..."` (required, top-level string) |
| Streaming | `stream: true` (optional) | `stream: true` (required) |
| Storage | not applicable | `store: false` (required) |
| Temperature | `temperature: 0.7` | **not supported** — omit |
| Max tokens | `max_tokens: N` | **not supported** — omit |
| Response format | `choices[0].message.content` | SSE `response.output_text.delta` events, or `output[].content[].text` (non-streaming fallback) |
| Image input | `{"type": "image_url", "image_url": {"url": "data:..."}}` | `{"type": "input_image", "image_url": "data:..."}` (plain string, not nested object) |
| Text input | `{"type": "text", "text": "..."}` | `{"type": "input_text", "text": "..."}` |

**`instructions` is required.** Unlike Chat Completions where a system message is optional, the Codex endpoint returns `400: Instructions are required` if you omit the `instructions` field. Always pass it, even for trivial requests (e.g. capability probes).

**Image input validation is stricter.** The Codex endpoint rejects very small images (e.g. 1×1 PNG) with `400: The image data you provided does not represent a valid image`. Use at least an 8×8 image for probes or tests.

Use `cfg.api_format` from `get_config()` to decide which payload shape to build.

#### 3. `auth_mode` is always `bearer` at the wire level

The SDK's `get_config()` translates `openai-oauth` → `bearer` before returning. Your code should **never** see `openai-oauth` as an auth mode. If you do, you are reading the raw provider record from the DB instead of going through `get_config()`. The access token is a standard Bearer token — set `Authorization: Bearer <api_key>` like any other OpenAI call.

#### 4. Credentials must be refreshed before every request

OAuth access tokens expire after ~1 hour. If your project caches credentials at module load time (e.g. reading env vars into module-level constants), those values will go stale during long-running sessions. The recommended pattern:

```python
# Bad — frozen at import time, stale after token refresh:
API_KEY = os.getenv("REWRITE_API_KEY")

# Good — read dynamically on each request:
api_key = os.getenv("REWRITE_API_KEY") or CACHED_FALLBACK
```

If your project uses LeafHub's `resolve_credentials()` → `os.environ` injection pattern, call `resolve_credentials()` before every request so the env vars are updated with fresh tokens.

#### 5. `leafhub provider list --json` for programmatic label parsing

When auto-binding in setup scripts or CLI tools, always use `--json` to get provider labels:

```python
result = subprocess.run(["leafhub", "provider", "list", "--json"],
                        capture_output=True, text=True)
providers = json.loads(result.stdout)
label = providers[0].get("label") if providers else None
```

Text-mode output may truncate or pad labels. Splitting on whitespace only gets the first word, which breaks multi-word labels like `"OpenAI Codex"`.

#### 6. Editable install (`pip install -e`) path can go stale

If the LeafHub source directory is moved or renamed after `pip install -e .`, the `.pth` file in `site-packages` will point to the old path. `open_sdk()` will raise `ImportError` and credential resolution will silently fail. Fix:

```bash
pip install -e /path/to/Leafhub    # re-install with correct path
```

This is not OAuth-specific, but OAuth providers surface it first because they require the full SDK (not just the distributed `probe.py`).

### Minimal pattern (detect → open_sdk → get_key)

```python
import os

try:
    from leafhub.probe import detect          # pip package (preferred)
except ImportError:
    from leafhub_dist.probe import detect     # distributed copy (fallback)

def resolve_credentials(alias: str) -> dict | None:
    """Resolve API credentials via LeafHub, with env var fallback.

    Returns a dict with keys: source, api_key, base_url, model
    Returns None if no credentials are found.
    """
    result = detect()
    if result.ready:
        try:
            hub = result.open_sdk()
            cfg = hub.get_config(alias)
            # cfg is a ProviderConfig with fields:
            #   api_key, base_url, model, api_format, auth_mode, auth_header, extra_headers
            # For OAuth providers, api_key is a fresh access token (auto-refreshed).
            # auth_mode is always "bearer" at the wire level (even for openai-oauth providers).
            return {"source": "leafhub", **cfg}
        except Exception:
            pass

    # Env var fallback (CI / advanced usage)
    key = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY")
    if key:
        return {
            "source": "env",
            "api_key": key,
            "base_url": os.getenv("API_BASE_URL", ""),
            "model": os.getenv("API_MODEL", ""),
        }
    return None
```

### Injecting into os.environ at startup

If your framework reads credentials from environment variables, resolve and inject them early in the startup sequence before importing any model or API code:

```python
def load_credentials(alias: str) -> None:
    creds = resolve_credentials(alias)
    if creds:
        os.environ.setdefault("API_KEY",      creds.get("api_key", ""))
        os.environ.setdefault("API_BASE_URL", creds.get("base_url", ""))
        os.environ.setdefault("API_MODEL",    creds.get("model", ""))
        os.environ["CREDENTIAL_SOURCE"] = creds["source"]
```

Call `load_credentials("my-alias")` at the top of your server launcher, before any imports that read those env vars.

---

## Optional: CLI setup command

For projects with a `trileaf setup`-style self-repair command, use this pattern. It mirrors what LeafHub writes at registration time and repairs the three most common failure modes: missing pip package, missing models, and missing binding.

```python
import json, shutil, subprocess, sys
from pathlib import Path

_ROOT  = Path(__file__).resolve().parent   # project root
_ALIAS = "my-alias"                         # must match hub.get_key() calls

def cmd_setup(args) -> None:
    """Self-repair: pip deps → LeafHub binding → project-specific steps."""

    # 1. Install leafhub pip package if missing
    try:
        import leafhub                       # noqa: F401
    except ImportError:
        print("[setup] Installing leafhub pip package ...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", f"{_ROOT}[leafhub]", "--quiet"],
            check=True,
        )

    # 2. Verify / auto-repair LeafHub binding
    _ensure_binding()

    # 3. Project-specific steps (model downloads, DB migrations, etc.)
    # ...

    raise SystemExit(0)


def _ensure_binding() -> bool:
    """Token-first binding check: reads project name from .leafhub.
    Returns True when credentials resolve successfully."""
    dotfile = _ROOT / ".leafhub"
    if not dotfile.exists():
        print("[setup] .leafhub not found — run setup.sh to register.")
        return False

    leafhub_bin = shutil.which("leafhub")
    if not leafhub_bin:
        print("[setup] leafhub binary not found — install LeafHub first.")
        return False

    # Fast path: full credential resolution
    try:
        from leafhub_dist.probe import detect
        if detect().ready:
            hub = detect().open_sdk()
            hub.get_key(_ALIAS)
            return True
    except Exception:
        pass

    # Read actual project name from dotfile (never hardcode)
    try:
        project = json.loads(dotfile.read_text())["project"]
    except Exception:
        return False

    # Check project health
    show = subprocess.run([leafhub_bin, "project", "show", project],
                          capture_output=True, text=True)
    if show.returncode != 0 or "not found" in show.stdout.lower():
        print(f"[setup] Project '{project}' not found in vault — re-run setup.sh.")
        return False

    if _ALIAS in show.stdout:
        return False   # binding present but key resolution failed; surface as-is

    # Attempt auto-bind to first available provider
    prov = subprocess.run([leafhub_bin, "provider", "list"],
                          capture_output=True, text=True)
    provider_name = next(
        (l.strip().split()[0] for l in prov.stdout.splitlines()
         if l.strip() and not l.strip().startswith(("─", "Label", "Provider"))),
        None,
    )
    if not provider_name:
        print(f"[setup] No providers in vault — add one: leafhub manage")
        return False

    result = subprocess.run(
        [leafhub_bin, "project", "bind", project, "--alias", _ALIAS, "--provider", provider_name],
        capture_output=True, text=True,
    )
    return result.returncode == 0
```

---

## Environment variables

| Variable | Effect |
|----------|--------|
| `LEAFHUB_HEADLESS=1` | Skip all interactive prompts (set before `leafhub_setup_project`) |
| `LEAFHUB_CALLER=1` | Set automatically by LeafHub when it invokes your `setup.sh`; prevents recursion |
| `LEAFHUB_DIR=<path>` | Custom LeafHub install directory (forwarded to the installer if it runs) |

---

## Troubleshooting

### `credentials: none` at runtime

Run in order:

```bash
leafhub project show <project-name>     # check binding exists
leafhub status                          # check vault health
my-project setup                        # if project has a setup command — auto-repairs
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Bindings: (none)` | Provider not bound | `leafhub project bind <name> --alias <alias> --provider <prov>` |
| `project not found in vault` | `.leafhub` token is stale | Re-run `setup.sh` |
| `leafhub pip package not installed` | `open_sdk()` fails silently | `pip install -e ".[leafhub]"` |
| Binding alias is `default` not `<alias>` | Registered without `--alias` | `leafhub project bind <name> --alias <alias> --provider <prov>` |

### Alias mismatch (most common)

```
setup.sh:      leafhub_setup_project "myapp" "$SCRIPT_DIR" "rewrite"
runtime code:  hub.get_key("default")   ← wrong — must match "rewrite"
```

Always verify:
```bash
leafhub project show myapp
# Bindings should list:
#   rewrite  →  ProviderName
```

### OAuth token expired / refresh failed

If you use an OpenAI Codex OAuth provider and see authentication errors:
```bash
leafhub provider login --name codex   # re-authenticate via browser
```

OAuth access tokens expire after ~1 hour. The SDK refreshes them automatically, but if the refresh token itself is revoked (e.g. password change, session invalidation), you must re-authenticate.

### Stale token after vault reset

If the vault was wiped or the project was deleted and re-created:
```bash
# Re-register with the exact name shown in the error message
leafhub register <name> --path <project-dir> --alias <alias>
```

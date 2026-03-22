# LeafHub Integration

This directory (`leafhub_dist/`) is written into your project root the first time you run `leafhub register`. It contains everything needed to integrate with LeafHub — offline-capable at both setup time and runtime.

| File | Purpose |
|------|---------|
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
            cfg = hub.get_config(alias)   # {api_key, base_url, model, auth_mode, ...}
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

### Stale token after vault reset

If the vault was wiped or the project was deleted and re-created:
```bash
# Re-register with the exact name shown in the error message
leafhub register <name> --path <project-dir> --alias <alias>
```

"""
leafhub_probe — LeafHub auto-detection and project registration
===============================================================

PURPOSE
-------
This module lets any project detect whether it is linked to LeafHub,
register itself if not, and open the SDK to retrieve API keys — all without
manual token management or hard-coded credentials.

Distribute this file in two ways:

  Option A — installed package (``pip install leafhub``)::

      from leafhub.probe import detect, register, ProbeResult

  Option B — standalone copy in your project root::

      # Copy leafhub_probe.py next to your onboarding script.
      # Zero external dependencies — pure stdlib.
      from leafhub_probe import detect, register, ProbeResult

The file is intentionally self-contained so it can be dropped into any
project without adding new dependencies.

────────────────────────────────────────────────────────────────────────────

CONCEPTS
--------

.leafhub dotfile
    A JSON file written into the project directory by LeafHub when you link
    a project.  Contains a short-lived project token (rotated on every link).
    ``detect()`` walks up the directory tree to find it (same as git + ``.git``).

Project token  (``lh-proj-<32 hex>``)
    Authenticates the project with the LeafHub SDK.  Written to ``.leafhub``
    with chmod 600.  Rotated whenever the project is re-linked.  Never commit it.

Provider
    A named API endpoint stored in LeafHub: label, base URL, API format,
    API key (AES-256-GCM encrypted), default model.

Alias
    A short name that binds a provider to a project.  Your code calls
    ``hub.get_key("my-alias")`` and LeafHub resolves which provider (and key)
    that alias maps to for this project.

    Example flow::

        LeafHub:  project "myapp" has binding  alias="chat" → provider "OpenAI-GPT4"
        SDK:      hub = found.open_sdk()
                  key = hub.get_key("chat")     # returns the encrypted API key
                  cfg = hub.get_config("chat")  # returns key + base_url + model

    Store the alias in your project config (e.g. ``.env`` → ``LEAFHUB_ALIAS=chat``).
    Never hard-code the key itself.

────────────────────────────────────────────────────────────────────────────

DETECTION FLOW  (detect)
------------------------
``detect(project_dir, port, timeout)`` — always fast, never raises.

Four checks run in order.  All failures are silent (reflected in result fields):

  1. ``.leafhub`` dotfile  (filesystem walk)
       Walks up from *project_dir* looking for a ``.leafhub`` file.
       Stops at the first entry found, valid or not (same rule git uses for ``.git``).
       ``found.ready`` is True when dotfile exists AND contains a non-empty token.

  2. Manage server  (TCP probe)
       Attempts ``socket.create_connection(("127.0.0.1", port), timeout)``.
       ``found.server_running`` is True when the port answers.
       ``found.server_url`` / ``found.manage_url`` give the base HTTP URL.

  3. CLI binary  (PATH lookup)
       ``shutil.which("leafhub")`` — searches ``$PATH`` for the leafhub binary.
       ``found.cli_available`` / ``found.cli_path`` report the result.

  4. SDK importable  (import machinery)
       ``importlib.util.find_spec("leafhub.sdk")`` — True only when the full
       LeafHub package (not just the probe) is installed in this interpreter.
       Required for ``found.open_sdk()`` and ``hub.get_key()``.

Timing: at most *timeout* seconds (default 1 s), dominated by the TCP probe.
Safe to call at import time or in a startup health check.

────────────────────────────────────────────────────────────────────────────

REGISTRATION FLOW  (register)
------------------------------
``register(project_name, project_dir, *, probe, port, timeout)``
→ creates a LeafHub project, links the directory, runs the binding wizard,
   returns a fresh ``ProbeResult`` with ``ready=True``.

Call this from your onboarding wizard when ``found.ready`` is False.
It is interactive: the binding wizard reads from stdin (skipped in CI / non-TTY).

Step-by-step:

  1. Stale-token check
       If ``found.ready`` is True but the token is expired or the server deleted
       the project, ``open_sdk()`` raises ``InvalidTokenError``.
       ``register()`` detects this, deletes the stale ``.leafhub``, and continues.

  2. REST API path  (preferred — stdlib urllib, no third-party deps)
       ``POST /admin/projects``  {"name": project_name}
           → creates project, returns {"id": "...", ...}
       ``POST /admin/projects/{id}/link``  {"path": str(proj_dir), "copy_probe": false}
           → rotates token, writes ``.leafhub`` to the project directory,
             stores the path in the LeafHub DB.

       Pass ``copy_probe: false`` when your project already distributes this file.
       LeafHub would otherwise overwrite it with the version bundled in the server.

  3. CLI fallback  (when server is unreachable but ``leafhub`` binary exists)
       ``leafhub project create <name> --path <dir> --no-probe``
       If the name already exists: ``leafhub project link <name> --path <dir> --no-probe``

  4. Interactive binding wizard  (TTY only — skipped in CI)
       After linking, asks the user to bind a provider to the project:
         • REST path → ``_bind_wizard_rest``:  lists providers via GET /admin/providers,
           lets user pick one or create a new provider (label, format, URL, key, model),
           sets an alias via PUT /admin/projects/{id}.
         • CLI path  → ``_bind_wizard_cli``:  runs ``leafhub provider list`` then
           ``leafhub project bind <name> --alias <a> --provider <p>``.

  5. Re-probe and verify
       Calls ``detect()`` again to confirm ``.leafhub`` is on disk and ``ready=True``.
       Raises ``RuntimeError`` (with a copy-pasteable manual command) if it still fails.

Registration methods are mutually exclusive — REST is tried first, CLI only
runs when ``not linked``.  The binding wizard step matches the path used for
the initial link.

────────────────────────────────────────────────────────────────────────────

RESULT FIELDS  (ProbeResult)
-----------------------------
Fields (all default to "not found"):

    dotfile_path   Path | None   Absolute path to .leafhub, or None
    dotfile_data   dict | None   Parsed JSON payload from the dotfile
    server_url     str | None    "http://127.0.0.1:<port>" when server is up
    server_running bool          True when manage server answered the probe
    cli_path       str | None    Absolute path to leafhub binary, or None
    sdk_importable bool          True when full SDK (leafhub.sdk) is importable

Computed properties:

    .ready          True when dotfile exists and contains a valid non-empty token.
                    Required before calling open_sdk().
    .can_link       True when at least one of: server running, CLI available,
                    SDK importable.  Use to decide whether register() can proceed.
    .cli_available  Shorthand for cli_path is not None.
    .manage_url     server_url if detected, otherwise "http://127.0.0.1:8765".
    .project_name   Value of "project" in the dotfile, or None.

Methods:

    .open_sdk(hub_dir=None) → LeafHub
        Returns a ready LeafHub instance using the dotfile token.
        Raises RuntimeError if not ready, ImportError if SDK not installed,
        InvalidTokenError if token has been rotated or project deleted.

────────────────────────────────────────────────────────────────────────────

INTEGRATION PATTERNS FOR NEW PROJECTS
--------------------------------------

Pattern 1 — Full onboarding wizard (interactive, covers all cases)::

    from leafhub.probe import detect, register

    PROJECT_ROOT = Path(__file__).parent

    def step_configure_provider():
        found = detect(project_dir=PROJECT_ROOT)

        if found.ready:
            print(f"  LeafHub: linked as '{found.project_name}' ✓")
        elif found.can_link:
            # Server is running or CLI available — auto-register
            proj_name = input("  Project name to create in LeafHub [myapp]: ").strip() or "myapp"
            try:
                found = register(proj_name, project_dir=PROJECT_ROOT, probe=found)
                print(f"  [OK] Linked as '{proj_name}'.")
            except RuntimeError as exc:
                print(f"  Could not link: {exc}")
                return False   # fall back to manual API key
        else:
            print("  LeafHub not found — configure provider manually.")
            return False

        # At this point found.ready is True.
        # Ask the user which alias to use (must match what was set in binding wizard).
        alias = input("  LeafHub alias for this provider [rewrite]: ").strip() or "rewrite"

        # Persist alias + backend config to your project config file (.env, etc.)
        write_config({
            "LEAFHUB_ALIAS":   alias,
            "REWRITE_BACKEND": "external",
            "REWRITE_BASE_URL": ...,   # from hub.get_config(alias).base_url if needed
            "REWRITE_MODEL":    ...,   # from hub.get_config(alias).model if needed
        })
        return True

Pattern 2 — Runtime credential resolution (call at startup)::

    from leafhub.probe import detect

    def resolve_api_key(alias: str, project_dir=None) -> str | None:
        \"\"\"
        Return the decrypted API key for *alias*, or None if LeafHub is
        unavailable.  The caller falls back to os.environ or .env.
        \"\"\"
        found = detect(project_dir=project_dir)
        if not found.ready:
            return None
        try:
            return found.open_sdk().get_key(alias)
        except Exception:
            return None   # stale token, alias not bound, SDK not installed, etc.

    # Usage:
    key = resolve_api_key("rewrite") or os.environ.get("REWRITE_API_KEY", "")

Pattern 3 — Silent fallback for libraries / agent pipelines::

    from leafhub.probe import detect

    # Probe once at module load — completes in < 1 s.
    _LH = detect()

    def get_key(alias: str) -> str | None:
        if _LH.ready:
            try:
                return _LH.open_sdk().get_key(alias)
            except Exception:
                pass
        return None   # caller uses env var / manual config

Pattern 4 — CI / headless (non-interactive check)::

    from leafhub.probe import detect

    found = detect()
    if not found.ready:
        # CI: expect OPENAI_API_KEY to be set in the environment
        assert os.environ.get("OPENAI_API_KEY"), "Provide OPENAI_API_KEY in CI"
    else:
        key = found.open_sdk().get_key("chat")

────────────────────────────────────────────────────────────────────────────

DOTFILE FORMAT
--------------
Written by the LeafHub server.  Do not create or edit manually.

    {
      "version":   1,
      "project":   "myapp",
      "token":     "lh-proj-<32 hex chars>",
      "linked_at": "2025-01-15T10:30:00+00:00"
    }

  - Written with chmod 600 (owner read/write only).
  - Added to ``.gitignore`` automatically.  Never commit it.
  - Token is rotated on every ``link`` call (stale tokens are rejected).
  - The dotfile walk stops at the first entry found (valid or not), just like
    git stops at the first ``.git`` directory.

────────────────────────────────────────────────────────────────────────────

STANDALONE SNIPPET  (zero dependencies — detect only)
------------------------------------------------------
Copy this function if you only need detection and don't want the full file.
Returns a plain dict; the ``ProbeResult`` dataclass is NOT available::

    import importlib.util, json, shutil, socket
    from pathlib import Path

    def lh_detect(project_dir=None, port=8765, timeout=1.0):
        \"\"\"
        Minimal LeafHub detection — stdlib only, never raises.
        Returns a dict with the same keys as ProbeResult fields.
        \"\"\"
        start = Path(project_dir or Path.cwd()).resolve()

        # 1. .leafhub dotfile (walk up like git)
        dotfile = next(
            (d / ".leafhub" for d in [start, *start.parents]
             if (d / ".leafhub").is_file()),
            None,
        )
        data = None
        if dotfile:
            try:
                data = json.loads(dotfile.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = None
            except Exception:
                pass

        # 2. Manage server TCP probe
        running = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                running = True
        except OSError:
            pass

        return {
            "ready":          data is not None and bool((data or {}).get("token")),
            "dotfile_path":   dotfile,
            "dotfile_data":   data,
            "server_running": running,
            "server_url":     f"http://127.0.0.1:{port}" if running else None,
            "cli_available":  shutil.which("leafhub") is not None,
            "cli_path":       shutil.which("leafhub"),
            "sdk_importable": importlib.util.find_spec("leafhub.sdk") is not None,
        }

    # Usage:
    #   info = lh_detect()
    #   if info["ready"]:
    #       from leafhub import LeafHub
    #       hub = LeafHub(token=info["dotfile_data"]["token"])
    #       key = hub.get_key("my-alias")

────────────────────────────────────────────────────────────────────────────

NEW PROJECT INTEGRATION CHECKLIST
-----------------------------------
When adding LeafHub support to a new project:

  □  Copy this file to your project root as ``leafhub_probe.py`` OR
     add ``leafhub`` to your dependencies (``pip install leafhub``).

  □  Call ``detect(project_dir=PROJECT_ROOT)`` in your onboarding / startup.

  □  If ``found.can_link`` and not ``found.ready``, call
     ``register(project_name, project_dir=PROJECT_ROOT)`` to create + link.
     The interactive binding wizard runs automatically in TTY sessions.

  □  Choose an alias (e.g. ``"rewrite"``, ``"chat"``) and store it in your
     project config (``LEAFHUB_ALIAS`` in ``.env`` or equivalent).
     The alias must match what was set during the binding wizard.

  □  At runtime, call ``found.open_sdk().get_key(alias)`` to retrieve the key.
     Wrap in try/except and fall back to ``os.environ`` for CI / offline use.

  □  Add ``.leafhub`` to ``.gitignore`` (LeafHub does this automatically,
     but double-check when committing from a fresh clone).

────────────────────────────────────────────────────────────────────────────

NOTES
-----
- ``detect()`` never raises — failures are reflected in result fields.
- ``register()`` IS interactive (reads stdin) and DOES raise on failure.
  Always call it from a try/except block and provide a manual fallback.
- The stale-token check in ``register()`` requires ``sdk_importable=True``.
  Without the full SDK, ``register()`` trusts the dotfile and returns early.
- ``sdk_importable`` is True only when ``leafhub.sdk`` is importable, meaning
  the full LeafHub package (not just this probe) is installed.
- ``copy_probe=False`` (REST) / ``--no-probe`` (CLI) tells LeafHub not to
  overwrite your project's probe file during link.  Use this flag when your
  project already distributes a copy of this file.
- This file is safe to copy verbatim into any project.  The standard library
  is the only module-level dependency.  ``open_sdk()`` lazy-imports ``leafhub``
  and is only reachable when ``found.ready`` is True.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path


# ── Public result type ────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    """
    Result of a LeafHub presence detection.  Returned by :func:`detect`.

    All fields default to "not found" so you can construct partial results
    in tests without specifying every attribute.

    Attributes:
        dotfile_path:   Absolute path to the ``.leafhub`` file, or None.
        dotfile_data:   Parsed JSON from the dotfile (dict), or None.
        server_url:     Full base URL of the running manage server, or None.
        server_running: True when the manage server answered the port probe.
        cli_path:       Absolute path to the ``leafhub`` CLI binary, or None.
        sdk_importable: True when ``import leafhub`` would succeed in this
                        Python interpreter.
    """

    dotfile_path:   Path | None = None
    dotfile_data:   dict | None = None
    server_url:     str | None  = None
    server_running: bool        = False
    cli_path:       str | None  = None
    sdk_importable: bool        = False

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """
        True when a ``.leafhub`` dotfile is present and contains a non-empty
        token.  Call :meth:`open_sdk` to get a configured ``LeafHub`` instance.
        """
        return (
            self.dotfile_data is not None
            and bool(self.dotfile_data.get("token"))
        )

    @property
    def cli_available(self) -> bool:
        """True when the ``leafhub`` CLI binary is on PATH."""
        return self.cli_path is not None

    @property
    def can_link(self) -> bool:
        """
        True when at least one LeafHub component is present (server, CLI, or
        SDK) and can accept a link request.  Use this to guide the user to link
        their project when ``ready`` is False.
        """
        return self.server_running or self.cli_available or self.sdk_importable

    @property
    def manage_url(self) -> str:
        """
        URL of the LeafHub Manage UI.  Returns the detected server URL, or the
        default ``http://127.0.0.1:8765`` when the server has not been probed or
        is not running.
        """
        return self.server_url or "http://127.0.0.1:8765"

    @property
    def project_name(self) -> str | None:
        """The project name stored in the dotfile, or None when no dotfile found."""
        if self.dotfile_data:
            return self.dotfile_data.get("project")
        return None

    # ── SDK access ────────────────────────────────────────────────────────────

    def open_sdk(self, hub_dir: "str | Path | None" = None) -> "LeafHub":
        """
        Return a ready-to-use :class:`leafhub.LeafHub` instance using the
        token from the dotfile.

        Args:
            hub_dir: Override the LeafHub data directory (``~/.leafhub`` by
                     default).  Useful in tests or when running multiple
                     LeafHub instances.

        Raises:
            RuntimeError:      No valid ``.leafhub`` dotfile found.
            ImportError:       The ``leafhub`` package is not installed.
            InvalidTokenError: The token in the dotfile is invalid or revoked.

        Example::

            found = detect()
            if found.ready:
                hub = found.open_sdk()
                key = hub.get_key("chat")
        """
        if not self.ready:
            raise RuntimeError(
                "LeafHub is not linked to this project. "
                "Open the Manage UI and click 'Link Dir', or run:\n"
                "    leafhub project link <name> --path ."
            )
        if not self.sdk_importable:
            raise ImportError(
                "The leafhub package is not installed in this environment. "
                "Run:  pip install leafhub"
            )
        from leafhub import LeafHub  # noqa: PLC0415  (lazy import by design)

        token = self.dotfile_data["token"]  # type: ignore[index]
        return LeafHub(token=token, hub_dir=hub_dir)


# ── Detection function ────────────────────────────────────────────────────────

def detect(
    project_dir: "Path | str | None" = None,
    *,
    port: int = 8765,
    timeout: float = 1.0,
) -> ProbeResult:
    """
    Run all LeafHub detection checks and return a :class:`ProbeResult`.

    This function is intentionally fast (at most ``timeout`` seconds in the
    worst case) and **never raises** — failures are reflected in the result
    fields.  It is safe to call at import time.

    Args:
        project_dir: Directory to start the dotfile search from.
                     Defaults to ``Path.cwd()``.  The function walks up the
                     directory tree from here (like git looking for ``.git``).
        port:        TCP port to probe for the LeafHub manage server.
        timeout:     TCP connect timeout in seconds for the port probe.

    Returns:
        A :class:`ProbeResult` with all detection outcomes populated.

    Example::

        found = detect(project_dir=Path(__file__).parent)
        if found.ready:
            hub = found.open_sdk()
    """
    start = Path(project_dir or Path.cwd()).resolve()

    # ── 1. .leafhub dotfile (walk up the directory tree, like git) ───────────
    dotfile_path: "Path | None" = None
    dotfile_data: "dict | None" = None

    for directory in [start, *start.parents]:
        candidate = directory / ".leafhub"
        if candidate.is_file():          # is_file() returns False for directories
            try:
                raw    = candidate.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    dotfile_data = parsed
                    dotfile_path = candidate
            except (OSError, json.JSONDecodeError):
                pass
            break   # stop at the first .leafhub entry whether valid or not

    # ── 2. Manage server TCP probe ────────────────────────────────────────────
    server_running = False
    server_url: "str | None" = None

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            server_running = True
            server_url     = f"http://127.0.0.1:{port}"
    except OSError:
        pass

    # ── 3. leafhub CLI binary on PATH ─────────────────────────────────────────
    cli_path = shutil.which("leafhub")

    # ── 4. leafhub SDK importable in this interpreter ─────────────────────────
    sdk_importable = importlib.util.find_spec("leafhub") is not None

    return ProbeResult(
        dotfile_path   = dotfile_path,
        dotfile_data   = dotfile_data,
        server_url     = server_url,
        server_running = server_running,
        cli_path       = cli_path,
        sdk_importable = sdk_importable,
    )


# ── Binding wizard helpers (used by register()) ───────────────────────────────

def _prompt_add_provider_rest(server_url: str, timeout: float) -> "dict | None":
    """
    Collect provider info interactively and POST it to the running manage server.
    Returns the created provider dict, or None on failure / cancellation.
    """
    import sys
    import getpass
    import urllib.request
    import urllib.error

    # Guard: callers check isatty() before calling _bind_wizard_rest, but
    # _prompt_add_provider_rest can also be reached indirectly, so check again.
    if not sys.stdin.isatty():
        return None

    _FORMATS = ["openai-completions", "anthropic-messages", "ollama"]
    _DEFAULT_URLS = {
        "openai-completions": "https://api.openai.com/v1",
        "anthropic-messages": "https://api.anthropic.com",
        "ollama":             "http://localhost:11434",
    }
    _DEFAULT_MODELS = {
        "openai-completions": "gpt-4o",
        "anthropic-messages": "claude-3-5-sonnet-20241022",
        "ollama":             "llama3",
    }

    print("\nNew provider setup:")
    label = input("  Label: ").strip()
    if not label:
        print("  Cancelled.")
        return None

    print("  API format:")
    for i, f in enumerate(_FORMATS, 1):
        print(f"    [{i}] {f}")
    fmt_raw = input("  Choose [1-3] (default 1): ").strip() or "1"
    try:
        fmt = _FORMATS[int(fmt_raw) - 1]
    except (ValueError, IndexError):
        fmt = "openai-completions"

    default_url = _DEFAULT_URLS.get(fmt, "")
    base_url = input(f"  Base URL [{default_url}]: ").strip() or default_url

    default_model_val = _DEFAULT_MODELS.get(fmt, "")
    model = input(f"  Default model [{default_model_val}]: ").strip() or default_model_val

    if fmt == "ollama":
        key = ""
    else:
        key = getpass.getpass("  API key: ").strip()
        if not key:
            print("  API key is required.")
            return None

    payload = json.dumps({
        "label":         label,
        "api_format":    fmt,
        "base_url":      base_url,
        "default_model": model,
        "api_key":       key,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/admin/providers",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            provider = json.loads(resp.read().decode("utf-8"))
        print(f"  ✓ Provider '{label}' added.")
        return provider
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        print(f"  Failed to add provider: {detail}")
        return None
    except Exception as exc:
        print(f"  Failed to add provider: {exc}")
        return None


def _bind_wizard_rest(
    server_url: str,
    proj_id: str,
    project_name: str,
    timeout: float,
) -> None:
    """
    Interactive provider-binding wizard that talks to the running manage server.
    Silently skips when stdin is not a TTY.
    """
    import sys
    import urllib.request
    import urllib.error

    if not sys.stdin.isatty():
        return

    def _get(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _put(url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    while True:
        try:
            providers = _get(f"{server_url}/admin/providers").get("data", [])
        except Exception:
            return  # server unreachable mid-session

        print()
        chosen: "dict | None" = None

        if providers:
            print("Bind a provider to this project?")
            print("Available providers:")
            for i, p in enumerate(providers, 1):
                print(f"  [{i}] {p['label']}  ({p['api_format']})")
            print("  [n] Add a new provider")
            print(
                f"  [s] Skip  "
                f"(run later: leafhub project bind {project_name} --alias <alias> --provider <name>)"
            )
            print()
            choice = input("Choice: ").strip().lower()
        else:
            print("No providers configured yet.")
            yn = input("Add a provider and bind it now? [Y/n]: ").strip().lower()
            if yn in ("", "y", "yes"):
                choice = "n"
            else:
                print(
                    f"  Bind later: "
                    f"leafhub project bind {project_name} --alias <alias> --provider <name>"
                )
                return

        if choice in ("s", "skip", ""):
            print(
                f"  Bind later: "
                f"leafhub project bind {project_name} --alias <alias> --provider <name>"
            )
            return

        if choice == "n":
            chosen = _prompt_add_provider_rest(server_url, timeout)
            if chosen is None:
                return
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(providers):
                    chosen = providers[idx]
                else:
                    print("  Invalid choice — binding skipped.")
                    return
            except ValueError:
                print("  Invalid choice — binding skipped.")
                return

        alias = input(f"  Alias for '{chosen['label']}' (e.g. 'chat', 'openai'): ").strip()
        if not alias:
            print("  No alias — binding skipped.")
            return

        try:
            proj_data = _get(f"{server_url}/admin/projects/{proj_id}")
            existing = [
                {"alias": b["alias"], "provider_id": b["provider_id"],
                 "model_override": b.get("model_override")}
                for b in proj_data.get("bindings", [])
            ]
            _put(
                f"{server_url}/admin/projects/{proj_id}",
                {"bindings": existing + [{"alias": alias, "provider_id": chosen["id"]}]},
            )
            print(f"✓ Bound alias '{alias}' → '{chosen['label']}' in project '{project_name}'.")
        except Exception as exc:
            print(f"  Binding failed: {exc}")
            print(
                f"  Run: leafhub project bind {project_name} "
                f"--alias {alias} --provider {chosen['label']}"
            )
            return

        again = input("  Add another binding? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            return


def _bind_wizard_cli(cli_path: str, project_name: str) -> None:
    """
    Interactive binding wizard using the ``leafhub`` CLI binary as a backend.
    Prints the current provider list and lets the user pick a name, then runs
    ``leafhub project bind``.  Silently skips when stdin is not a TTY.
    """
    import sys
    import subprocess

    if not sys.stdin.isatty():
        return

    while True:
        print()
        # Print the provider list directly so the user can see their options.
        r = subprocess.run([cli_path, "provider", "list"], text=True)
        if r.returncode != 0:
            return

        provider_name = input(
            "Provider to bind (enter label, or press Enter to skip): "
        ).strip()
        if not provider_name:
            print(
                f"  Bind later: "
                f"leafhub project bind {project_name} --alias <alias> --provider <name>"
            )
            return

        alias = input(f"  Alias for '{provider_name}' (e.g. 'chat', 'openai'): ").strip()
        if not alias:
            print("  No alias — binding skipped.")
            return

        r = subprocess.run(
            [cli_path, "project", "bind", project_name,
             "--alias", alias, "--provider", provider_name],
            text=True,
        )
        if r.returncode != 0:
            print(
                f"  Binding failed. Run manually:\n"
                f"    leafhub project bind {project_name} "
                f"--alias {alias} --provider {provider_name}"
            )
            return

        again = input("  Add another binding? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            return


# ── Project registration ──────────────────────────────────────────────────────

def register(
    project_name: str,
    project_dir: "Path | str | None" = None,
    *,
    probe: "ProbeResult | None" = None,
    port: int = 8765,
    timeout: float = 5.0,
) -> "ProbeResult":
    """
    Create and link a LeafHub project for *project_dir*.

    Tries the REST API first (stdlib ``urllib``, no third-party deps), then
    falls back to the ``leafhub`` CLI binary.  After a successful link the
    ``.leafhub`` dotfile is present and :attr:`ProbeResult.ready` is True.

    Parameters
    ----------
    project_name:
        Name to create in the LeafHub Manage UI (e.g. ``"trileaf"``).
    project_dir:
        Project root to link.  Defaults to ``Path.cwd()``.
    probe:
        Pre-computed :class:`ProbeResult` from a prior :func:`detect` call.
        When provided, skips the duplicate TCP / CLI probes.
    port:
        Manage server TCP port (ignored when *probe* is supplied).
    timeout:
        Timeout in seconds for HTTP calls and the port probe.

    Returns
    -------
    ProbeResult
        A fresh result from :func:`detect` after successful registration.
        ``result.ready`` is ``True``.

    Raises
    ------
    RuntimeError
        When all registration methods fail.  The message includes a
        copy-pasteable manual fallback command.

    Example::

        from leafhub.probe import detect, register

        found = detect()
        if not found.ready:
            found = register("my-project")   # links cwd
        hub = found.open_sdk()
        key = hub.get_key("openai")
    """
    import subprocess
    import urllib.request
    import urllib.error

    proj_dir = Path(project_dir or Path.cwd()).resolve()
    info: ProbeResult = probe if probe is not None else detect(proj_dir, port=port, timeout=timeout)

    # Already linked — validate the token before declaring success.
    # The dotfile can be stale if the server was restarted or the project deleted.
    if info.ready:
        if info.sdk_importable:
            try:
                info.open_sdk()
                return info  # Token valid, nothing to do.
            except Exception:
                # Token invalid/expired — delete stale dotfile and re-register.
                if info.dotfile_path and info.dotfile_path.exists():
                    info.dotfile_path.unlink(missing_ok=True)
                info = detect(proj_dir, port=port, timeout=timeout)
        else:
            return info  # SDK not installed, can't validate — trust the dotfile.

    if not info.can_link:
        raise RuntimeError(
            "LeafHub is not available (no server, CLI, or SDK found).\n"
            f"  Start LeafHub and re-run, or link manually:\n"
            f"  leafhub project create {project_name} --path {proj_dir}"
        )

    last_error = "no registration method succeeded"
    linked     = False
    # Exactly one of rest_proj_id / used_cli will be set after a successful
    # link — the CLI fallback only runs when `not linked`, so they are mutually
    # exclusive.  The if-elif below in the binding wizard section relies on this.
    rest_proj_id: "str | None" = None   # set when REST path succeeds
    used_cli     = False                 # set when CLI fallback succeeds

    # ── REST API (urllib — stdlib only) ───────────────────────────────────
    if not linked and info.server_running and info.server_url:
        def _post(url: str, payload: dict) -> dict:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            created = _post(f"{info.server_url}/admin/projects", {"name": project_name})
            proj_id = created.get("id")
            if not proj_id:
                raise ValueError(f"unexpected response (no 'id'): {created}")
            _post(
                f"{info.server_url}/admin/projects/{proj_id}/link",
                {"path": str(proj_dir), "copy_probe": False},
            )
            rest_proj_id = proj_id
            linked = True
        except Exception as exc:
            last_error = f"REST API: {exc}"

    # ── CLI fallback ──────────────────────────────────────────────────────
    if not linked and info.cli_available and info.cli_path:
        def _run(*args: str) -> bool:
            r = subprocess.run(
                [info.cli_path, *args],
                capture_output=True,
                text=True,
            )
            nonlocal last_error
            if r.returncode != 0:
                last_error = f"CLI: {(r.stderr or r.stdout).strip()[:200]}"
            return r.returncode == 0

        ok = _run("project", "create", project_name, "--path", str(proj_dir), "--no-probe")
        if not ok:
            # Name may already exist — try linking instead.
            ok = _run("project", "link", project_name, "--path", str(proj_dir), "--no-probe")
        if ok:
            linked = True
            used_cli = True

    if not linked:
        raise RuntimeError(
            f"Could not register '{project_name}' with LeafHub.\n"
            f"  {last_error}\n"
            f"  Manual: leafhub project create {project_name} --path {proj_dir}"
        )

    # ── Interactive provider binding ───────────────────────────────────────
    if rest_proj_id and info.server_url:
        _bind_wizard_rest(info.server_url, rest_proj_id, project_name, timeout)
    elif used_cli and info.cli_path:
        _bind_wizard_cli(info.cli_path, project_name)

    # Confirm .leafhub was written.
    result = detect(proj_dir, port=port, timeout=timeout)
    if not result.ready:
        raise RuntimeError(
            f"Registration reported success but .leafhub was not found in {proj_dir}.\n"
            f"  Manual: leafhub project create {project_name} --path {proj_dir}"
        )
    return result


# ── Convenience re-export ─────────────────────────────────────────────────────

__all__ = ["ProbeResult", "detect", "register"]

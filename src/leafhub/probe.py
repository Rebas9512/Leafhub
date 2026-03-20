"""
leafhub_probe — LeafHub auto-detection for project onboarding
==============================================================

PURPOSE
-------
This module (or the standalone copy ``leafhub_probe.py`` placed in your
project root by LeafHub) lets any project detect whether it is already
linked to LeafHub and open the SDK without manual token management.

LeafHub writes a ``.leafhub`` dotfile into your project directory when you
click "Link Dir" in the Manage UI (or run the ``link`` endpoint).  This
module walks up the directory tree looking for that file, then reports what
else is available (running server, CLI, installed SDK).

QUICK START
-----------
Use it at the top of your onboarding / startup script::

    # Option A: installed package (pip install leafhub)
    from leafhub.probe import detect, ProbeResult

    # Option B: standalone copy (leafhub_probe.py in project root)
    from leafhub_probe import detect, ProbeResult

    found = detect()          # searches from cwd by default

    if found.ready:
        # Already linked — open the SDK and go
        hub = found.open_sdk()
        key = hub.get_key("chat")          # raw API key string
        client = hub.openai("chat")        # openai.OpenAI instance

    elif found.server_running:
        print(f"LeafHub is running at {found.manage_url}")
        print("Open the UI and click 'Link Dir' to link this project.")

    elif found.can_link:
        print("LeafHub is installed but this project is not linked yet.")
        print(f"Open {found.manage_url} and link this directory.")

    else:
        # LeafHub not found — fall back to manual config
        api_key = os.environ["OPENAI_API_KEY"]

DETECTION CHECKS (in order)
----------------------------
1. ``.leafhub`` dotfile
     Walk up from *project_dir* (like git) looking for a ``.leafhub`` file.
     If found, read the JSON payload — it contains the project token.
     ``found.ready`` is True when a valid token is present.

2. Manage server (TCP probe)
     Try to connect to ``127.0.0.1:<port>`` (default 8765).
     ``found.server_running`` is True when a server answers.
     ``found.manage_url`` gives the full base URL.

3. CLI binary (``leafhub`` on PATH)
     ``found.cli_available`` is True when ``shutil.which("leafhub")`` succeeds.
     ``found.cli_path`` gives the absolute path to the binary.

4. SDK importable in current interpreter
     ``found.sdk_importable`` is True when ``import leafhub`` would succeed.

RESULT FIELDS
-------------
ProbeResult fields:

    dotfile_path   Path | None   Absolute path to .leafhub if found
    dotfile_data   dict | None   Parsed JSON from the dotfile
    server_url     str | None    "http://127.0.0.1:<port>" if server is up
    server_running bool          True when manage server answered
    cli_path       str | None    Absolute path to leafhub binary or None
    sdk_importable bool          True when leafhub package is installed

ProbeResult computed properties:

    .ready          dotfile found and contains a token (can call open_sdk())
    .can_link       at least one of: server running, CLI available, SDK installed
    .cli_available  shorthand for cli_path is not None
    .manage_url     server_url if detected, else "http://127.0.0.1:8765"
    .project_name   value of "project" field in the dotfile, or None

TYPICAL PATTERNS
----------------

Pattern 1 — Auto-configure, fall back to manual::

    from leafhub_probe import detect

    def get_api_key(alias="chat"):
        found = detect()
        if found.ready:
            return found.open_sdk().get_key(alias)
        return os.environ["OPENAI_API_KEY"]

Pattern 2 — Onboarding wizard::

    from leafhub_probe import detect

    found = detect()
    if found.ready:
        print(f"LeafHub linked as '{found.project_name}' — skipping setup.")
    elif found.server_running:
        print(f"Step 1: open {found.manage_url}")
        print("Step 2: create a project and click 'Link Dir' for this repo.")
    else:
        print("LeafHub not found. Install it: pip install leafhub")
        print("Then run: leafhub serve &  and link this project from the UI.")

Pattern 3 — Silent fallback (for libraries / agent pipelines)::

    from leafhub_probe import detect

    _found = detect()

    def resolve_key(alias):
        if _found.ready:
            try:
                return _found.open_sdk().get_key(alias)
            except Exception:
                pass
        return None   # caller falls back to env var / config file

DOTFILE FORMAT
--------------
The ``.leafhub`` file contains JSON with these keys::

    {
      "version":   1,
      "project":   "<project name as set in Manage UI>",
      "token":     "lh-proj-<32 hex chars>",
      "linked_at": "2025-01-15T10:30:00+00:00"
    }

The file is created with chmod 600 (owner read/write only) and is added
to ``.gitignore`` automatically when LeafHub writes it.  Never commit it.

STANDALONE SNIPPET (zero dependencies)
---------------------------------------
If you don't want to distribute this whole file, copy just this function
into your onboarding script.  It has zero runtime dependencies::

    import importlib.util, json, shutil, socket
    from pathlib import Path

    def lh_detect(project_dir=None, port=8765):
        \"\"\"Minimal LeafHub detection — stdlib only.\"\"\"
        start = Path(project_dir or Path.cwd()).resolve()
        dotfile = next(
            (d / ".leafhub" for d in [start, *start.parents]
             if (d / ".leafhub").is_file()),   # is_file(): skip dirs named .leafhub
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
        running = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                running = True
        except OSError:
            pass
        return {
            "ready":          data is not None and bool((data or {}).get("token")),
            "dotfile":        dotfile,
            "dotfile_data":   data,
            "server_running": running,
            "server_url":     f"http://127.0.0.1:{port}" if running else None,
            "cli_available":  shutil.which("leafhub") is not None,
            "sdk_importable": importlib.util.find_spec("leafhub") is not None,
        }

    # Usage:
    #   info = lh_detect()
    #   if info["ready"]:
    #       from leafhub import LeafHub
    #       hub = LeafHub(token=info["dotfile_data"]["token"])

NOTES
-----
- ``detect()`` never raises — failures are reflected in result fields.
- ``detect()`` completes in at most ``timeout`` seconds (default 1 s), so it
  is safe to call at module import time or in a startup health check.
- The dotfile walk stops at the first ``.leafhub`` entry whether it is valid
  or not (same rule git uses for ``.git``).
- This file is safe to copy verbatim into any project.  The only stdlib
  dependency used at module level is the standard library itself.
  ``open_sdk()`` imports ``leafhub`` lazily — it is only called when
  ``found.ready`` is True and you explicitly invoke it.
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


# ── Convenience re-export ─────────────────────────────────────────────────────

__all__ = ["ProbeResult", "detect"]

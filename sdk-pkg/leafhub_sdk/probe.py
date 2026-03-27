"""
LeafHub presence detection — stdlib only, zero external dependencies.

This module detects whether the current project is linked to a LeafHub vault
and provides access to the full LeafHub SDK when available.

Usage::

    from leafhub_sdk.probe import detect

    result = detect()
    if result.ready:
        hub = result.open_sdk()
        key = hub.get_key("my-alias")
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProbeResult:
    """
    Result of a LeafHub presence detection.  Returned by :func:`detect`.

    All fields default to "not found" so you can construct partial results
    in tests without specifying every attribute.
    """

    dotfile_path:   Path | None = None
    dotfile_data:   dict | None = None
    server_url:     str | None  = None
    server_running: bool        = False
    cli_path:       str | None  = None
    sdk_importable: bool        = False

    @property
    def ready(self) -> bool:
        """True when ``.leafhub`` exists and contains a non-empty token."""
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
        """True when at least one LeafHub component can accept a link request."""
        return self.server_running or self.cli_available or self.sdk_importable

    @property
    def manage_url(self) -> str:
        """URL of the LeafHub Manage UI (detected or default)."""
        return self.server_url or "http://127.0.0.1:8765"

    @property
    def project_name(self) -> str | None:
        """Project name from the dotfile, or None."""
        if self.dotfile_data:
            return self.dotfile_data.get("project")
        return None

    def open_sdk(self, hub_dir: str | Path | None = None):
        """
        Return a ready-to-use ``leafhub.LeafHub`` instance using the dotfile token.

        Raises:
            RuntimeError:  No valid ``.leafhub`` dotfile found.
            ImportError:   The ``leafhub`` package is not installed.
        """
        if not self.ready:
            raise RuntimeError(
                "LeafHub is not linked to this project. "
                "Run:  leafhub project link <name> --path ."
            )
        if not self.sdk_importable:
            raise ImportError(
                "The leafhub package is not installed. "
                "Run:  pip install leafhub"
            )
        from leafhub import LeafHub  # noqa: PLC0415

        token = self.dotfile_data["token"]  # type: ignore[index]
        return LeafHub(token=token, hub_dir=hub_dir)


def detect(
    project_dir: Path | str | None = None,
    *,
    port: int = 8765,
    timeout: float = 1.0,
) -> ProbeResult:
    """
    Run all LeafHub detection checks and return a :class:`ProbeResult`.

    Never raises — failures are reflected in the result fields.
    Completes in at most *timeout* seconds.

    Args:
        project_dir: Directory to start the dotfile search from (default: cwd).
        port:        TCP port to probe for the manage server.
        timeout:     TCP connect timeout in seconds.
    """
    start = Path(project_dir or Path.cwd()).resolve()

    # 1. .leafhub dotfile (walk up like git looks for .git)
    dotfile_path: Path | None = None
    dotfile_data: dict | None = None

    for directory in [start, *start.parents]:
        candidate = directory / ".leafhub"
        if candidate.is_file():
            try:
                raw = candidate.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    dotfile_data = parsed
                    dotfile_path = candidate
            except (OSError, json.JSONDecodeError):
                pass
            break  # stop at first .leafhub whether valid or not

    # 2. Manage server TCP probe
    server_running = False
    server_url: str | None = None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            server_running = True
            server_url = f"http://127.0.0.1:{port}"
    except OSError:
        pass

    # 3. CLI binary on PATH
    cli_path = shutil.which("leafhub")

    # 4. SDK importable
    sdk_importable = importlib.util.find_spec("leafhub") is not None

    return ProbeResult(
        dotfile_path=dotfile_path,
        dotfile_data=dotfile_data,
        server_url=server_url,
        server_running=server_running,
        cli_path=cli_path,
        sdk_importable=sdk_importable,
    )

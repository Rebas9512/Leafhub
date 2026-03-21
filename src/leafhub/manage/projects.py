"""
Admin API — Project management.

Endpoints:
  GET    /admin/projects
  POST   /admin/projects
  GET    /admin/projects/{id}
  PUT    /admin/projects/{id}
  DELETE /admin/projects/{id}
  POST   /admin/projects/{id}/rotate-token
  POST   /admin/projects/{id}/deactivate
  POST   /admin/projects/{id}/activate
  POST   /admin/projects/{id}/link        — link to a local directory, write .leafhub

Ref: ModelHub/admin/projects.py
     (max_concurrent, rpm_limit, token_expires_at removed — Leafhub has no scheduler)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


# ── Path helpers ───────────────────────────────────────────────────────────────

def _clean_path(raw: str) -> str:
    """Strip wrapping single or double quotes from a path string.

    Users often copy paths from a terminal (e.g. drag-and-drop on macOS/Linux
    produces ``'/path/with spaces'``).  Strip one matching pair of quotes so
    ``Path(...)`` resolves correctly.
    """
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


# ── Dotfile helpers ────────────────────────────────────────────────────────────

_DOTFILE_NAME = ".leafhub"
_GITIGNORE_ENTRY = ".leafhub\n"


def _write_dotfile(project_dir: Path, project_name: str, raw_token: str) -> Path:
    """
    Write a ``.leafhub`` dotfile into *project_dir*.

    The file is readable only by its owner (chmod 600) and contains the project
    token so that ``leafhub.probe.detect()`` / ``LeafHub.from_directory()`` can
    auto-configure any compatible project without manual setup.

    Also appends ``.leafhub`` to ``.gitignore`` in the same directory if one
    exists (idempotent).
    """
    from datetime import datetime, timezone

    payload = {
        "version":   1,
        "project":   project_name,
        "token":     raw_token,
        "linked_at": datetime.now(timezone.utc).isoformat(),
    }
    content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    dotfile = project_dir / _DOTFILE_NAME

    # Atomic write: write to a temp file in the same directory, set permissions
    # while the fd is still open (before the file is reachable at its final path),
    # then rename into place.  This ensures the token is never world-readable,
    # even transiently, and that a partial write never corrupts an existing dotfile.
    fd, tmp_path = tempfile.mkstemp(dir=project_dir, prefix=".leafhub-")
    try:
        os.write(fd, content)
        # fchmod sets permissions before the file is reachable at its final path
        # so the token is never transiently world-readable.
        # os.fchmod is Unix/macOS only; on Windows we fall back to os.chmod
        # after the rename (chmod is available but doesn't enforce permissions).
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, dotfile)  # atomic on POSIX; best-effort on Windows
        if not hasattr(os, "fchmod"):
            os.chmod(dotfile, 0o600)   # no-op on Windows, but documents intent
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Keep .leafhub out of git history (non-sensitive, non-atomic is acceptable).
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        gi_content = gitignore.read_text(encoding="utf-8")
        if _DOTFILE_NAME not in gi_content.splitlines():
            gitignore.write_text(
                gi_content.rstrip("\n") + "\n" + _GITIGNORE_ENTRY,
                encoding="utf-8",
            )
    return dotfile


# ── Integration file distribution ────────────────────────────────────────────

_PROBE_COPY_NAME   = "leafhub_probe.py"
_REGISTER_SH_NAME  = "register.sh"

# The canonical probe.py lives inside the leafhub package.
_PROBE_SOURCE = Path(__file__).resolve().parents[1] / "probe.py"


def _is_integrated(project_dir: Path) -> bool:
    """Return True if *project_dir* already contains ``register.sh``.

    Presence of ``register.sh`` indicates the project has previously been
    integrated with LeafHub (its ``setup.sh`` sources the file).  In that
    case we only refresh ``.leafhub`` — the integration files are already
    in place and may have been customised.
    """
    return (project_dir / _REGISTER_SH_NAME).exists()


def _copy_probe_to_project(project_dir: Path) -> None:
    """Copy ``leafhub/probe.py`` into *project_dir* as ``leafhub_probe.py``."""
    dest = project_dir / _PROBE_COPY_NAME
    try:
        shutil.copy2(_PROBE_SOURCE, dest)
    except Exception:
        pass   # non-fatal


def _copy_register_sh_to_project(project_dir: Path) -> None:
    """Copy ``register.sh`` (from package data) into *project_dir*.

    Tries importlib.resources first (installed package / editable install),
    then falls back to the repository root (development checkout only).
    Failures are silently ignored — the dotfile is the critical artefact.
    """
    import importlib.resources as _pkg_res

    dest = project_dir / _REGISTER_SH_NAME
    try:
        content = (
            _pkg_res.files("leafhub")
            .joinpath("register.sh")
            .read_text(encoding="utf-8")
        )
        dest.write_text(content, encoding="utf-8")
        return
    except Exception:
        pass

    # Fallback: development checkout layout
    # Path: src/leafhub/manage/projects.py → parents[3] = repo root
    src = Path(__file__).resolve().parents[3] / "register.sh"
    try:
        shutil.copy2(src, dest)
    except Exception:
        pass


def _distribute_integration_files(project_dir: Path) -> list[str]:
    """Copy ``register.sh`` and ``leafhub_probe.py`` to a new project directory.

    Called only when ``_is_integrated(project_dir)`` returns False — i.e. this
    is the first time LeafHub is being set up in this directory.

    Returns the list of filenames that were written (for use in response bodies
    and CLI output).
    """
    _copy_probe_to_project(project_dir)
    _copy_register_sh_to_project(project_dir)
    # Report only files that are actually on disk after the operation.
    distributed = []
    for name in (_REGISTER_SH_NAME, _PROBE_COPY_NAME):
        if (project_dir / name).exists():
            distributed.append(name)
    return distributed


# ── Dotfile removal ───────────────────────────────────────────────────────────

_FILES_TO_REMOVE = (_DOTFILE_NAME, _PROBE_COPY_NAME, _REGISTER_SH_NAME)


def _remove_project_files(project_dir: Path) -> list[str]:
    """
    Remove LeafHub-managed files from *project_dir* when a project is deleted.

    Removes:
    - ``.leafhub``       — project token (the critical one)
    - ``leafhub_probe.py`` — convenience probe copy

    Returns a list of filenames that were actually removed.
    Silently skips files that do not exist and never raises.
    """
    removed: list[str] = []
    for name in _FILES_TO_REMOVE:
        target = project_dir / name
        try:
            target.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("Could not remove %s", target)
    return removed


# ── CLI detection & registration ──────────────────────────────────────────────

#: venv executables that belong to Python/pip itself, not to the project.
_VENV_STDLIB_PREFIXES = ("python", "pip", "_", ".")
_VENV_STDLIB_NAMES = frozenset({
    "activate", "activate.csh", "activate.fish",
    "easy_install", "wheel", "pydoc", "pydoc3",
    "normalizer", "chardetect", "f2py",
})


def _detect_project_cli(project_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(name, venv_path)`` for project CLI executables not yet in ``~/.local/bin``.

    Scans ``<project_dir>/.venv/bin/`` for executable files that are not
    standard Python-venv tools, then filters out any that are already
    symlinked (and point into this project) in ``~/.local/bin/``.

    Returns an empty list on non-POSIX platforms or when no venv is present.
    """
    if os.name != "posix":
        return []
    venv_bin = project_dir / ".venv" / "bin"
    if not venv_bin.is_dir():
        return []

    resolved_project = project_dir.resolve()
    local_bin = Path.home() / ".local" / "bin"

    result: list[tuple[str, Path]] = []
    for entry in sorted(venv_bin.iterdir()):
        if any(entry.name.startswith(p) for p in _VENV_STDLIB_PREFIXES):
            continue
        if entry.name in _VENV_STDLIB_NAMES:
            continue
        if not entry.is_file() or not os.access(entry, os.X_OK):
            continue
        # Skip if already symlinked from ~/.local/bin into this project.
        link = local_bin / entry.name
        if link.is_symlink():
            try:
                if link.resolve().is_relative_to(resolved_project):
                    continue
            except ValueError:
                pass
        result.append((entry.name, entry))
    return result


def _register_cli_symlinks(project_dir: Path) -> list[str]:
    """Create ``~/.local/bin`` symlinks for unregistered project CLI tools.

    Calls :func:`_detect_project_cli` and creates one symlink per detected
    tool.  Existing symlinks that point elsewhere are replaced.

    Returns the list of CLI names that were successfully registered.
    Non-fatal errors are logged as warnings.
    """
    unregistered = _detect_project_cli(project_dir)
    if not unregistered:
        return []

    local_bin = Path.home() / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)

    registered: list[str] = []
    for name, target in unregistered:
        link = local_bin / name
        try:
            if link.is_symlink():
                link.unlink()   # replace stale / wrong symlink
            link.symlink_to(target)
            registered.append(name)
        except OSError as exc:
            log.warning("Could not create CLI symlink %s → %s: %s", link, target, exc)
    return registered


# ── Installer registration cleanup ────────────────────────────────────────────

#: Shell RC files that project installers typically add PATH entries to.
_RC_FILES = (".zprofile", ".zshrc", ".bashrc", ".bash_profile", ".profile")


def _cleanup_installer_registration(project_dir: Path) -> list[str]:
    """
    Remove system-level registrations that the project's installer created.

    This is called automatically on project delete so the host machine is left
    in a clean state — only the project's source files remain.

    Unix / macOS:
      1. Symlinks in ``~/.local/bin/`` whose resolved target lives inside
         *project_dir* (e.g. the ``trileaf`` command symlink).
      2. Lines in shell RC files that reference ``<project_dir>/.venv/bin``
         (the PATH export added by the installer).

    Windows:
      1. ``<project_dir>\\.venv\\Scripts`` entry removed from the User PATH
         environment variable (HKCU\\Environment).

    Returns a list of human-readable strings describing each item removed.
    Silently skips entries it cannot remove and never raises.
    """
    removed: list[str] = []

    if os.name == "posix":
        # ── 1. CLI symlinks ────────────────────────────────────────────────────
        # Resolve project_dir once so comparison works even when project_dir
        # itself contains symlink components (e.g. /home/user/app → /mnt/data/app).
        resolved_project_dir = project_dir.resolve()
        local_bin = Path.home() / ".local" / "bin"
        if local_bin.is_dir():
            for entry in local_bin.iterdir():
                try:
                    if entry.is_symlink():
                        resolved = entry.resolve()
                        if resolved.is_relative_to(resolved_project_dir):
                            entry.unlink()
                            removed.append(f"~/.local/bin/{entry.name} (CLI symlink)")
                except (OSError, ValueError):
                    pass

        # ── 2. Shell RC PATH lines ─────────────────────────────────────────────
        # Match lines that export or prepend <project_dir>/.venv/bin to PATH.
        # We match the directory string rather than the project name so we never
        # accidentally strip unrelated entries in files we don't fully own.
        venv_bin = str(project_dir / ".venv" / "bin")
        for rc_name in _RC_FILES:
            rc = Path.home() / rc_name
            if not rc.exists():
                continue
            try:
                original = rc.read_text(encoding="utf-8", errors="replace")
                lines = original.splitlines(keepends=True)
                filtered = [ln for ln in lines if venv_bin not in ln]
                if len(filtered) < len(lines):
                    rc.write_text("".join(filtered), encoding="utf-8")
                    removed.append(f"~/{rc_name} (PATH entry)")
            except OSError as exc:
                log.warning("Could not clean %s: %s", rc, exc)

    elif os.name == "nt":
        # ── Windows User PATH ──────────────────────────────────────────────────
        venv_scripts = str(project_dir / ".venv" / "Scripts").rstrip("\\")
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Environment",
                0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            )
            try:
                current_path, reg_type = winreg.QueryValueEx(key, "Path")
                entries = [e for e in current_path.split(";") if e.strip()]
                filtered = [
                    e for e in entries
                    if e.strip().rstrip("\\").lower() != venv_scripts.lower()
                ]
                if len(filtered) < len(entries):
                    winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(filtered))
                    removed.append(r".venv\Scripts (User PATH)")
            finally:
                winreg.CloseKey(key)
        except (ImportError, OSError, PermissionError) as exc:
            log.warning("Could not clean Windows User PATH: %s", exc)

    return removed


# ── Schemas ───────────────────────────────────────────────────────────────────

class BindingSchema(BaseModel):
    alias:          str
    provider_id:    str
    model_override: str | None = None


class ProjectCreateRequest(BaseModel):
    name:     str
    bindings: list[BindingSchema] = Field(default_factory=list)
    path:     str | None = None   # optional: link to a local directory immediately


class ProjectUpdateRequest(BaseModel):
    name:     str | None = None
    bindings: list[BindingSchema] | None = None
    path:     str | None = None


class LinkRequest(BaseModel):
    path: str    # absolute path to the project directory


def _store(request: Request):
    return request.app.state.store


def _project_dict(p) -> dict:
    return {
        "id":           p.id,
        "name":         p.name,
        "token_prefix": p.token_prefix,
        "is_active":    p.is_active,
        "created_at":   p.created_at,
        "path":         p.path,
        "bindings": [
            {
                "id":             b.id,
                "alias":          b.alias,
                "provider_id":    b.provider_id,
                "model_override": b.model_override,
            }
            for b in p.bindings
        ],
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects(request: Request):
    store = _store(request)
    projects = await asyncio.to_thread(store.list_projects)
    return {"data": [_project_dict(p) for p in projects]}


@router.post("/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreateRequest):
    store = _store(request)

    # Validate path before touching the DB — avoids creating a project that
    # the user thinks is linked but actually has no dotfile.
    link_dir: Path | None = None
    if body.path:
        link_dir = Path(_clean_path(body.path)).resolve()
        if not link_dir.is_dir():
            raise HTTPException(400, f"Link directory not found: {body.path}")

    def _create():
        project, raw_token = store.create_project(body.name)
        if body.bindings:
            store.set_bindings(
                project.id,
                [{"alias": b.alias,
                  "provider_id": b.provider_id,
                  "model_override": b.model_override}
                 for b in body.bindings],
            )
        distributed: list[str] = []
        cli_registered: list[str] = []
        if link_dir is not None:
            # Write dotfile first; only update DB path if write succeeds.
            _write_dotfile(link_dir, body.name, raw_token)
            if not _is_integrated(link_dir):
                distributed = _distribute_integration_files(link_dir)
            cli_registered = _register_cli_symlinks(link_dir)
            store.set_project_path(project.id, str(link_dir))
        project = store.get_project(project.id)
        return project, raw_token, distributed, cli_registered

    project, raw_token, distributed, cli_registered = await asyncio.to_thread(_create)
    result = _project_dict(project)
    if link_dir is None:
        result["token"] = raw_token   # shown ONCE
    if distributed:
        result["files_distributed"] = distributed
    if cli_registered:
        result["cli_registered"] = cli_registered
    return result


@router.get("/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    store = _store(request)
    try:
        p = await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return _project_dict(p)


@router.put("/projects/{project_id}")
async def update_project(request: Request, project_id: str,
                          body: ProjectUpdateRequest):
    store = _store(request)

    try:
        await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")

    def _update():
        if body.name is not None:
            store.rename_project(project_id, body.name)
        if body.bindings is not None:
            store.set_bindings(
                project_id,
                [{"alias": b.alias,
                  "provider_id": b.provider_id,
                  "model_override": b.model_override}
                 for b in body.bindings],
            )
        return store.get_project(project_id)

    p = await asyncio.to_thread(_update)
    return _project_dict(p)


@router.delete("/projects/{project_id}")
async def delete_project(request: Request, project_id: str):
    store = _store(request)
    try:
        p = await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")

    def _delete() -> dict:
        files_removed: list[str] = []
        reg_removed: list[str] = []
        if p.path:
            project_dir = Path(p.path)
            files_removed = _remove_project_files(project_dir)
            reg_removed   = _cleanup_installer_registration(project_dir)
        store.delete_project(project_id)
        return {"files": files_removed, "registration": reg_removed}

    result = await asyncio.to_thread(_delete)
    return {
        "deleted": True,
        "files_removed": result["files"],
        "registration_removed": result["registration"],
    }


@router.post("/projects/{project_id}/rotate-token")
async def rotate_token(request: Request, project_id: str):
    store = _store(request)
    try:
        await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")

    def _rotate():
        raw_token = store.rotate_token(project_id)
        # If the project has a linked directory, keep the dotfile in sync.
        p = store.get_project(project_id)
        dotfile_updated = False
        if p.path:
            project_dir = Path(p.path)
            if project_dir.is_dir():
                _write_dotfile(project_dir, p.name, raw_token)
                dotfile_updated = True
        return raw_token, dotfile_updated

    new_token, dotfile_updated = await asyncio.to_thread(_rotate)
    resp = {"token": new_token, "message": "Token rotated. Store the new token securely."}
    if dotfile_updated:
        resp["dotfile_updated"] = True
        resp["message"] = "Token rotated and .leafhub file updated automatically."
    return resp


@router.post("/projects/{project_id}/link")
async def link_project(request: Request, project_id: str, body: LinkRequest):
    """
    Link a project to a local directory.

    Rotates the project token, writes a ``.leafhub`` dotfile into the target
    directory (chmod 600), and stores the path in the database.  Any existing
    token is invalidated — apps using the old token must restart to pick up
    the new dotfile.

    The raw token is written only to the dotfile — it is NOT returned in the
    response body.  The linked project will auto-detect it via
    ``leafhub.probe.detect()`` or ``LeafHub.from_directory()``.
    """
    store = _store(request)
    try:
        await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")

    project_dir = Path(_clean_path(body.path)).resolve()
    if not project_dir.is_dir():
        raise HTTPException(400, f"Directory not found: {body.path}")

    def _link():
        # Rotate produces a fresh raw token we can write immediately.
        raw_token = store.rotate_token(project_id)
        store.set_project_path(project_id, str(project_dir))
        p = store.get_project(project_id)
        dotfile = _write_dotfile(project_dir, p.name, raw_token)
        distributed: list[str] = []
        if not _is_integrated(project_dir):
            distributed = _distribute_integration_files(project_dir)
        cli_registered = _register_cli_symlinks(project_dir)
        return p, str(dotfile), distributed, cli_registered

    project, dotfile_path, distributed, cli_registered = await asyncio.to_thread(_link)
    resp: dict = {
        "linked":  True,
        "path":    str(project_dir),
        "dotfile": dotfile_path,
        "project": _project_dict(project),
    }
    if distributed:
        resp["files_distributed"] = distributed
    if cli_registered:
        resp["cli_registered"] = cli_registered
    resp["message"] = (
        f"Project '{project.name}' linked to {project_dir}. "
        "The .leafhub file has been written — apps in that directory will "
        "auto-detect LeafHub on next startup."
        + (f" Integration files written: {', '.join(distributed)}." if distributed else "")
        + (f" CLI registered: {', '.join(cli_registered)}." if cli_registered else "")
    )
    return resp


@router.post("/projects/{project_id}/deactivate", status_code=204)
async def deactivate_project(request: Request, project_id: str):
    store = _store(request)
    try:
        await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")
    await asyncio.to_thread(store.deactivate_project, project_id)


@router.post("/projects/{project_id}/activate", status_code=204)
async def activate_project(request: Request, project_id: str):
    store = _store(request)
    try:
        await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")
    await asyncio.to_thread(store.activate_project, project_id)

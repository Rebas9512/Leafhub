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


# ── Probe file distributor ────────────────────────────────────────────────────

_PROBE_COPY_NAME = "leafhub_probe.py"
# The canonical probe.py lives inside the leafhub package.
_PROBE_SOURCE = Path(__file__).resolve().parents[1] / "probe.py"


def _copy_probe_to_project(project_dir: Path) -> None:
    """
    Copy ``leafhub/probe.py`` into *project_dir* as ``leafhub_probe.py``.

    The copy is a standalone, self-contained Python file that developers can
    read and adapt without referring back to the LeafHub source.  It is
    overwritten on every link so it stays in sync with the installed version.

    Failures are silently ignored — the dotfile is the critical artefact; the
    probe copy is a convenience.
    """
    dest = project_dir / _PROBE_COPY_NAME
    try:
        shutil.copy2(_PROBE_SOURCE, dest)
    except Exception:
        pass   # probe copy is optional; never fail the link for this


# ── Dotfile removal ───────────────────────────────────────────────────────────

_FILES_TO_REMOVE = (_DOTFILE_NAME, _PROBE_COPY_NAME)


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


# ── Schemas ───────────────────────────────────────────────────────────────────

class BindingSchema(BaseModel):
    alias:          str
    provider_id:    str
    model_override: str | None = None


class ProjectCreateRequest(BaseModel):
    name:       str
    bindings:   list[BindingSchema] = Field(default_factory=list)
    path:       str | None = None   # optional: link to a local directory immediately
    copy_probe: bool = True         # copy leafhub_probe.py to the project root


class ProjectUpdateRequest(BaseModel):
    name:     str | None = None
    bindings: list[BindingSchema] | None = None
    path:     str | None = None


class LinkRequest(BaseModel):
    path:       str    # absolute path to the project directory
    copy_probe: bool = True  # copy leafhub_probe.py to the project root


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
        if link_dir is not None:
            # Write dotfile first; only update DB path if write succeeds.
            # This keeps the DB consistent with the filesystem.
            _write_dotfile(link_dir, body.name, raw_token)
            if body.copy_probe:
                _copy_probe_to_project(link_dir)
            store.set_project_path(project.id, str(link_dir))
        project = store.get_project(project.id)
        return project, raw_token

    project, raw_token = await asyncio.to_thread(_create)
    result = _project_dict(project)
    # Only include the raw token in the response when there is no linked
    # directory — in that case it was written to .leafhub and never needs
    # to be shown.
    if link_dir is None:
        result["token"] = raw_token   # shown ONCE
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


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(request: Request, project_id: str):
    store = _store(request)
    try:
        p = await asyncio.to_thread(store.get_project, project_id)
    except KeyError:
        raise HTTPException(404, f"Project '{project_id}' not found")

    def _delete():
        if p.path:
            _remove_project_files(Path(p.path))
        store.delete_project(project_id)

    await asyncio.to_thread(_delete)


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
        if body.copy_probe:
            _copy_probe_to_project(project_dir)
        return p, str(dotfile)

    project, dotfile_path = await asyncio.to_thread(_link)
    resp: dict = {
        "linked":  True,
        "path":    str(project_dir),
        "dotfile": dotfile_path,
        "project": _project_dict(project),
    }
    if body.copy_probe:
        resp["probe_copy"] = str(project_dir / _PROBE_COPY_NAME)
    resp["message"] = (
        f"Project '{project.name}' linked to {project_dir}. "
        "The .leafhub file has been written — apps in that directory will "
        "auto-detect LeafHub on next startup."
        + (f" A standalone probe copy was written to {_PROBE_COPY_NAME}."
           if body.copy_probe else "")
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

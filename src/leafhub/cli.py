"""
LeafHub CLI — direct management of ~/.leafhub/ without a running server.

Usage:
    leafhub provider add   --name <name> --key <key> [--type <type>]
                           [--base-url <url>] [--format <fmt>]
                           [--default-model <model>] [--models <m1,m2,...>]
    leafhub provider list
    leafhub provider show  --name <name>
    leafhub provider delete --name <name>

    leafhub project create  <name> [--path <dir>] [--no-probe]
    leafhub project link    <name> --path <dir>   [--no-probe]
    leafhub project list
    leafhub project show    <name>
    leafhub project token   <name>
    leafhub project bind    <name> --alias <alias> --provider <provider>
                                   [--model <model>]
    leafhub project unbind  <name> --alias <alias>
    leafhub project delete  <name>

    leafhub status
    leafhub manage [--port 8765]
    leafhub clean
    leafhub uninstall
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_store(hub_dir: Path | None = None):
    """Open DB and return (SyncStore, resolved_hub_dir)."""
    from .core import default_hub_dir
    from .core.db import open_db
    from .core.store import SyncStore
    resolved = hub_dir if hub_dir is not None else default_hub_dir()
    conn = open_db(resolved)
    return SyncStore(conn), resolved


def _print_token_box(raw_token: str, label: str = "Project Token") -> None:
    """Display token prominently — shown only once."""
    line = "─" * 60
    print(f"\n┌{line}┐")
    print(f"│  {label:<56}  │")
    print(f"│                                                            │")
    print(f"│  {raw_token:<56}  │")
    print(f"│                                                            │")
    print(f"│  ⚠  Save this token — it will NOT be shown again.        │")
    print(f"└{line}┘\n")


def _die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _strip_path_quotes(path: str) -> str:
    """Strip a single wrapping pair of quotes from a path string.

    Terminal drag-and-drop (and copy-paste on some platforms) wraps paths
    containing spaces in single quotes, e.g. ``'/my path/project'``.
    """
    s = path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


def _require_arg(value, name: str):
    if value is None:
        _die(f"--{name} is required")
    return value


# ── Provider commands ─────────────────────────────────────────────────────────

def cmd_provider_add(args: argparse.Namespace) -> None:
    import getpass
    name   = _require_arg(args.name,  "name")
    fmt    = args.format or "openai-completions"

    if args.key:
        key = args.key
    else:
        key = getpass.getpass("API key: ").strip()
        if not key:
            _die("API key is required")
    ptype  = args.type   or "custom"
    base_url = args.base_url or _default_base_url(fmt)
    model    = args.default_model or _default_model(fmt)
    models   = [m.strip() for m in args.models.split(",")] if args.models else []

    # Parse extra headers: --extra-header "Name: Value" (repeatable)
    extra_headers: dict[str, str] = {}
    for h in (args.extra_header or []):
        if ":" not in h:
            _die(f"Invalid --extra-header '{h}'. Expected format: 'Header-Name: value'")
        hname, _, hval = h.partition(":")
        extra_headers[hname.strip()] = hval.strip()

    store, hub_dir = _open_store()

    # Reject duplicate label
    if store.find_provider_by_label(name):
        store.close()
        _die(f"Provider '{name}' already exists. Use a different name.")

    provider = store.create_provider(
        label=name,
        provider_type=ptype,
        api_format=fmt,
        base_url=base_url,
        default_model=model,
        available_models=models,
        auth_mode=args.auth_mode or None,     # None → inferred in store
        auth_header=args.auth_header or None,
        extra_headers=extra_headers,
    )

    # Persist API key to providers.enc.  If this fails, roll back the DB row
    # so the store never has a provider record without a key in providers.enc.
    from .core.crypto import load_master_key, encrypt_providers, decrypt_providers
    try:
        master_key  = load_master_key(hub_dir)
        key_store   = decrypt_providers(master_key, hub_dir)
        key_store[provider.id] = {"api_key": key}
        encrypt_providers(key_store, master_key, hub_dir)
    except Exception as exc:
        store.delete_provider(provider.id)
        store.close()
        _die(f"Failed to save API key — provider creation rolled back: {exc}")

    store.close()
    print(f"✓ Provider '{name}' added (id: {provider.id[:8]}…)")


def cmd_provider_list(args: argparse.Namespace) -> None:
    store, _ = _open_store()
    providers = store.list_providers()
    store.close()

    if getattr(args, "json", False):
        import json
        print(json.dumps([
            {"label": p.label, "api_format": p.api_format,
             "default_model": p.default_model, "id": p.id}
            for p in providers
        ]))
        return

    if not providers:
        print("No providers configured.")
        return

    print(f"\n{'Label':<24} {'Format':<22} {'Default Model':<20} {'ID'}")
    print("─" * 90)
    for p in providers:
        print(f"  {p.label:<22} {p.api_format:<22} {p.default_model:<20} {p.id[:8]}…")
    print()


def cmd_provider_show(args: argparse.Namespace) -> None:
    name  = _require_arg(args.name, "name")
    store, _ = _open_store()
    p = store.find_provider_by_label(name)
    store.close()

    if p is None:
        _die(f"Provider '{name}' not found.")

    print(f"\nProvider: {p.label}")
    print(f"  ID            : {p.id}")
    print(f"  Type          : {p.provider_type}")
    print(f"  API Format    : {p.api_format}")
    print(f"  Base URL      : {p.base_url}")
    print(f"  Default Model : {p.default_model}")
    if p.available_models:
        print(f"  Models        : {', '.join(p.available_models)}")
    print(f"  Auth Mode     : {p.auth_mode}")
    if p.auth_header:
        print(f"  Auth Header   : {p.auth_header}")
    if p.extra_headers:
        for k, v in p.extra_headers.items():
            print(f"  Extra Header  : {k}: {v}")
    print(f"  Created       : {p.created_at}")
    print(f"  API Key       : (encrypted — use 'leafhub provider add' to replace)\n")


def cmd_provider_delete(args: argparse.Namespace) -> None:
    name  = _require_arg(args.name, "name")
    store, hub_dir = _open_store()
    p = store.find_provider_by_label(name)

    if p is None:
        store.close()
        _die(f"Provider '{name}' not found.")

    # Confirm
    answer = input(f"Delete provider '{name}' ({p.id[:8]}…)? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        store.close()
        print("Aborted.")
        return

    # Remove from DB (FK prevents deletion if bindings exist)
    try:
        store.delete_provider(p.id)
    except sqlite3.IntegrityError:
        store.close()
        _die(
            f"Cannot delete provider '{name}' — it is still bound to one or more projects.\n"
            "  Hint: unbind this provider from all projects first."
        )

    # Remove API key from providers.enc
    from .core.crypto import load_master_key, encrypt_providers, decrypt_providers
    master_key = load_master_key(hub_dir)
    key_store  = decrypt_providers(master_key, hub_dir)
    key_store.pop(p.id, None)
    encrypt_providers(key_store, master_key, hub_dir)

    store.close()
    print(f"✓ Provider '{name}' deleted.")


# ── Project commands ───────────────────────────────────────────────────────────

def cmd_project_create(args: argparse.Namespace) -> None:
    from .manage.projects import (
        _distribute_integration_files,
        _is_integrated, _register_cli_symlinks, _write_dotfile,
    )

    name          = args.project_name
    raw_path      = getattr(args, "path", None)
    link_path     = Path(_strip_path_quotes(raw_path)).resolve() if raw_path else None
    skip_wizard   = getattr(args, "yes", False)
    if_not_exists = getattr(args, "if_not_exists", False)

    if link_path is not None and not link_path.is_dir():
        _die(f"Link directory not found: {args.path}")

    store, hub_dir = _open_store()
    try:
        # --if-not-exists: re-link silently if project already registered
        if if_not_exists:
            existing = store.find_project_by_name(name)
            if existing is not None:
                if link_path is not None:
                    raw_token = store.rotate_token(existing.id)
                    store.set_project_path(existing.id, str(link_path))
                    _write_dotfile(link_path, name, raw_token)
                    if not _is_integrated(link_path):
                        _distribute_integration_files(link_path)
                    cli_registered = _register_cli_symlinks(link_path)
                    print(f"✓ Project '{name}' already exists — re-linked to {link_path}.")
                    if cli_registered:
                        print(f"✓ CLI registered: {', '.join(cli_registered)}")
                else:
                    print(f"✓ Project '{name}' already exists — no path change.")
                return

        project, raw_token = store.create_project(name)

        if link_path is not None:
            _write_dotfile(link_path, name, raw_token)
            distributed = (
                _distribute_integration_files(link_path)
                if not _is_integrated(link_path) else []
            )
            cli_registered = _register_cli_symlinks(link_path)
            store.set_project_path(project.id, str(link_path))
            print(f"✓ Project '{name}' created and linked to {link_path}.")
            print(f"  .leafhub written — project auto-detects credentials on startup.")
            if distributed:
                print(f"  Integration files written: {', '.join(distributed)}.")
            if cli_registered:
                print(f"✓ CLI registered: {', '.join(cli_registered)}")
        else:
            print(f"✓ Project '{name}' created.")
            _print_token_box(raw_token)
            print("  Add to your project .env:")
            print(f"    LEAFHUB_TOKEN={raw_token}\n")

        if not skip_wizard:
            _interactive_bind_wizard(store, hub_dir, project.id, name)
    finally:
        store.close()


def cmd_project_link(args: argparse.Namespace) -> None:
    from .manage.projects import (
        _distribute_integration_files,
        _is_integrated, _register_cli_symlinks, _write_dotfile,
    )

    name      = args.project_name
    link_path = Path(_strip_path_quotes(args.path)).resolve()

    if not link_path.is_dir():
        _die(f"Directory not found: {args.path}")

    store, hub_dir = _open_store()
    try:
        p = store.find_project_by_name(name)
        if p is None:
            _die(f"Project '{name}' not found.")

        # Rotate token so the new dotfile is the only valid credential.
        raw_token = store.rotate_token(p.id)
        store.set_project_path(p.id, str(link_path))

        _write_dotfile(link_path, name, raw_token)
        distributed = (
            _distribute_integration_files(link_path)
            if not _is_integrated(link_path) else []
        )
        cli_registered = _register_cli_symlinks(link_path)

        print(f"✓ Project '{name}' linked to {link_path}.")
        print(f"  .leafhub written — token rotated, old token invalidated.")
        if distributed:
            print(f"  Integration files written: {', '.join(distributed)}.")
        if cli_registered:
            print(f"✓ CLI registered: {', '.join(cli_registered)}")

        _interactive_bind_wizard(store, hub_dir, p.id, name)
    finally:
        store.close()


def cmd_project_list(args: argparse.Namespace) -> None:
    store, _ = _open_store()
    projects = store.list_projects()
    store.close()

    if not projects:
        print("No projects configured.")
        return

    print(f"\n{'Name':<24} {'Active':<8} {'Token Prefix':<16} {'Bindings':<10} {'ID'}")
    print("─" * 82)
    for p in projects:
        active = "yes" if p.is_active else "no"
        print(f"  {p.name:<22} {active:<8} {p.token_prefix:<16} {len(p.bindings):<10} {p.id[:8]}…")
    print()


def cmd_project_show(args: argparse.Namespace) -> None:
    name  = args.project_name
    store, _ = _open_store()
    p = store.find_project_by_name(name)
    store.close()

    if p is None:
        _die(f"Project '{name}' not found.")

    status = "active" if p.is_active else "inactive"
    print(f"\nProject: {p.name}")
    print(f"  ID           : {p.id}")
    print(f"  Status       : {status}")
    print(f"  Token Prefix : {p.token_prefix}…")
    print(f"  Created      : {p.created_at}")
    if p.bindings:
        print(f"  Bindings     :")
        for b in p.bindings:
            model_note = f"  (model: {b.model_override})" if b.model_override else ""
            print(f"    {b.alias:<20} → provider {b.provider_id[:8]}…{model_note}")
    else:
        print(f"  Bindings     : (none)")
    print()


def cmd_project_token(args: argparse.Namespace) -> None:
    name  = args.project_name
    store, _ = _open_store()
    p = store.find_project_by_name(name)

    if p is None:
        store.close()
        _die(f"Project '{name}' not found.")

    answer = input(
        f"Rotate token for '{name}'? The old token will stop working immediately. [y/N] "
    ).strip().lower()
    if answer not in ("y", "yes"):
        store.close()
        print("Aborted.")
        return

    raw_token = store.rotate_token(p.id)
    store.close()

    print(f"✓ Token rotated for '{name}'.")
    _print_token_box(raw_token, label=f"New Token for '{name}'")
    print("  Update your project .env:")
    print(f"    LEAFHUB_TOKEN={raw_token}\n")


def cmd_project_bind(args: argparse.Namespace) -> None:
    name          = args.project_name
    alias         = _require_arg(args.alias, "alias")
    provider_name = _require_arg(args.provider, "provider")

    store, _ = _open_store()
    p = store.find_project_by_name(name)
    if p is None:
        store.close()
        _die(f"Project '{name}' not found.")

    prov = store.find_provider_by_label(provider_name)
    if prov is None:
        store.close()
        _die(f"Provider '{provider_name}' not found.")

    binding = store.add_binding(
        project_id=p.id,
        alias=alias,
        provider_id=prov.id,
        model_override=args.model,
    )
    store.close()

    model_note = f" (model: {args.model})" if args.model else f" (model: {prov.default_model})"
    print(f"✓ Bound alias '{alias}' → '{provider_name}'{model_note} in project '{name}'.")


def cmd_project_unbind(args: argparse.Namespace) -> None:
    name  = args.project_name
    alias = _require_arg(args.alias, "alias")

    store, _ = _open_store()
    p = store.find_project_by_name(name)
    if p is None:
        store.close()
        _die(f"Project '{name}' not found.")

    store.remove_binding(p.id, alias)
    store.close()
    print(f"✓ Removed binding '{alias}' from project '{name}'.")


def cmd_project_delete(args: argparse.Namespace) -> None:
    name  = args.project_name
    store, _ = _open_store()
    p = store.find_project_by_name(name)

    if p is None:
        store.close()
        _die(f"Project '{name}' not found.")

    answer = input(
        f"Delete project '{name}' and all its bindings? [y/N] "
    ).strip().lower()
    if answer not in ("y", "yes"):
        store.close()
        print("Aborted.")
        return

    if p.path:
        from leafhub.manage.projects import (
            _remove_project_files,
            _cleanup_installer_registration,
        )
        project_dir = Path(p.path)
        for fname in _remove_project_files(project_dir):
            print(f"  removed {p.path}/{fname}")
        for desc in _cleanup_installer_registration(project_dir):
            print(f"  removed {desc}")

    store.delete_project(p.id)
    store.close()
    print(f"✓ Project '{name}' deleted.")


# ── Provider/binding wizard helpers ────────────────────────────────────────────

def _prompt_new_provider(store, hub_dir: "Path") -> "object | None":
    """
    Interactively collect provider info, persist it, and return the provider object.
    Returns None if the user cancels or an error occurs.
    """
    import getpass

    print("\nNew provider setup:")
    name = input("  Label: ").strip()
    if not name:
        print("  Cancelled.")
        return None
    if store.find_provider_by_label(name):
        print(f"  Provider '{name}' already exists — using it.")
        return store.find_provider_by_label(name)

    formats = ["openai-completions", "anthropic-messages", "ollama"]
    print("  API format:")
    for i, f in enumerate(formats, 1):
        print(f"    [{i}] {f}")
    fmt_raw = input("  Choose [1-3] (default 1): ").strip() or "1"
    try:
        fmt = formats[int(fmt_raw) - 1]
    except (ValueError, IndexError):
        fmt = "openai-completions"

    default_url = _default_base_url(fmt)
    base_url = input(f"  Base URL [{default_url}]: ").strip() or default_url

    default_model_val = _default_model(fmt)
    model = input(f"  Default model [{default_model_val}]: ").strip() or default_model_val

    if fmt == "ollama":
        key = input("  API key (leave blank for none): ").strip()
    else:
        key = getpass.getpass("  API key: ").strip()
        if not key:
            print("  API key is required.")
            return None

    from .core.crypto import load_master_key, encrypt_providers, decrypt_providers

    provider = store.create_provider(
        label=name,
        provider_type="custom",
        api_format=fmt,
        base_url=base_url,
        default_model=model,
        available_models=[],
        auth_mode=None,
        auth_header=None,
        extra_headers={},
    )
    try:
        master_key = load_master_key(hub_dir)
        key_store  = decrypt_providers(master_key, hub_dir)
        key_store[provider.id] = {"api_key": key}
        encrypt_providers(key_store, master_key, hub_dir)
    except Exception as exc:
        store.delete_provider(provider.id)
        print(f"  Failed to save API key: {exc}")
        return None

    print(f"  ✓ Provider '{name}' added.")
    return provider


def _interactive_bind_wizard(
    store,
    hub_dir: "Path",
    project_id: str,
    project_name: str,
) -> None:
    """
    Offer to bind provider aliases to *project_id* interactively.

    Uses *project_id* (not name) for all store operations so the right project
    is targeted even when same-name projects exist.  Silently skips when stdin
    is not a TTY (CI / non-interactive shells).
    """
    import sys
    if not sys.stdin.isatty():
        return

    while True:
        providers = store.list_providers()

        print()
        chosen_provider = None

        if providers:
            print("Bind a provider to this project?")
            print("Available providers:")
            for i, p in enumerate(providers, 1):
                print(f"  [{i}] {p.label}  ({p.api_format})")
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
            chosen_provider = _prompt_new_provider(store, hub_dir)
            if chosen_provider is None:
                return
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(providers):
                    chosen_provider = providers[idx]
                else:
                    print("  Invalid choice — binding skipped.")
                    return
            except ValueError:
                print("  Invalid choice — binding skipped.")
                return

        alias = input(f"  Alias for '{chosen_provider.label}' (e.g. 'chat', 'openai'): ").strip()
        if not alias:
            print("  No alias given — binding skipped.")
            return

        try:
            store.add_binding(
                project_id=project_id,
                alias=alias,
                provider_id=chosen_provider.id,
            )
            print(f"✓ Bound alias '{alias}' → '{chosen_provider.label}' in project '{project_name}'.")
        except Exception as exc:
            print(f"  Binding failed: {exc}")
            print(
                f"  Run: leafhub project bind {project_name} "
                f"--alias {alias} --provider {chosen_provider.label}"
            )
            return

        again = input("  Add another binding? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            return


# ── Status ─────────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    from .core.db import default_hub_dir

    hub_dir = default_hub_dir()
    db_file  = hub_dir / "projects.db"
    enc_file = hub_dir / "providers.enc"

    store, _ = _open_store()
    providers = store.list_providers()
    projects  = store.list_projects()
    store.close()

    if getattr(args, "json", False):
        import json
        bound_projects = sum(1 for p in projects if p.bindings)
        print(json.dumps({
            "providers":      len(providers),
            "projects":       len(projects),
            "bound_projects": bound_projects,
            # ready = at least one provider exists; scripts may additionally
            # check bound_projects > 0 when a binding is required.
            "ready":          len(providers) > 0,
        }))
        return

    active_projects = sum(1 for p in projects if p.is_active)
    total_bindings  = sum(len(p.bindings) for p in projects)

    print(f"\nLeafHub Status")
    print(f"  Storage dir   : {hub_dir}")
    print(f"  Database      : {'✓ exists' if db_file.exists() else '✗ missing'}")
    print(f"  Providers enc : {'✓ exists' if enc_file.exists() else '○ empty (no providers yet)'}")
    print(f"  Providers     : {len(providers)}")
    print(f"  Projects      : {len(projects)} total, {active_projects} active")
    print(f"  Bindings      : {total_bindings}")
    print()


# ── Manage (Phase 3 placeholder) ───────────────────────────────────────────────

def cmd_manage(args: argparse.Namespace) -> None:
    import shutil
    import subprocess
    import threading
    import time
    import webbrowser
    from pathlib import Path

    port       = getattr(args, "port",       8765)  or 8765
    dev        = getattr(args, "dev",        False)
    rebuild    = getattr(args, "rebuild",    False)
    no_browser = getattr(args, "no_browser", False)

    try:
        from .manage.server import run_server
    except ImportError:
        _die(
            "Web UI dependencies not installed.\n"
            "  Run: pip install 'leafhub[manage]'"
        )
        return

    ui_dir  = Path(__file__).parent.parent.parent / "ui"
    ui_dist = ui_dir / "dist"

    def _need_node(action: str) -> None:
        if shutil.which("npm") is None:
            _die(
                f"npm not found — cannot {action}.\n"
                "  Install Node.js: https://nodejs.org/"
            )

    def _npm_install() -> None:
        if not (ui_dir / "node_modules").exists():
            print("  Installing npm dependencies...")
            subprocess.run(["npm", "install"], cwd=ui_dir, check=True)

    def _build_ui() -> None:
        if ui_dist.exists() and not rebuild:
            return
        _need_node("build the Web UI")
        _npm_install()
        action = "Rebuilding" if ui_dist.exists() else "Building"
        print(f"  {action} Vue UI (first run may take a few seconds)...")
        subprocess.run(["npm", "run", "build"], cwd=ui_dir, check=True)
        print("  UI build complete.\n")

    def _open_browser(url: str, delay: float = 1.5) -> None:
        def _open():
            time.sleep(delay)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    def _free_port(p: int) -> None:
        """Kill any process already bound to *p* so we can start cleanly."""
        import socket as _sock
        import sys as _sys
        with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return  # port is free
        print(f"  Port {p} already in use — stopping existing process...")
        killed = False
        if _sys.platform == "win32":
            # netstat to find PID, then taskkill
            import re as _re
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True,
            ).stdout
            for line in out.splitlines():
                if f":{p}" in line and "LISTENING" in line:
                    pid = _re.split(r"\s+", line.strip())[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed = True
                    break
        else:
            # fuser works on Linux; lsof fallback covers macOS
            if shutil.which("fuser"):
                result = subprocess.run(
                    ["fuser", "-k", f"{p}/tcp"],
                    capture_output=True,
                )
                killed = result.returncode == 0
            elif shutil.which("lsof"):
                pids = subprocess.run(
                    ["lsof", "-ti", f"tcp:{p}"],
                    capture_output=True, text=True,
                ).stdout.split()
                for pid in pids:
                    subprocess.run(["kill", pid], capture_output=True)
                killed = bool(pids)
        if killed:
            time.sleep(0.5)  # give the OS a moment to release the socket

    api_url = f"http://127.0.0.1:{port}"

    if dev:
        _need_node("start the Vite dev server")
        _npm_install()
        ui_url = "http://localhost:5173"
        print("Starting LeafHub in development mode")
        print(f"  Vue dev server : {ui_url}  (Vite, hot-reload)")
        print(f"  FastAPI backend: {api_url}")
        print(f"  API docs       : {api_url}/admin/docs")
        print("Press Ctrl+C to stop.\n")
        _free_port(port)
        vite = subprocess.Popen(["npm", "run", "dev"], cwd=ui_dir)
        try:
            if not no_browser:
                _open_browser(ui_url, delay=2.5)
            run_server(port=port)
        except KeyboardInterrupt:
            pass
        finally:
            vite.terminate()
            print("\nServer stopped.")
    else:
        _build_ui()
        _free_port(port)
        print(f"Starting LeafHub manage server  →  {api_url}")
        print(f"  Web UI       : {api_url}")
        print(f"  API docs     : {api_url}/admin/docs")
        print(f"  Health check : {api_url}/health")
        print("Press Ctrl+C to stop.\n")
        if not no_browser:
            _open_browser(api_url)
        try:
            run_server(port=port)
        except KeyboardInterrupt:
            print("\nServer stopped.")


# ── Defaults ───────────────────────────────────────────────────────────────────

def _default_base_url(api_format: str) -> str:
    return {
        "openai-completions":  "https://api.openai.com/v1",
        "anthropic-messages":  "https://api.anthropic.com",
        "ollama":              "http://localhost:11434",
    }.get(api_format, "")


def _default_model(api_format: str) -> str:
    return {
        "openai-completions":  "gpt-4o",
        "anthropic-messages":  "claude-3-5-sonnet-20241022",
        "ollama":              "llama3",
    }.get(api_format, "")


# ── Double-confirmation helper ─────────────────────────────────────────────────

def _confirm_twice(prompt1: str, prompt2: str) -> bool:
    """Return True only after the user answers yes to two separate prompts."""
    try:
        a1 = input(prompt1).strip().lower()
    except EOFError:
        print("\nAborted.")
        return False
    if a1 not in ("y", "yes"):
        print("Aborted.")
        return False
    try:
        a2 = input(prompt2).strip().lower()
    except EOFError:
        print("\nAborted.")
        return False
    if a2 not in ("y", "yes"):
        print("Aborted.")
        return False
    return True


# ── Clean command ──────────────────────────────────────────────────────────────

def cmd_clean(args: argparse.Namespace) -> None:
    """Remove all providers and projects, including artifacts and CLI registrations."""
    from .manage.projects import (
        _cleanup_installer_registration,
        _remove_project_files,
    )

    store, hub_dir = _open_store()
    try:
        providers = store.list_providers()
        projects  = store.list_projects()
    finally:
        store.close()

    if not providers and not projects:
        print("Nothing to clean — no providers or projects configured.")
        return

    # Show what will be removed
    print("\nThis will permanently remove:")
    if providers:
        print(f"  {len(providers)} provider(s) : {', '.join(p.label for p in providers)}")
    if projects:
        print(f"  {len(projects)} project(s)  : {', '.join(p.name for p in projects)}")
    linked = [p for p in projects if p.path]
    if linked:
        dirs_word = "directory" if len(linked) == 1 else "directories"
        print(
            f"  Project artefacts (.leafhub, leafhub_dist/)"
            f" from {len(linked)} linked {dirs_word}"
        )
        print("  CLI registrations (symlinks + shell PATH entries) for those projects")
    print()

    if not _confirm_twice(
        "Remove all providers and projects? [y/N] ",
        "This cannot be undone. Confirm again [y/N] ",
    ):
        return

    store, hub_dir = _open_store()
    try:
        # Delete projects first (also removes bindings, preventing FK errors on provider delete)
        for proj in projects:
            if proj.path:
                proj_dir = Path(proj.path)
                for fname in _remove_project_files(proj_dir):
                    print(f"  removed {proj.path}/{fname}")
                for desc in _cleanup_installer_registration(proj_dir):
                    print(f"  removed {desc}")
            store.delete_project(proj.id)
            print(f"  deleted project '{proj.name}'")

        # Delete providers
        for prov in providers:
            try:
                store.delete_provider(prov.id)
            except Exception as exc:
                log.warning("Could not delete provider '%s': %s", prov.label, exc)
            print(f"  deleted provider '{prov.label}'")

        # Wipe encrypted key store
        try:
            from .core.crypto import load_master_key, encrypt_providers
            master_key = load_master_key(hub_dir)
            encrypt_providers({}, master_key, hub_dir)
        except Exception as exc:
            log.warning("Could not wipe providers.enc: %s", exc)
    finally:
        store.close()

    print(f"\n✓ Clean complete.")
    print(f"  Data directory {hub_dir} is now empty.")
    print("  LeafHub is still installed — run 'leafhub uninstall' to remove everything.")


# ── Uninstall helpers ──────────────────────────────────────────────────────────

def _remove_leafhub_self(install_dir: Path, hub_dir: Path) -> None:
    """Remove LeafHub's own CLI registration, PATH block, data dir, and source tree."""
    import shutil as _shutil

    _MARKER   = "# >>> leafhub PATH >>>"
    _ENDMARK  = "# <<< leafhub PATH <<<"
    _RC_NAMES = (".zprofile", ".zshrc", ".bashrc", ".bash_profile", ".profile")

    if os.name == "posix":
        # 1. Remove CLI symlink
        leafhub_link = Path.home() / ".local" / "bin" / "leafhub"
        if leafhub_link.is_symlink():
            try:
                leafhub_link.unlink()
                print(f"  removed {leafhub_link}")
            except OSError as exc:
                log.warning("Could not remove %s: %s", leafhub_link, exc)
        else:
            print(f"  (CLI symlink not found at {leafhub_link} — skipped)")

        # 2. Remove leafhub PATH block from shell RC files
        for rc_name in _RC_NAMES:
            rc = Path.home() / rc_name
            if not rc.exists():
                continue
            try:
                original = rc.read_text(encoding="utf-8", errors="replace")
                if _MARKER not in original:
                    continue
                lines = original.splitlines(keepends=True)
                out, inside = [], False
                for line in lines:
                    if _MARKER in line:
                        inside = True
                        continue
                    if _ENDMARK in line:
                        inside = False
                        continue
                    if not inside:
                        out.append(line)
                rc.write_text("".join(out), encoding="utf-8")
                print(f"  removed leafhub PATH block from ~/{rc_name}")
            except OSError as exc:
                log.warning("Could not clean %s: %s", rc, exc)

    elif os.name == "nt":
        # Windows: remove .venv\Scripts from User PATH
        venv_scripts = str(install_dir / ".venv" / "Scripts").rstrip("\\")
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Environment", 0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            )
            try:
                current_path, reg_type = winreg.QueryValueEx(key, "Path")
                entries  = [e for e in current_path.split(";") if e.strip()]
                filtered = [
                    e for e in entries
                    if e.strip().rstrip("\\").lower() != venv_scripts.lower()
                ]
                if len(filtered) < len(entries):
                    winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(filtered))
                    print(r"  removed leafhub .venv\Scripts from User PATH")
            finally:
                winreg.CloseKey(key)
        except (ImportError, OSError, PermissionError) as exc:
            log.warning("Could not clean Windows User PATH: %s", exc)

    # 3. Remove data directory (~/.leafhub/)
    if hub_dir.exists():
        try:
            _shutil.rmtree(hub_dir)
            print(f"  removed {hub_dir}")
        except OSError as exc:
            log.warning("Could not remove %s: %s", hub_dir, exc)

    # 4. Remove source tree — do this last; the running process keeps its
    #    already-loaded bytecode in memory so deletion is safe on POSIX.
    if install_dir.is_dir() and (install_dir / "setup.sh").exists():
        try:
            _shutil.rmtree(install_dir)
            print(f"  removed {install_dir}")
        except OSError as exc:
            log.warning("Could not remove install directory %s: %s", install_dir, exc)
    else:
        print(f"  (Install directory {install_dir} not found or unrecognised — skipped)")


# ── Uninstall command ──────────────────────────────────────────────────────────

def cmd_uninstall(args: argparse.Namespace) -> None:
    """Clean all data, remove LeafHub's CLI registration, and delete the source tree."""
    from .core import default_hub_dir
    from .manage.projects import (
        _cleanup_installer_registration,
        _remove_project_files,
    )

    install_dir = Path(__file__).resolve().parents[2]  # cli.py → leafhub/ → src/ → install root
    hub_dir     = default_hub_dir()

    # Preflight summary
    store, _ = _open_store()
    try:
        providers = store.list_providers()
        projects  = store.list_projects()
    finally:
        store.close()

    print("\nLeafHub uninstall will:")
    print(f"  1. Remove {len(providers)} provider(s) and {len(projects)} project(s)")
    print("     (same as 'leafhub clean' — project artefacts + CLI registrations removed)")
    print(f"  2. Remove the leafhub CLI symlink and PATH entries from shell RC files")
    print(f"  3. Remove the data directory    : {hub_dir}")
    print(f"  4. Remove the install directory : {install_dir}")
    print()

    if not _confirm_twice(
        "Uninstall LeafHub completely? [y/N] ",
        "This will delete all stored API keys and cannot be undone. Confirm [y/N] ",
    ):
        return

    # Phase 1: clean projects and providers (no re-prompting)
    print("\n── Phase 1: removing projects and providers ──")
    store, _ = _open_store()
    try:
        for proj in projects:
            if proj.path:
                proj_dir = Path(proj.path)
                for fname in _remove_project_files(proj_dir):
                    print(f"  removed {proj.path}/{fname}")
                for desc in _cleanup_installer_registration(proj_dir):
                    print(f"  removed {desc}")
            store.delete_project(proj.id)
            print(f"  deleted project '{proj.name}'")

        for prov in providers:
            try:
                store.delete_provider(prov.id)
            except Exception as exc:
                log.warning("Could not delete provider '%s': %s", prov.label, exc)
            print(f"  deleted provider '{prov.label}'")
    finally:
        store.close()

    if not providers and not projects:
        print("  (nothing to remove)")

    # Phase 2: remove LeafHub itself
    print("\n── Phase 2: removing LeafHub installation ──")
    _remove_leafhub_self(install_dir, hub_dir)

    print("\n✓ LeafHub uninstalled.")
    print("  Open a new terminal to reload your shell environment.")


# ── Register command ───────────────────────────────────────────────────────────

def cmd_register(args: argparse.Namespace) -> None:
    """
    Full project registration flow: create/re-link project, guide provider
    setup if none exist, then auto-bind a provider to the project.

    Designed to be called from install scripts via:
        leafhub register <name> --path <dir> [--alias <alias>] [--headless]
    """
    import json
    import subprocess

    from .manage.projects import (
        _detect_project_cli, _distribute_integration_files,
        _is_integrated, _register_cli_symlinks, _write_dotfile,
    )

    name     = args.project_name
    raw_path = getattr(args, "path", None)
    path     = Path(_strip_path_quotes(raw_path)).resolve() if raw_path else Path.cwd()
    alias    = getattr(args, "alias", None) or "default"
    headless = getattr(args, "headless", False)

    if not path.is_dir():
        _die(f"Project directory not found: {path}")

    # ── 1. Create or re-link project ─────────────────────────────────────────
    # Capture hub_dir before entering the try block so it remains accessible
    # later in the function (Python has no block scoping).
    store, hub_dir = _open_store()
    try:
        existing = store.find_project_by_name(name)
        if existing is not None:
            raw_token = store.rotate_token(existing.id)
            store.set_project_path(existing.id, str(path))
            project_id = existing.id
            _write_dotfile(path, name, raw_token)
            # Already integrated — only refresh .leafhub; leave existing files alone.
            print(f"✓ Project '{name}' re-linked to {path}.")
        else:
            project, raw_token = store.create_project(name)
            project_id = project.id
            store.set_project_path(project_id, str(path))
            _write_dotfile(path, name, raw_token)
            distributed = _distribute_integration_files(path)
            if distributed:
                print(f"  Integration files written: {', '.join(distributed)}.")
            print(f"✓ Project '{name}' created and linked to {path}.")
    finally:
        store.close()

    # ── 2. CLI registration ───────────────────────────────────────────────────
    # Detect executables in <project>/.venv/bin/ that aren't yet symlinked in
    # ~/.local/bin/.  In headless mode (or non-interactive stdin) we register
    # automatically; otherwise we ask once before proceeding.
    unregistered = _detect_project_cli(path)
    if unregistered:
        names = [n for n, _ in unregistered]
        do_register = headless or not sys.stdin.isatty()
        if not do_register:
            print()
            print(f"  Detected CLI tool(s) not yet registered: {', '.join(names)}")
            print("  Register to ~/.local/bin? [Y/n] ", end="", flush=True)
            try:
                ans = input().strip().lower()
            except EOFError:
                ans = ""
            do_register = ans in ("", "y", "yes")
        if do_register:
            registered = _register_cli_symlinks(path)
            if registered:
                print(f"✓ CLI registered: {', '.join(registered)}")
        else:
            print(f"  Skipped. Re-run setup.sh or: leafhub project link {name} --path {path}")

    # ── 3. Check providers ────────────────────────────────────────────────────
    store, _ = _open_store()
    providers = store.list_providers()
    store.close()

    if not providers and not headless and sys.stdin.isatty():
        print()
        print(f"  Project '{name}' needs an AI provider.")
        print("  LeafHub stores API keys encrypted on this machine — nothing leaves your system.")
        print("  Supported: OpenAI · Anthropic · Groq · Mistral · OpenRouter · xAI · Ollama · vLLM")
        print()
        print("  How would you like to configure your provider?")
        print("    [1] Launch Web UI   — visual setup at http://localhost:8765  (recommended)")
        print("    [2] Terminal        — step-by-step CLI prompts")
        print("    [s] Skip            — configure later with: leafhub provider add")
        print()
        try:
            choice = input("  Choice [1]: ").strip() or "1"
        except EOFError:
            choice = "s"

        if choice in ("1", ""):
            leafhub_bin = sys.argv[0]
            proc = subprocess.Popen(
                [leafhub_bin, "manage", "--no-browser"],
                start_new_session=True,
            )
            print()
            print("  Web UI running at http://localhost:8765")
            print("  Add a provider, then come back here.")
            try:
                input("\n  Press Enter when done...")
            except EOFError:
                pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        elif choice == "2":
            store_tmp, _ = _open_store()
            try:
                _prompt_new_provider(store_tmp, hub_dir)
            finally:
                store_tmp.close()
        else:
            print(f"  Skipped. Run: leafhub provider add")
            print(f"  Then bind:   leafhub project bind {name} --alias {alias} --provider <name>")
            return

        # Re-read providers after setup
        store, _ = _open_store()
        providers = store.list_providers()
        store.close()

    # ── 3. Auto-bind provider ─────────────────────────────────────────────────
    if not providers:
        if not headless:
            print()
            print(f"  No providers configured. Bind later:")
            print(f"    leafhub provider add")
            print(f"    leafhub project bind {name} --alias {alias} --provider <name>")
        return

    if len(providers) == 1:
        chosen = providers[0]
    elif headless:
        chosen = providers[0]
    else:
        print()
        print(f"  Multiple providers — which should '{name}' use?")
        for i, p in enumerate(providers, 1):
            print(f"    [{i}] {p.label}  ({p.api_format})")
        try:
            raw = input("  Choice [1]: ").strip() or "1"
            idx = int(raw) - 1
            chosen = providers[idx] if 0 <= idx < len(providers) else providers[0]
        except (EOFError, ValueError, IndexError):
            chosen = providers[0]

    store, _ = _open_store()
    try:
        # Check if this alias is already bound and skip to avoid duplicate noise.
        existing_bindings = store.list_bindings(project_id)
        already_bound = any(b.alias == alias for b in existing_bindings)
        if already_bound:
            print(f"✓ Alias '{alias}' already bound — skipping re-bind.")
        else:
            store.add_binding(
                project_id=project_id,
                alias=alias,
                provider_id=chosen.id,
            )
            print(f"✓ Bound '{chosen.label}' → '{name}' (alias: {alias})")
    except Exception as exc:
        # Print visibly — a silent warning left users with no binding and no explanation.
        print(
            f"[!] Auto-bind failed ({type(exc).__name__}: {exc})\n"
            f"    Fix: leafhub project bind {name} --alias {alias} --provider {chosen.label}",
            file=sys.stderr,
        )
    finally:
        store.close()


# ── Shell-helper command ────────────────────────────────────────────────────────

def cmd_shell_helper(args: argparse.Namespace) -> None:
    """Print register.sh content for eval in install scripts.

    Usage in setup.sh (v2 standard, 2026-03-21):

        eval "$(leafhub shell-helper 2>/dev/null)" \\
            || source "$SCRIPT_DIR/leafhub_dist/register.sh"

    ``leafhub shell-helper`` outputs register.sh to stdout for the calling
    shell to eval.  The local ``leafhub_dist/register.sh`` (distributed to
    the project at registration time) is the offline fallback — it is sourced
    directly when the leafhub binary is absent or not yet installed.
    """
    import importlib.resources as _pkg_res

    # Primary: package data (works when installed or in editable mode)
    try:
        content = _pkg_res.files("leafhub").joinpath("register.sh").read_text(encoding="utf-8")
        print(content, end="")
        return
    except (FileNotFoundError, TypeError, AttributeError):
        pass

    # Fallback: git checkout layout (Leafhub/register.sh).
    # This path only works in a development checkout — the relative traversal
    # (cli.py → leafhub/ → src/ → Leafhub/) is not guaranteed in installed packages.
    register_sh = Path(__file__).parent.parent.parent / "register.sh"
    if not register_sh.exists():
        _die(
            f"register.sh not found (tried package data and {register_sh}).\n"
            "  Fetch directly: curl -fsSL "
            "https://raw.githubusercontent.com/Rebas9512/Leafhub/main/register.sh"
        )
    print(register_sh.read_text(encoding="utf-8"), end="")


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leafhub",
        description="Manage LeafHub encrypted API key vault.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── provider ──────────────────────────────────────────────────────────────
    p_prov = sub.add_parser("provider", help="Manage providers")
    p_prov_sub = p_prov.add_subparsers(dest="subcommand", metavar="<subcommand>")
    p_prov_sub.required = True

    # provider add
    p_add = p_prov_sub.add_parser("add", help="Add a provider")
    p_add.add_argument("--name",          required=True,  help="Display label")
    p_add.add_argument("--key",           default=None,
                       help="API key. If omitted, prompted interactively (recommended — "
                            "avoids key appearing in shell history and process list)")
    p_add.add_argument("--type",          default="custom",
                       choices=["openai", "anthropic", "ollama", "custom"],
                       help="Provider type (default: custom)")
    p_add.add_argument("--format",
                       choices=["openai-completions", "anthropic-messages", "ollama"],
                       help="API format (default: openai-completions)")
    p_add.add_argument("--base-url",      help="Override base URL")
    p_add.add_argument("--default-model", help="Default model name")
    p_add.add_argument("--models",        help="Comma-separated list of available models")
    p_add.add_argument("--auth-mode",     choices=["bearer", "x-api-key", "none"],
                       help="How to inject the API key (default: inferred from --format)")
    p_add.add_argument("--auth-header",   help="Override auth header name (e.g. 'api-key' for Azure)")
    p_add.add_argument("--extra-header",  action="append", metavar="NAME:VALUE",
                       help="Extra fixed request header; may be repeated "
                            "(e.g. --extra-header 'anthropic-version: 2023-06-01')")
    p_add.set_defaults(func=cmd_provider_add)

    # provider list
    p_lst = p_prov_sub.add_parser("list", help="List all providers")
    p_lst.add_argument("--json", action="store_true",
                       help="Output as JSON array (for scripting)")
    p_lst.set_defaults(func=cmd_provider_list)

    # provider show
    p_show = p_prov_sub.add_parser("show", help="Show provider details")
    p_show.add_argument("--name", required=True, help="Provider label")
    p_show.set_defaults(func=cmd_provider_show)

    # provider delete
    p_del = p_prov_sub.add_parser("delete", help="Delete a provider")
    p_del.add_argument("--name", required=True, help="Provider label")
    p_del.set_defaults(func=cmd_provider_delete)

    # ── project ───────────────────────────────────────────────────────────────
    p_proj = sub.add_parser("project", help="Manage projects")
    p_proj_sub = p_proj.add_subparsers(dest="subcommand", metavar="<subcommand>")
    p_proj_sub.required = True

    # project create
    p_pc = p_proj_sub.add_parser("create", help="Create a project (outputs token once)")
    p_pc.add_argument("project_name", help="Project name")
    p_pc.add_argument("--path",     metavar="DIR",
                      help="Link to a local directory and write .leafhub immediately")
    p_pc.add_argument("--yes", "-y", action="store_true",
                      help="Skip interactive bind wizard (for use in scripts)")
    p_pc.add_argument("--if-not-exists", action="store_true", dest="if_not_exists",
                      help="Re-link silently if project already exists instead of erroring")
    p_pc.set_defaults(func=cmd_project_create)

    # project link
    p_lnk = p_proj_sub.add_parser(
        "link",
        help="Link an existing project to a local directory (rotates token, writes .leafhub)",
    )
    p_lnk.add_argument("project_name", help="Project name")
    p_lnk.add_argument("--path", required=True, metavar="DIR",
                       help="Absolute path to the project directory")
    p_lnk.set_defaults(func=cmd_project_link)

    # project list
    p_pl = p_proj_sub.add_parser("list", help="List all projects")
    p_pl.set_defaults(func=cmd_project_list)

    # project show
    p_ps = p_proj_sub.add_parser("show", help="Show project details and bindings")
    p_ps.add_argument("project_name", help="Project name")
    p_ps.set_defaults(func=cmd_project_show)

    # project token
    p_pt = p_proj_sub.add_parser("token", help="Rotate project token (old token invalidated)")
    p_pt.add_argument("project_name", help="Project name")
    p_pt.set_defaults(func=cmd_project_token)

    # project bind
    p_pb = p_proj_sub.add_parser("bind", help="Bind an alias to a provider")
    p_pb.add_argument("project_name",            help="Project name")
    p_pb.add_argument("--alias",    required=True, help="Alias (e.g. 'gpt-4')")
    p_pb.add_argument("--provider", required=True, help="Provider label")
    p_pb.add_argument("--model",                  help="Override model name")
    p_pb.set_defaults(func=cmd_project_bind)

    # project unbind
    p_pu = p_proj_sub.add_parser("unbind", help="Remove an alias binding")
    p_pu.add_argument("project_name",            help="Project name")
    p_pu.add_argument("--alias", required=True, help="Alias to remove")
    p_pu.set_defaults(func=cmd_project_unbind)

    # project delete
    p_pd = p_proj_sub.add_parser("delete", help="Delete a project and its bindings")
    p_pd.add_argument("project_name", help="Project name")
    p_pd.set_defaults(func=cmd_project_delete)

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Show storage status")
    p_status.add_argument("--json", action="store_true",
                          help="Output as JSON (for scripting)")
    p_status.set_defaults(func=cmd_status)

    # ── manage ────────────────────────────────────────────────────────────────
    p_manage = sub.add_parser(
        "manage",
        help="Start the Web UI management server",
        description=(
            "Build the Vue UI (if needed) and start the management server.\n\n"
            "First run performs 'npm install + npm run build' automatically.\n"
            "Subsequent runs skip the build unless --rebuild is given."
        ),
    )
    p_manage.add_argument(
        "--port", type=int, default=8765,
        help="FastAPI backend port (default: 8765)",
    )
    p_manage.add_argument(
        "--rebuild", action="store_true",
        help="Force a fresh UI build even if ui/dist/ already exists",
    )
    p_manage.add_argument(
        "--dev", action="store_true",
        help="Development mode: start Vite dev server (port 5173) with hot-reload",
    )
    p_manage.add_argument(
        "--no-browser", action="store_true", dest="no_browser",
        help="Do not open a browser window automatically",
    )
    p_manage.set_defaults(func=cmd_manage)

    # ── register ──────────────────────────────────────────────────────────────
    p_reg = sub.add_parser(
        "register",
        help="Register a project: create/re-link, guide provider setup, auto-bind",
        description=(
            "One-shot project registration for use in install scripts.\n\n"
            "Creates the project if it does not exist, re-links if it does,\n"
            "guides provider setup when none are configured, then auto-binds\n"
            "a provider to the project under the given alias."
        ),
    )
    p_reg.add_argument("project_name", help="Project name")
    p_reg.add_argument("--path", metavar="DIR",
                       help="Project directory (default: current directory)")
    p_reg.add_argument("--alias", default="default",
                       help="Binding alias to create (default: default)")
    p_reg.add_argument("--headless", action="store_true",
                       help="Non-interactive mode: skip all prompts, auto-select first provider")
    p_reg.set_defaults(func=cmd_register)

    # ── shell-helper ──────────────────────────────────────────────────────────
    p_sh = sub.add_parser(
        "shell-helper",
        help="Print register.sh for eval in install scripts",
        description=(
            "Outputs the contents of register.sh — the standard shell module\n"
            "for project bootstrap — so install scripts can source it without curl:\n\n"
            "    eval \"$(leafhub shell-helper)\""
        ),
    )
    p_sh.set_defaults(func=cmd_shell_helper)

    # ── clean ─────────────────────────────────────────────────────────────────
    p_clean = sub.add_parser(
        "clean",
        help="Remove all providers and projects (requires two confirmations)",
        description=(
            "Permanently removes all stored providers and projects.\n\n"
            "For each linked project: removes .leafhub, leafhub_probe.py, and\n"
            "register.sh from the project directory, and strips CLI symlinks /\n"
            "shell PATH entries added by the project's installer.\n\n"
            "The LeafHub installation itself is NOT removed.\n"
            "Requires two explicit confirmations."
        ),
    )
    p_clean.set_defaults(func=cmd_clean)

    # ── uninstall ──────────────────────────────────────────────────────────────
    p_uninst = sub.add_parser(
        "uninstall",
        help="Clean all data, remove LeafHub CLI registration, and delete source files",
        description=(
            "Full removal of LeafHub from this machine.\n\n"
            "Step 1 — Clean: removes all providers, projects, project artefacts,\n"
            "         and project CLI registrations (same as 'leafhub clean').\n"
            "Step 2 — Self-remove: deletes the leafhub CLI symlink, strips the\n"
            "         leafhub PATH block from shell RC files, removes ~/.leafhub/\n"
            "         (encrypted keys + DB), and deletes the install directory.\n\n"
            "Requires two explicit confirmations."
        ),
    )
    p_uninst.set_defaults(func=cmd_uninstall)

    return parser


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        _die(str(exc))


if __name__ == "__main__":
    main()

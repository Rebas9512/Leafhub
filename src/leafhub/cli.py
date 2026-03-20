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
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


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
    from .manage.projects import _copy_probe_to_project, _write_dotfile

    name       = args.project_name
    raw_path   = getattr(args, "path", None)
    link_path  = Path(_strip_path_quotes(raw_path)).resolve() if raw_path else None
    copy_probe = not getattr(args, "no_probe", False)

    if link_path is not None and not link_path.is_dir():
        _die(f"Link directory not found: {args.path}")

    store, _ = _open_store()
    project, raw_token = store.create_project(name)

    if link_path is not None:
        _write_dotfile(link_path, name, raw_token)
        if copy_probe:
            _copy_probe_to_project(link_path)
        store.set_project_path(project.id, str(link_path))
        store.close()
        print(f"✓ Project '{name}' created and linked to {link_path}.")
        print(f"  .leafhub written — project auto-detects credentials on startup.")
        if copy_probe:
            print(f"  leafhub_probe.py copied to project root.")
    else:
        store.close()
        print(f"✓ Project '{name}' created.")
        _print_token_box(raw_token)
        print("  Add to your project .env:")
        print(f"    LEAFHUB_TOKEN={raw_token}\n")


def cmd_project_link(args: argparse.Namespace) -> None:
    from .manage.projects import _copy_probe_to_project, _write_dotfile

    name       = args.project_name
    link_path  = Path(_strip_path_quotes(args.path)).resolve()
    copy_probe = not getattr(args, "no_probe", False)

    if not link_path.is_dir():
        _die(f"Directory not found: {args.path}")

    store, _ = _open_store()
    p = store.find_project_by_name(name)
    if p is None:
        store.close()
        _die(f"Project '{name}' not found.")

    # Rotate token so the new dotfile is the only valid credential.
    raw_token = store.rotate_token(p.id)
    store.set_project_path(p.id, str(link_path))
    store.close()

    _write_dotfile(link_path, name, raw_token)
    if copy_probe:
        _copy_probe_to_project(link_path)

    print(f"✓ Project '{name}' linked to {link_path}.")
    print(f"  .leafhub written — token rotated, old token invalidated.")
    if copy_probe:
        print(f"  leafhub_probe.py copied to project root.")


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

    store.delete_project(p.id)
    store.close()
    print(f"✓ Project '{name}' deleted.")


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


# ── Uninstall ──────────────────────────────────────────────────────────────────

def cmd_uninstall(args: argparse.Namespace) -> None:
    """Delegate to setup.sh --uninstall."""
    import subprocess
    setup_sh = Path(__file__).parent.parent.parent / "setup.sh"
    if not setup_sh.exists():
        _die(
            f"setup.sh not found at {setup_sh}.\n"
            "  Run manually: rm ~/.local/bin/leafhub  (and remove the leafhub PATH "
            "block from ~/.bashrc / ~/.zshrc)"
        )
    subprocess.run(["bash", str(setup_sh), "--uninstall"], check=True)


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
    p_pc.add_argument("--no-probe", action="store_true",
                      help="Skip copying leafhub_probe.py to the project root "
                           "(only applies when --path is given)")
    p_pc.set_defaults(func=cmd_project_create)

    # project link
    p_lnk = p_proj_sub.add_parser(
        "link",
        help="Link an existing project to a local directory (rotates token, writes .leafhub)",
    )
    p_lnk.add_argument("project_name", help="Project name")
    p_lnk.add_argument("--path", required=True, metavar="DIR",
                       help="Absolute path to the project directory")
    p_lnk.add_argument("--no-probe", action="store_true",
                       help="Skip copying leafhub_probe.py to the project root")
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

    # ── uninstall ──────────────────────────────────────────────────────────────
    p_uninst = sub.add_parser(
        "uninstall",
        help="Remove the leafhub CLI registration (symlink, PATH, venv)",
        description=(
            "Reverses what 'python scripts/setup.py' did:\n"
            "removes the symlink, PATH entries, and the project venv.\n\n"
            "The ~/.leafhub/ data directory (API keys, DB) is NOT removed."
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

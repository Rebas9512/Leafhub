#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — Project Registration Helper  (register.sh)
#
#  PURPOSE
#  ───────
#  This file is the standard integration module for linking any project to
#  LeafHub.  It provides a single shell function — leafhub_setup_project() —
#  that handles the complete registration flow:
#
#    1. Detect the leafhub binary in PATH; install it automatically if absent.
#    2. Call `leafhub register` to create or re-link the project (idempotent).
#    3. Guide API provider setup when no providers are configured yet.
#    4. Auto-bind a provider to the project under the specified alias.
#    5. Write leafhub_dist/ into the project directory for runtime detection.
#
#  HOW TO USE IN A NEW PROJECT  (v2 standard, 2026-03-21)
#  ───────────────────────────────────────────────────────
#  Add the following block to your project's setup.sh, after the venv step:
#
#    # ── LeafHub integration ───────────────────────────────────────────────────
#    # Resolution order (v2 standard, 2026-03-21):
#    #   1. leafhub shell-helper     — system PATH binary (fast path, offline)
#    #   2. leafhub_dist/register.sh — local distributed copy (offline fallback)
#    #   3. GitHub curl              — first-time bootstrap, network required
#    # NOTE: `eval "$(cmd)"` is NOT used — eval "" always exits 0, making the
#    # fallback unreachable when leafhub is absent from PATH.
#    _lh_content=""
#    if _lh_content="$(leafhub shell-helper 2>/dev/null)" && [[ -n "$_lh_content" ]]; then
#        eval "$_lh_content"
#    elif [[ -f "$SCRIPT_DIR/leafhub_dist/register.sh" ]]; then
#        source "$SCRIPT_DIR/leafhub_dist/register.sh"
#    else
#        _TMP_REG="$(mktemp)"
#        if ! curl -fsSL \
#                https://raw.githubusercontent.com/Rebas9512/Leafhub/main/register.sh \
#                -o "$_TMP_REG" 2>/dev/null; then
#            rm -f "$_TMP_REG"
#            fail "Could not fetch LeafHub installer."
#        fi
#        source "$_TMP_REG"
#        rm -f "$_TMP_REG"
#    fi
#    unset _lh_content
#    leafhub_setup_project "my-project-name" "$SCRIPT_DIR" "my-alias" \
#        || fail "LeafHub registration failed."
#
#  Optional 4th tier (when leafhub is a pip dependency of your project):
#  Insert after tier 1:
#    elif [[ -x "$VENV_DIR/bin/leafhub" ]] \
#        && _lh_content="$("$VENV_DIR/bin/leafhub" shell-helper 2>/dev/null)" \
#        && [[ -n "$_lh_content" ]]; then
#        eval "$_lh_content"
#
#  Last argument to leafhub_setup_project is the alias your runtime code uses
#  with hub.get_key("<alias>").  See the ARGUMENTS section below.
#  Treat a non-zero return as fatal (see FAILURE BEHAVIOUR below).
#
#  Only two things need changing per project:
#    - The name argument: lowercase slug matching the repository name.
#      Examples: "trileaf", "my-toolkit", "data-pipeline"
#    - The path argument: typically $SCRIPT_DIR (the directory containing setup.sh).
#
#
#  WHAT `leafhub register` DOES UNDER THE HOOD
#  ────────────────────────────────────────────
#  When leafhub_setup_project() runs `leafhub register <name> --path <dir>`:
#
#    a) Project create / re-link (idempotent):
#       If no project with this name exists, a new one is created.
#       If one already exists, the token is rotated and the path is updated.
#       A `.leafhub` token file (chmod 600) is written to the project directory
#       and auto-added to .gitignore.
#
#    b) Provider setup (interactive, skipped if headless):
#       If no API providers are configured in the vault, the wizard opens and
#       prompts the user to add one (name, base URL, API key, model, etc.).
#       The provider is connectivity-probed before being saved.
#
#    c) Auto-bind:
#       If exactly one provider exists, it is bound automatically under the
#       default alias.  If multiple providers exist, the user is asked to pick.
#       Binding records which provider and model to use for this project.
#
#    d) Distribute integration module (v2 standard, 2026-03-21):
#       leafhub_dist/ is written into the project root on first registration.
#       This directory contains probe.py (stdlib-only runtime detection),
#       register.sh (this file), and __init__.py (makes it importable as a
#       Python package).  The project imports probe.py at runtime to
#       auto-detect credentials — no token in source code, no env vars needed.
#       Re-registration (token rotation / re-link) refreshes the dotfile only;
#       the leafhub_dist/ directory is not overwritten.
#
#
#  RUNTIME CREDENTIAL RESOLUTION (what happens in your project at startup)
#  ─────────────────────────────────────────────────────────────────────────
#  IMPORTANT — two-tier dependency model:
#    detect()     is stdlib-only: works without leafhub pip package.
#    open_sdk()   requires the leafhub pip package (imports leafhub.sdk).
#
#  Your project MUST declare leafhub as a pip dependency so that open_sdk()
#  works at runtime.  Add to pyproject.toml and setup.sh:
#
#    # pyproject.toml
#    [project.optional-dependencies]
#    leafhub = ["leafhub @ git+https://github.com/Rebas9512/Leafhub.git"]
#
#    # setup.sh — after venv creation and main deps install
#    "$VENV_PIP" install -e "$SCRIPT_DIR[leafhub]" --quiet
#
#  After setup, your project's startup code resolves credentials like this:
#
#    # Ensure project root is on sys.path so leafhub_dist is importable
#    # when leafhub pip package is absent (editable installs expose only
#    # named packages, not the project root directory itself).
#    import sys
#    from pathlib import Path
#    _proj_root = str(Path(__file__).resolve().parent)  # adjust depth as needed
#    if _proj_root not in sys.path:
#        sys.path.insert(0, _proj_root)
#
#    try:
#        from leafhub.probe import detect          # pip package (preferred)
#    except ImportError:
#        from leafhub_dist.probe import detect     # local distributed fallback
#
#    found = detect()                              # fast, never raises, < 1 s
#    if found.ready:
#        hub  = found.open_sdk()                   # requires leafhub pip package
#        key  = hub.get_key("my-alias")            # decrypted API key string
#        cfg  = hub.get_config("my-alias")         # base_url, model, auth_mode, ...
#
#  If found.ready is False (project not linked, token expired, etc.), fall back
#  to environment variables or show a helpful error.
#
#
#  ENVIRONMENT VARIABLES
#  ─────────────────────
#  LEAFHUB_HEADLESS=1
#      Non-interactive / CI mode.  Skips all provider setup prompts.
#      Set this when running unattended installs or in CI pipelines.
#      Example:
#        [[ "$HEADLESS" == "true" ]] && export LEAFHUB_HEADLESS=1
#        leafhub_setup_project "my-project" "$SCRIPT_DIR"
#
#  LEAFHUB_CALLER=1  (set automatically by LeafHub — do not set manually)
#      When a project is registered from the LeafHub Web UI or via
#      `leafhub project link`, LeafHub may run your setup.sh automatically
#      (with --headless) to install the project and register its CLI.
#      To prevent infinite recursion (setup.sh → leafhub register →
#      setup.sh again), LeafHub sets LEAFHUB_CALLER=1 in the subprocess
#      environment before invoking setup.sh.
#
#      Any nested `leafhub register` call inherits this variable and skips
#      the auto-setup step.  You do not need to read or forward it manually.
#
#      If you want to detect that your setup.sh was invoked by LeafHub
#      rather than by the user directly, you can check:
#        [[ "${LEAFHUB_CALLER:-0}" == "1" ]] && echo "invoked by LeafHub"
#
#  LEAFHUB_DIR=<path>
#      Custom install directory for LeafHub (forwarded to the LeafHub
#      installer if it runs from scratch).  Defaults to ~/leafhub.
#
#
#  LEAFHUB-INITIATED REGISTRATION FLOW  (standard v2, updated 2026-03-21)
#  ────────────────────────────────────
#  When a user registers a project from the LeafHub Web UI or via
#  `leafhub project link`, LeafHub performs these steps automatically:
#
#    1. Write .leafhub token file (chmod 600) to project directory.
#    2. Distribute leafhub_dist/ integration module (first link only):
#         leafhub_dist/__init__.py  — Python package entrypoint
#         leafhub_dist/probe.py     — stdlib-only runtime detection
#         leafhub_dist/register.sh  — this file (shell integration module)
#       Re-link / token rotation: only the .leafhub dotfile is updated.
#    3. Auto-bind the first available provider under the requested alias
#       (v2 addition — the link endpoint now accepts an `alias` field;
#        callers must pass the alias they intend to query at runtime, e.g.
#        "rewrite" for Trileaf).  If no providers are configured the bind
#        is skipped; add a provider first, then run:
#          leafhub project bind <name> --alias <alias> --provider <name>
#    4. If setup.sh is present AND .venv is absent:
#         → run `bash setup.sh --headless` with LEAFHUB_CALLER=1
#         → setup.sh installs the project, creates .venv, calls leafhub register
#    5. Detect executables in .venv/bin/ that are not Python stdlib tools
#       and not yet in ~/.local/bin/ → create symlinks automatically.
#
#  For this to work correctly, your setup.sh must:
#    a) Support the --headless flag (or LEAFHUB_HEADLESS=1 env var) to run
#       non-interactively without hanging on prompts.
#    b) Call `pip install -e .` (or equivalent) before calling
#       `leafhub_setup_project` so the CLI binary exists in .venv/bin/.
#    c) Define CLI entry points in pyproject.toml / setup.py so pip creates
#       the executable in .venv/bin/ automatically.
#
#  If setup.sh is absent, LeafHub assumes the project has no CLI to register
#  and skips step 3-4 entirely.  This is the correct behaviour for libraries
#  or projects that manage their own PATH separately.
#
#
#  IDEMPOTENCY
#  ───────────
#  Running setup.sh multiple times is safe:
#  - If the project already exists in LeafHub, the token is rotated and the
#    .leafhub file is updated in-place.  No duplicate projects are created.
#  - If a provider is already bound, the binding step is skipped silently.
#  - If leafhub is already installed, _leafhub_ensure() returns immediately.
#
#
#  FAILURE BEHAVIOUR
#  ─────────────────
#  LeafHub is a hard dependency — without it, credentials cannot be resolved
#  at runtime.  If _leafhub_ensure fails (network error, permission denied,
#  etc.), leafhub_setup_project returns 1.  Your setup.sh should abort:
#
#    leafhub_setup_project "my-project" "$SCRIPT_DIR" \
#        || { echo "[!] LeafHub registration failed."; exit 1; }
#
#  Do not add a silent fallback to plain env vars unless you explicitly intend
#  to support that as a long-term credential management path.
#
# ──────────────────────────────────────────────────────────────────────────────


# ── Internal: detect or install LeafHub ───────────────────────────────────────
#
# Sets LEAFHUB_BIN to the absolute path of the leafhub binary.
# Returns 0 on success, 1 if install fails or binary is still not found.
#
# Detection order:
#   1. command -v leafhub  (standard PATH lookup — fast, works offline)
#   2. curl + run the LeafHub install.sh bootstrap
#   3. Prepend ~/.local/bin to PATH and retry lookup
#
_leafhub_ensure() {
    # Fast path: binary already in PATH.
    LEAFHUB_BIN="$(command -v leafhub 2>/dev/null || true)"
    [[ -n "$LEAFHUB_BIN" ]] && return 0

    echo "  LeafHub not found — installing (required dependency) ..." >&2

    # Download installer to a temp file so LEAFHUB_DIR is forwarded via env.
    local _tmp
    _tmp="$(mktemp)"

    if ! curl -fsSL \
            "https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh" \
            -o "$_tmp" 2>/dev/null; then
        echo "  [!] LeafHub: failed to download installer (network error)." >&2
        echo "      Check your internet connection and retry." >&2
        rm -f "$_tmp"
        return 1
    fi

    if ! bash "$_tmp"; then
        echo "  [!] LeafHub: installer exited with an error." >&2
        echo "      Run manually: bash <(curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh)" >&2
        rm -f "$_tmp"
        return 1
    fi
    rm -f "$_tmp"

    # The installer adds ~/.local/bin to shell RC files, but those changes only
    # take effect in a new shell session.  Reload PATH here so the freshly-
    # installed binary is reachable in the current script without a new terminal.
    export PATH="$HOME/.local/bin:$PATH"
    hash -r 2>/dev/null || true   # clear bash's command-hash cache

    LEAFHUB_BIN="$(command -v leafhub 2>/dev/null || true)"
    if [[ -z "$LEAFHUB_BIN" ]]; then
        echo "  [!] LeafHub installed but 'leafhub' not found in PATH." >&2
        echo "      Open a new terminal and re-run this installer." >&2
        return 1
    fi
    return 0
}


# ── Public API ────────────────────────────────────────────────────────────────
#
# leafhub_setup_project <name> [path [alias]]
#
# The single function that new projects call from their setup.sh.
# Everything else is handled internally.
#
# ARGUMENTS
#   name   Required.  Project name in LeafHub — lowercase slug matching the
#          repository name (e.g. "trileaf", "my-toolkit", "api-gateway").
#          If a project with this name already exists, it is re-linked
#          (token rotated, directory path updated) — no duplicate is created.
#
#   path   Optional.  Absolute path to the project directory.
#          LeafHub writes the .leafhub token file here.
#          Defaults to the current working directory: $(pwd).
#          Typically: pass "$SCRIPT_DIR" (the directory containing setup.sh).
#
#   alias  Optional.  The binding alias your project queries at runtime via
#          hub.get_key("<alias>") or the LEAFHUB_ALIAS env var.
#          Defaults to "default" when omitted.
#          Pass this when your project uses a custom alias (e.g. "rewrite"):
#            leafhub_setup_project "my-project" "$SCRIPT_DIR" "rewrite"
#          This must match what your runtime code passes to hub.get_key().
#
# ENVIRONMENT
#   LEAFHUB_HEADLESS=1   Skip all interactive prompts.
#                        Set before calling if your setup.sh has --headless.
#
# RETURNS
#   0   Registration and binding completed successfully.
#   1   LeafHub install failed, or `leafhub register` returned non-zero.
#
# EXAMPLE — default alias
#   leafhub_setup_project "my-project" "$SCRIPT_DIR" \
#       || fail "LeafHub registration failed."
#
# EXAMPLE — custom alias
#   leafhub_setup_project "my-project" "$SCRIPT_DIR" "rewrite" \
#       || fail "LeafHub registration failed."
#
leafhub_setup_project() {
    local _name="${1:?leafhub_setup_project: project name required}"
    local _path="${2:-$(pwd)}"
    local _alias="${3:-}"   # optional; if omitted, leafhub register uses "default"

    # Ensure the binary exists; install it automatically if not found.
    _leafhub_ensure || return 1

    # Pass --headless when LEAFHUB_HEADLESS is set so leafhub register skips
    # all interactive prompts (provider setup wizard, binding selection, etc.).
    local _headless_flag=""
    [[ "${LEAFHUB_HEADLESS:-0}" == "1" ]] && _headless_flag="--headless"

    # Pass --alias when the project uses a non-default binding alias.
    local _alias_flag=""
    [[ -n "$_alias" ]] && _alias_flag="--alias $_alias"

    # Run the full registration flow (create/re-link → provider setup → bind).
    # shellcheck disable=SC2086
    "$LEAFHUB_BIN" register "$_name" --path "$_path" $_headless_flag $_alias_flag
}

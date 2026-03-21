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
#    4. Auto-bind a provider to the project under the default alias.
#    5. Copy leafhub_probe.py into the project directory for runtime detection.
#
#  HOW TO USE IN A NEW PROJECT  (3-line integration)
#  ──────────────────────────────────────────────────
#  Add these three lines to your project's setup.sh, after the venv step:
#
#    # ── LeafHub integration ───────────────────────────────────────────────────
#    eval "$(leafhub shell-helper 2>/dev/null)" \
#        || eval "$(curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/register.sh)"
#    leafhub_setup_project "my-project-name" "$SCRIPT_DIR" \
#        || fail "LeafHub registration failed."
#
#  Line 1: Try to get this file's content from the locally-installed leafhub
#           binary (`leafhub shell-helper` prints register.sh to stdout).
#           If leafhub is already installed, this works instantly and offline.
#           '2>/dev/null' silences the error when leafhub is not installed yet.
#
#  Line 2: Fallback — if leafhub is not yet installed, fetch this file directly
#           from GitHub and source it.  _leafhub_ensure() inside will then
#           install leafhub automatically before proceeding.
#
#  Line 3: Call the function.  Pass your project's slug name and directory.
#           Treat a non-zero return as fatal (see FAILURE BEHAVIOUR below).
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
#    d) Probe copy:
#       leafhub_probe.py is copied into the project root.  This is a
#       stdlib-only file that the project imports at runtime to auto-detect
#       its credentials — no token in source code, no env vars required.
#
#
#  RUNTIME CREDENTIAL RESOLUTION (what happens in your project at startup)
#  ─────────────────────────────────────────────────────────────────────────
#  After setup, your project's startup code resolves credentials like this:
#
#    from leafhub.probe import detect          # or: from leafhub_probe import detect
#    found = detect()                          # fast, never raises, < 1 s
#    if found.ready:
#        hub  = found.open_sdk()
#        key  = hub.get_key("default")         # decrypted API key string
#        cfg  = hub.get_config("default")      # base_url, model, auth_mode, ...
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
#  LEAFHUB-INITIATED REGISTRATION FLOW
#  ────────────────────────────────────
#  When a user registers a project from the LeafHub Web UI or via
#  `leafhub project link`, LeafHub performs these steps automatically:
#
#    1. Write .leafhub token file (chmod 600) to project directory.
#    2. Distribute register.sh and leafhub_probe.py (first link only).
#    3. If setup.sh is present AND .venv is absent:
#         → run `bash setup.sh --headless` with LEAFHUB_CALLER=1
#         → setup.sh installs the project, creates .venv, calls leafhub register
#    4. Detect executables in .venv/bin/ that are not Python stdlib tools
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
# leafhub_setup_project <name> [path]
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
# ENVIRONMENT
#   LEAFHUB_HEADLESS=1   Skip all interactive prompts.
#                        Set before calling if your setup.sh has --headless.
#
# RETURNS
#   0   Registration and binding completed successfully.
#   1   LeafHub install failed, or `leafhub register` returned non-zero.
#
# EXAMPLE
#   leafhub_setup_project "my-project" "$SCRIPT_DIR" \
#       || fail "LeafHub registration failed."
#
leafhub_setup_project() {
    local _name="${1:?leafhub_setup_project: project name required}"
    local _path="${2:-$(pwd)}"

    # Ensure the binary exists; install it automatically if not found.
    _leafhub_ensure || return 1

    # Pass --headless when LEAFHUB_HEADLESS is set so leafhub register skips
    # all interactive prompts (provider setup wizard, binding selection, etc.).
    local _headless_flag=""
    [[ "${LEAFHUB_HEADLESS:-0}" == "1" ]] && _headless_flag="--headless"

    # Run the full registration flow (create/re-link → provider setup → bind).
    "$LEAFHUB_BIN" register "$_name" --path "$_path" $_headless_flag
}

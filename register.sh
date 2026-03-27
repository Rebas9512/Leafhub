#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — Project Registration Helper  (register.sh)
#
#  Provides leafhub_setup_project() for use in project setup scripts.
#  Full documentation: https://github.com/Rebas9512/Leafhub
#
#  Source via (v3 standard — choose the first that succeeds):
#    1. eval "$(leafhub shell-helper 2>/dev/null)"
#    2. eval "$($VENV_DIR/bin/leafhub shell-helper 2>/dev/null)"
#    3. curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/register.sh
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
# ARGUMENTS
#   name   Required.  Project name — lowercase slug matching the repo name.
#          Idempotent: re-links and rotates the token if the project already exists.
#
#   path   Optional.  Absolute path to the project directory (default: pwd).
#          LeafHub writes .leafhub here. Pass "$SCRIPT_DIR" from setup.sh.
#
#   alias  Optional.  Binding alias used in hub.get_key("<alias>") at runtime.
#          Defaults to "default". Must exactly match your runtime code.
#
# ENVIRONMENT
#   LEAFHUB_HEADLESS=1   Skip all interactive prompts (CI / --headless mode).
#
# RETURNS
#   0   Registration and binding completed successfully.
#   1   LeafHub install failed, or `leafhub register` returned non-zero.
#
# EXAMPLES
#   leafhub_setup_project "my-project" "$SCRIPT_DIR" \
#       || fail "LeafHub registration failed."
#
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

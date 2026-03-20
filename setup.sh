#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — Setup  (macOS / Linux / WSL)
#
#  Developer / CI script — more control than install.sh.
#
#  Usage:
#    chmod +x setup.sh && ./setup.sh
#
#  Options:
#    --reinstall     Delete and recreate the .venv
#    --headless      Non-interactive / CI mode; sets --skip-setup
#    --doctor        Run environment check only, then exit
#    --uninstall     Remove symlink, PATH entries, and the project venv
#
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
LEAFHUB_LINK="$BIN_DIR/leafhub"

_MARKER='# >>> leafhub PATH >>>'
_ENDMARK='# <<< leafhub PATH <<<'
_PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
_RC_FILES=("$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile")

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
    BOLD='\033[1m'
    GREEN='\033[38;2;0;229;180m'
    YELLOW='\033[38;2;255;176;32m'
    RED='\033[38;2;230;57;70m'
    MUTED='\033[38;2;110;120;148m'
    NC='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' RED='' MUTED='' NC=''
fi

ok()      { echo -e "${GREEN}✓${NC}  $*"; }
info()    { echo -e "${MUTED}·${NC}  $*"; }
warn()    { echo -e "${YELLOW}!${NC}  $*"; }
fail()    { echo -e "${RED}✗${NC}  $*" >&2; exit 1; }
section() { echo ""; echo -e "${BOLD}── $* ──${NC}"; }

# ── Argument parsing ──────────────────────────────────────────────────────────
REINSTALL=false
HEADLESS=false
DOCTOR=false
UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --reinstall)  REINSTALL=true ;;
        --headless)   HEADLESS=true ;;
        --doctor)     DOCTOR=true ;;
        --uninstall)  UNINSTALL=true ;;
        --help|-h)
            echo "Usage: ./setup.sh [--reinstall] [--headless] [--doctor] [--uninstall]"
            exit 0
            ;;
        *) warn "Unknown option: $arg  (ignored)" ;;
    esac
done

# ── Uninstall ─────────────────────────────────────────────────────────────────
if [[ "$UNINSTALL" == "true" ]]; then
    echo ""
    echo -e "${BOLD}  LeafHub — Uninstall${NC}"

    section "Removing CLI registration"
    if [[ -L "$LEAFHUB_LINK" ]]; then
        rm -f "$LEAFHUB_LINK"
        ok "Removed symlink: $LEAFHUB_LINK"
    else
        warn "Symlink not found: $LEAFHUB_LINK"
    fi

    section "Cleaning shell rc files"
    for rc in "${_RC_FILES[@]}"; do
        [[ -f "$rc" ]] || continue
        grep -qF "$_MARKER" "$rc" 2>/dev/null || continue
        # Remove the block between markers
        python3 - "$rc" "$_MARKER" "$_ENDMARK" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1]); start = sys.argv[2]; end = sys.argv[3]
lines = p.read_text().splitlines(keepends=True)
out, inside = [], False
for line in lines:
    if start in line: inside = True; continue
    if end in line:   inside = False; continue
    if not inside: out.append(line)
p.write_text("".join(out))
PY
        ok "Removed PATH block from $(basename "$rc")"
    done

    section "Removing venv"
    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        ok "Removed: $VENV_DIR"
    else
        warn "Venv not found: $VENV_DIR"
    fi

    echo ""
    echo -e "${BOLD}  Done.${NC}"
    echo -e "  ${MUTED}~/.leafhub/ (API keys, DB) was NOT removed.${NC}"
    echo -e "  ${MUTED}To delete your stored keys: rm -rf ~/.leafhub/${NC}"
    echo ""
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  LeafHub — Setup${NC}"
echo -e "${MUTED}  Creates a Python virtual environment and registers the leafhub CLI.${NC}"
echo ""

# ── Step 1: Platform ──────────────────────────────────────────────────────────
section "Step 1 / 4  —  Platform"

OS="unknown"
if   [[ "$OSTYPE" == "darwin"* ]];                                    then OS="macos"
elif [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]];        then OS="wsl"
elif [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "linux"* ]];        then OS="linux"
fi

if [[ "$OS" == "unknown" ]]; then
    fail "Unsupported OS ($OSTYPE).  On Windows run: install.ps1"
fi
ok "Platform: $OS"

# ── Step 2: Python 3.11+ ──────────────────────────────────────────────────────
section "Step 2 / 4  —  Python"

PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3 python; do
    command -v "$cmd" >/dev/null 2>&1 || continue
    ver="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    [[ -z "$ver" ]] && continue
    maj="${ver%%.*}"; min="${ver##*.}"
    if [[ "$maj" -ge 3 && "$min" -ge 11 ]]; then PYTHON="$cmd"; break; fi
done

if [[ -z "$PYTHON" ]]; then
    fail "Python 3.11+ is required but was not found in PATH.
  macOS:  brew install python@3.12
  Ubuntu: sudo apt install python3.12 python3.12-venv"
fi
ok "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# ── Step 3: Venv + install ────────────────────────────────────────────────────
section "Step 3 / 4  —  Virtual environment"

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [[ -d "$VENV_DIR" ]]; then
    if [[ "$REINSTALL" == "true" ]]; then
        info "Removing existing .venv (--reinstall) ..."
        rm -rf "$VENV_DIR"
    elif [[ ! -x "$VENV_PYTHON" ]]; then
        warn "Existing .venv appears broken — recreating ..."
        rm -rf "$VENV_DIR"
    else
        ok ".venv exists — reusing  (pass --reinstall to force rebuild)"
    fi
fi

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating .venv with $PYTHON ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok ".venv created."
fi

info "Upgrading pip and setuptools ..."
"$VENV_PYTHON" -m pip install --upgrade pip setuptools --quiet

info "Installing leafhub[manage] ..."
"$VENV_PIP" install -e "$SCRIPT_DIR[manage]" --quiet
ok "Package installed."

# ── Step 4: CLI registration ──────────────────────────────────────────────────
section "Step 4 / 4  —  CLI registration"

LEAFHUB_BIN="$VENV_DIR/bin/leafhub"

if [[ ! -x "$LEAFHUB_BIN" ]]; then
    fail "Entry point not found after install: $LEAFHUB_BIN"
fi

if [[ "$DOCTOR" == "true" ]]; then
    info "Running environment check (--doctor) ..."
    "$VENV_PYTHON" "$SCRIPT_DIR/scripts/check_env.py"
    exit $?
fi

mkdir -p "$BIN_DIR"
ln -sf "$LEAFHUB_BIN" "$LEAFHUB_LINK"
ok "Linked: $LEAFHUB_LINK → $LEAFHUB_BIN"

PATH_PERSISTED=0
for rc in "${_RC_FILES[@]}"; do
    [[ -f "$rc" ]] || continue
    grep -qF '.local/bin' "$rc" 2>/dev/null && { PATH_PERSISTED=1; continue; }
    printf '\n%s\n%s\n%s\n' "$_MARKER" "$_PATH_LINE" "$_ENDMARK" >> "$rc"
    info "Added ~/.local/bin to PATH in $(basename "$rc")"
    PATH_PERSISTED=1
done
[[ "$PATH_PERSISTED" -eq 0 ]] && {
    printf '\n%s\n%s\n%s\n' "$_MARKER" "$_PATH_LINE" "$_ENDMARK" >> "$HOME/.bashrc"
    info "Created ~/.bashrc with PATH entry"
}

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Setup complete!${NC}"
echo ""
if [[ "$HEADLESS" == "true" ]]; then
    echo -e "  ${MUTED}Headless mode — CLI registered at $LEAFHUB_LINK${NC}"
else
    echo -e "  Activate the venv or use the global ${GREEN}leafhub${NC} command:"
    echo ""
    echo -e "    ${GREEN}leafhub provider add${NC}        # add an API key"
    echo -e "    ${GREEN}leafhub project create${NC}      # create a project"
    echo -e "    ${GREEN}leafhub manage${NC}              # start the Web UI (port 8765)"
    echo -e "    ${GREEN}leafhub --help${NC}              # full command reference"
fi
echo ""
echo -e "  ${MUTED}Data stored at: ~/.leafhub/${NC}"
echo -e "  ${MUTED}To uninstall:   ./setup.sh --uninstall${NC}"
echo ""

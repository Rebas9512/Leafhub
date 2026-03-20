#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — Installer  (macOS / Linux / WSL)
#
#  Run from the project root:
#    chmod +x install.sh && ./install.sh
#
#  Environment variables:
#    LEAFHUB_NO_SETUP=1   Skip the interactive first-run hint
#    NO_COLOR=1           Disable colour output
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
ORIGINAL_PATH="${PATH:-}"
PATH_PERSISTED=0

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

path_has_dir() { case ":${1}:" in *":${2%/}:"*) return 0 ;; *) return 1 ;; esac }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  LeafHub — Installer${NC}"
echo -e "${MUTED}  Project: $SCRIPT_DIR${NC}"
echo ""

# ── Step 1: OS ────────────────────────────────────────────────────────────────
section "Step 1 / 4  —  Platform"

OS="unknown"
if   [[ "$OSTYPE" == "darwin"* ]];                                    then OS="macos"
elif [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]];        then OS="wsl"
elif [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "linux"* ]];        then OS="linux"
fi

if [[ "$OS" == "unknown" ]]; then
    fail "Unsupported OS ($OSTYPE).  On Windows use: install.ps1"
fi
ok "Platform: $OS"

# ── Step 2: Python ────────────────────────────────────────────────────────────
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
    fail "Python 3.11+ not found.\n  macOS:  brew install python@3.12\n  Ubuntu: sudo apt install python3.12 python3.12-venv"
fi
ok "Python: $PYTHON ($("$PYTHON" -c 'import sys; print(sys.version.split()[0])'))"

# ── Step 3: Virtual environment + install ─────────────────────────────────────
section "Step 3 / 4  —  Virtual environment"

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [[ -x "$VENV_PYTHON" ]]; then
    ok "Venv exists — reusing."
else
    info "Creating .venv ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Venv created."
fi

info "Upgrading pip and setuptools ..."
"$VENV_PYTHON" -m pip install --upgrade pip setuptools --quiet

info "Installing leafhub[manage] ..."
"$VENV_PIP" install -e "$SCRIPT_DIR[manage]" --quiet
ok "Package installed."

# ── Step 4: CLI registration ──────────────────────────────────────────────────
section "Step 4 / 4  —  CLI registration"

LEAFHUB_BIN="$VENV_DIR/bin/leafhub"
LEAFHUB_LINK="$BIN_DIR/leafhub"

if [[ ! -x "$LEAFHUB_BIN" ]]; then
    fail "Entry point not found after install: $LEAFHUB_BIN"
fi

mkdir -p "$BIN_DIR"
ln -sf "$LEAFHUB_BIN" "$LEAFHUB_LINK"
ok "Linked: $LEAFHUB_LINK → $LEAFHUB_BIN"

# Persist ~/.local/bin in shell rc files
MARKER='# >>> leafhub PATH >>>'
ENDMARK='# <<< leafhub PATH <<<'
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

RC_FILES=("$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile")
for rc in "${RC_FILES[@]}"; do
    [[ -f "$rc" ]] || continue
    grep -qF '.local/bin' "$rc" 2>/dev/null && { PATH_PERSISTED=1; continue; }
    printf '\n%s\n%s\n%s\n' "$MARKER" "$PATH_LINE" "$ENDMARK" >> "$rc"
    info "Added ~/.local/bin to PATH in $(basename "$rc")"
    PATH_PERSISTED=1
done

if [[ "$PATH_PERSISTED" -eq 0 ]]; then
    # ~/.bashrc didn't exist — create it
    RC="$HOME/.bashrc"
    printf '\n%s\n%s\n%s\n' "$MARKER" "$PATH_LINE" "$ENDMARK" >> "$RC"
    info "Created ~/.bashrc with PATH entry"
    PATH_PERSISTED=1
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  LeafHub installed!${NC}"
echo ""

if path_has_dir "$ORIGINAL_PATH" "$BIN_DIR"; then
    echo -e "  ${GREEN}leafhub --help${NC}              # verify install"
    echo -e "  ${GREEN}leafhub provider add${NC}        # add an API key"
    echo -e "  ${GREEN}leafhub project create${NC}      # create a project"
    echo -e "  ${GREEN}leafhub manage${NC}              # start the Web UI"
else
    echo "  Open a new terminal (or run: source ~/.bashrc), then:"
    echo -e "    ${GREEN}leafhub --help${NC}"
fi
echo ""
echo -e "  ${MUTED}Data stored at: ~/.leafhub/${NC}"
echo -e "  ${MUTED}To uninstall:   ./install.sh --uninstall${NC}"
echo ""

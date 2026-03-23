#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
#  LeafHub — One-liner Installer  (macOS / Linux / WSL)
#
#  curl -fsSL https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.sh | bash
#
#  Environment variables:
#    LEAFHUB_DIR=<path>      Install directory  (default: ~/leafhub)
#    LEAFHUB_REPO_URL=<url>  Clone URL          (default: GitHub repo)
#    NO_COLOR=1              Disable colour output
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DEFAULT_INSTALL_DIR="$HOME/leafhub"
LEAFHUB_DIR="${LEAFHUB_DIR:-}"
REPO_URL="${LEAFHUB_REPO_URL:-https://github.com/Rebas9512/Leafhub.git}"
BIN_DIR="$HOME/.local/bin"
ORIGINAL_PATH="${PATH:-}"
PATH_PERSISTED=0
VENV_DIR=""
VENV_PYTHON=""
VENV_PIP=""
LEAFHUB_BIN=""
LEAFHUB_LINK="$BIN_DIR/leafhub"

# ── Colours ───────────────────────────────────────────────────────────────────
if [[ -n "${NO_COLOR:-}" || "${TERM:-dumb}" == "dumb" ]]; then
    BOLD='' GREEN='' YELLOW='' RED='' MUTED='' NC=''
else
    BOLD='\033[1m'
    GREEN='\033[38;2;0;229;180m'
    YELLOW='\033[38;2;255;176;32m'
    RED='\033[38;2;230;57;70m'
    MUTED='\033[38;2;110;120;148m'
    NC='\033[0m'
fi

ok()      { echo -e "${GREEN}✓${NC}  $*"; }
info()    { echo -e "${MUTED}·${NC}  $*"; }
warn()    { echo -e "${YELLOW}!${NC}  $*"; }
fail()    { echo -e "${RED}✗${NC}  $*" >&2; exit 1; }
section() { echo ""; echo -e "${BOLD}── $* ──${NC}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
normalise_path() {
    local raw="${1:-}"
    local expanded="${raw/#\~/$HOME}"
    if [[ -n "$expanded" && "$expanded" != /* ]]; then
        expanded="$(pwd -P)/$expanded"
    fi
    while [[ "${expanded}" != "/" && "${expanded}" == */ ]]; do
        expanded="${expanded%/}"
    done
    printf '%s' "$expanded"
}

dir_has_entries() {
    local dir="$1"
    local entry
    for entry in "$dir"/.[!.]* "$dir"/..?* "$dir"/*; do
        [[ -e "$entry" ]] && return 0
    done
    return 1
}

path_has_dir() {
    case ":${1}:" in *":${2%/}:"*) return 0 ;; *) return 1 ;; esac
}

# ── Select install directory ──────────────────────────────────────────────────
if [[ -z "$LEAFHUB_DIR" ]]; then
    default_dir="$(normalise_path "$DEFAULT_INSTALL_DIR")"
    if [[ -r /dev/tty && -w /dev/tty && -z "${CI:-}" ]]; then
        printf 'Install directory [%s]: ' "$default_dir" > /dev/tty
        if IFS= read -r candidate < /dev/tty; then
            candidate="${candidate:-$default_dir}"
        else
            candidate="$default_dir"
        fi
        LEAFHUB_DIR="$(normalise_path "$candidate")"
    else
        LEAFHUB_DIR="$(normalise_path "$DEFAULT_INSTALL_DIR")"
    fi
fi

# If the target exists and is non-empty but not a git repo, redirect into a
# subdirectory so we don't clobber the user's existing files.
if [[ ! -d "$LEAFHUB_DIR/.git" ]] && \
   [[ -d "$LEAFHUB_DIR" ]] && dir_has_entries "$LEAFHUB_DIR"; then
    LEAFHUB_DIR="$(normalise_path "$LEAFHUB_DIR/leafhub")"
fi

VENV_DIR="$LEAFHUB_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
LEAFHUB_BIN="$VENV_DIR/bin/leafhub"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  LeafHub — Installer${NC}"
echo -e "${MUTED}  Install path: $LEAFHUB_DIR${NC}"
echo ""

# ── Step 1: Platform ──────────────────────────────────────────────────────────
section "Platform"

OS="unknown"
if   [[ "$OSTYPE" == "darwin"* ]];                                    then OS="macos"
elif [[ -n "${WSL_DISTRO_NAME:-}" || -n "${WSL_INTEROP:-}" ]];        then OS="wsl"
elif [[ "$OSTYPE" == "linux-gnu"* || "$OSTYPE" == "linux"* ]];        then OS="linux"
fi

if [[ "$OS" == "unknown" ]]; then
    fail "Unsupported OS ($OSTYPE).\nOn Windows use: irm https://raw.githubusercontent.com/Rebas9512/Leafhub/main/install.ps1 | iex"
fi
ok "Platform: $OS"

# ── Step 2: Python 3.11+ ──────────────────────────────────────────────────────
section "Python"

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

command -v git >/dev/null 2>&1 || fail "git is required but not found."

# ── Step 3: Clone / update ────────────────────────────────────────────────────
section "Installing LeafHub"

if [[ -d "$LEAFHUB_DIR/.git" ]]; then
    info "Existing installation found — syncing to latest..."
    git -C "$LEAFHUB_DIR" fetch origin --quiet
    branch="$(git -C "$LEAFHUB_DIR" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|.*/||')"
    [[ -z "$branch" ]] && branch="main"
    git -C "$LEAFHUB_DIR" reset --hard "origin/$branch" --quiet
    ok "Updated to latest ($branch)."
else
    [[ -e "$LEAFHUB_DIR" ]] && { info "Removing stale directory $LEAFHUB_DIR ..."; rm -rf "$LEAFHUB_DIR"; }
    info "Cloning into $LEAFHUB_DIR ..."
    git clone --depth=1 "$REPO_URL" "$LEAFHUB_DIR" --quiet
    ok "Cloned."
fi

# ── Step 4: Virtual environment + install ─────────────────────────────────────
section "Virtual environment"

if [[ ! -x "$VENV_PYTHON" ]]; then
    info "Creating .venv ..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Venv created."
else
    ok "Venv exists — reusing."
fi

info "Upgrading pip and setuptools ..."
"$VENV_PYTHON" -m pip install --upgrade pip setuptools --quiet

info "Installing leafhub[manage] ..."
"$VENV_PIP" install -e "$LEAFHUB_DIR[manage]" --quiet
ok "Package installed."

# ── Step 5: CLI registration ──────────────────────────────────────────────────
section "PATH"

if [[ ! -x "$LEAFHUB_BIN" ]]; then
    fail "Entry point not found after install: $LEAFHUB_BIN"
fi

mkdir -p "$BIN_DIR"
ln -sf "$LEAFHUB_BIN" "$LEAFHUB_LINK"
ok "Linked: $LEAFHUB_LINK → $LEAFHUB_BIN"

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
    printf '\n%s\n%s\n%s\n' "$MARKER" "$PATH_LINE" "$ENDMARK" >> "$HOME/.bashrc"
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
echo -e "  ${MUTED}Install dir:  $LEAFHUB_DIR${NC}"
echo -e "  ${MUTED}Data stored:  ~/.leafhub/${NC}"
echo -e "  ${MUTED}To update:    git -C \"$LEAFHUB_DIR\" pull${NC}"
echo -e "  ${MUTED}To uninstall: rm \"$LEAFHUB_LINK\" && rm -rf \"$LEAFHUB_DIR\"${NC}"
echo ""

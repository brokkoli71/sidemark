#!/bin/bash
# install.sh — user-local install for Sidemark
# Usage:  ./install.sh                 install
#         ./install.sh -y              install, auto-confirm dependency prompt
#         ./install.sh --walker-menu   also install the walker/elephant recent-files menu
#         ./install.sh --register-pptx also register as default handler for PowerPoint files
#         ./install.sh --uninstall     remove everything
set -euo pipefail

_YES=0
_WALKER=0
_PPTX=0
for _arg in "$@"; do
    case "$_arg" in
        -y|--yes) _YES=1 ;;
        --walker-menu) _WALKER=1 ;;
        --register-pptx) _PPTX=1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/sidemark"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"
DESKTOP_ID="de.hspitz.sidemark"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; exit 1; }
step() { echo -e "\n${BOLD}$1${NC}"; }

# ── uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    step "Uninstalling Sidemark…"
    rm -rf  "$INSTALL_DIR"
    rm -f   "$BIN_DIR/sidemark"
    rm -f   "$DESKTOP_DIR/$DESKTOP_ID.desktop"
    rm -f   "$HOME/.config/elephant/menus/sidemark_recent.lua"
    rm -f   "$ICON_BASE/scalable/apps/$DESKTOP_ID.svg"
    for size in 16 32 48 64 128 256; do
        rm -f "$ICON_BASE/${size}x${size}/apps/$DESKTOP_ID.png"
    done
    gtk-update-icon-cache  -f -t "$ICON_BASE"  2>/dev/null || true
    update-desktop-database    "$DESKTOP_DIR"  2>/dev/null || true
    ok "Uninstalled."
    exit 0
fi

# ── distro detection ──────────────────────────────────────────────────────────
_DISTRO="unknown"
if   command -v pacman &>/dev/null; then _DISTRO="arch"
elif command -v apt    &>/dev/null; then _DISTRO="deb"
elif command -v dnf    &>/dev/null; then _DISTRO="rpm"
fi

# Print the right install hint for the detected distro.
# Args: arch-hint  deb-hint  rpm-hint
_hint() {
    case "$_DISTRO" in
        arch) echo "sudo pacman -S $1" ;;
        deb)  echo "sudo apt install $2" ;;
        rpm)  echo "sudo dnf install $3" ;;
        *)    echo "install: arch=$1  deb=$2  rpm=$3" ;;
    esac
}

# ── dependency check ───────────────────────────────────────────────────────────
step "Checking dependencies…"

_MISS_ARCH=(); _MISS_DEB=(); _MISS_RPM=(); _MISS_PIP=()

# _need ARCH_PKGS DEB_PKGS RPM_PKGS [pip=PKG]
_need() {
    local arch="$1" deb="$2" rpm="$3" pip="${4:-}"
    local display="${arch:-${pip}}"
    read -ra _a <<< "$arch"; _MISS_ARCH+=("${_a[@]}")
    read -ra _d <<< "$deb";  _MISS_DEB+=("${_d[@]}")
    read -ra _r <<< "$rpm";  _MISS_RPM+=("${_r[@]}")
    [[ -n "$pip" ]] && _MISS_PIP+=("$pip")
    echo -e "  ${RED}✗${NC} Missing: $display"
}

# check_py TEST ARCH_PKGS DEB_PKGS RPM_PKGS [pip=PKG]
check_py() {
    /usr/bin/python3 -c "$1" 2>/dev/null || _need "$2" "$3" "$4" "${5:-}"
}

if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found  →  $(_hint python python3 python3)"
fi

check_py "import gi" \
    "python-gobject" "python3-gi python3-gi-cairo" "python3-gobject"
check_py "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" \
    "gtk4" "gir1.2-gtk-4.0 libgtk-4-1" "gtk4"
check_py "import gi; gi.require_version('Adw','1'); from gi.repository import Adw" \
    "libadwaita" "gir1.2-adw-1 libadwaita-1-0" "libadwaita"
# PyMuPDF: Arch = pacman, others = pip
if ! /usr/bin/python3 -c "import fitz" 2>/dev/null; then
    case "$_DISTRO" in
        arch) _need "python-pymupdf" "" "" ;;
        *)    _need "" "" "" "pymupdf" ;;
    esac
fi
check_py "import numpy" \
    "python-numpy" "python3-numpy" "python3-numpy"
check_py "import cairo" \
    "python-cairo" "python3-gi-cairo" "python3-cairo"
check_py "import gi; gi.require_version('GtkSource','5'); from gi.repository import GtkSource" \
    "gtksourceview5" "gir1.2-gtksource-5 libgtksourceview-5-0" "gtksourceview5"

if ! find /usr/share/icons/Adwaita -name "go-next-symbolic*" 2>/dev/null | grep -q .; then
    warn "adwaita-icon-theme not found — icons may be missing."
    _need "adwaita-icon-theme" "adwaita-icon-theme" "adwaita-icon-theme"
fi

# ── auto-install missing packages ─────────────────────────────────────────────
_has_missing() {
    [[ ${#_MISS_ARCH[@]} -gt 0 || ${#_MISS_DEB[@]} -gt 0 || \
       ${#_MISS_RPM[@]} -gt 0  || ${#_MISS_PIP[@]} -gt 0 ]]
}

if _has_missing; then
    echo ""
    if [[ $_YES -eq 1 ]]; then
        _ans="Y"
    else
        read -rp "  Install missing packages automatically? [Y/n] " _ans
    fi
    if [[ "${_ans:-Y}" =~ ^[Yy]$ ]]; then
        case "$_DISTRO" in
            arch)
                [[ ${#_MISS_ARCH[@]} -gt 0 ]] && sudo pacman -S --needed --noconfirm "${_MISS_ARCH[@]}"
                ;;
            deb)
                [[ ${#_MISS_DEB[@]} -gt 0 ]] && sudo apt-get install -y "${_MISS_DEB[@]}"
                if [[ ${#_MISS_PIP[@]} -gt 0 ]]; then
                    /usr/bin/python3 -m pip --version &>/dev/null || sudo apt-get install -y python3-pip
                    /usr/bin/python3 -m pip install --user --break-system-packages "${_MISS_PIP[@]}"
                fi
                ;;
            rpm)
                [[ ${#_MISS_RPM[@]} -gt 0 ]] && sudo dnf install -y "${_MISS_RPM[@]}"
                if [[ ${#_MISS_PIP[@]} -gt 0 ]]; then
                    /usr/bin/python3 -m pip --version &>/dev/null || sudo dnf install -y python3-pip
                    /usr/bin/python3 -m pip install --user --break-system-packages "${_MISS_PIP[@]}"
                fi
                ;;
            *)
                [[ ${#_MISS_PIP[@]} -gt 0 ]] && /usr/bin/python3 -m pip install --user --break-system-packages "${_MISS_PIP[@]}"
                ;;
        esac
        # Re-verify after install
        step "Re-checking dependencies…"
        _MISS_ARCH=(); _MISS_DEB=(); _MISS_RPM=(); _MISS_PIP=()
        check_py "import gi" \
            "python-gobject" "python3-gi python3-gi-cairo" "python3-gobject"
        check_py "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" \
            "gtk4" "gir1.2-gtk-4.0 libgtk-4-1" "gtk4"
        check_py "import gi; gi.require_version('Adw','1'); from gi.repository import Adw" \
            "libadwaita" "gir1.2-adw-1 libadwaita-1-0" "libadwaita"
        if ! /usr/bin/python3 -c "import fitz" 2>/dev/null; then
            case "$_DISTRO" in
                arch) _need "python-pymupdf" "" "" ;;
                *)    _need "" "" "" "pymupdf" ;;
            esac
        fi
        check_py "import numpy" \
            "python-numpy" "python3-numpy" "python3-numpy"
        check_py "import cairo" \
            "python-cairo" "python3-gi-cairo" "python3-cairo"
        check_py "import gi; gi.require_version('GtkSource','5'); from gi.repository import GtkSource" \
            "gtksourceview5" "gir1.2-gtksource-5 libgtksourceview-5-0" "gtksourceview5"
        _has_missing && fail "Some dependencies still missing after install."
    else
        fail "Aborted — install missing packages first."
    fi
fi

ok "All required dependencies present."

if command -v rsvg-convert >/dev/null 2>&1; then
    HAVE_RSVG=1; ok "rsvg-convert found — will render PNG icons."
else
    HAVE_RSVG=0; warn "rsvg-convert not found — only SVG icon will be installed."
fi

# ── install ────────────────────────────────────────────────────────────────────
step "Installing…"

mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR"
mkdir -p "$ICON_BASE/scalable/apps"
for size in 16 32 48 64 128 256; do
    mkdir -p "$ICON_BASE/${size}x${size}/apps"
done

# Main script
install -m 755 "$SCRIPT_DIR/sidemark.py" "$INSTALL_DIR/sidemark.py"
ok "sidemark.py  →  $INSTALL_DIR/"

# Wrapper so 'sidemark' works from any shell / Exec line
cat > "$BIN_DIR/sidemark" <<EOF
#!/bin/sh
exec /usr/bin/python3 "$INSTALL_DIR/sidemark.py" "\$@"
EOF
chmod 755 "$BIN_DIR/sidemark"
ok "wrapper        →  $BIN_DIR/sidemark"

# Desktop entry
install -m 644 "$SCRIPT_DIR/de.hspitz.sidemark.desktop" \
    "$DESKTOP_DIR/$DESKTOP_ID.desktop"
ok ".desktop file  →  $DESKTOP_DIR/"

# Default handler
xdg-mime default "$DESKTOP_ID.desktop" application/pdf          2>/dev/null || true
xdg-mime default "$DESKTOP_ID.desktop" text/markdown            2>/dev/null || true
xdg-mime default "$DESKTOP_ID.desktop" text/x-markdown          2>/dev/null || true
ok "registered as default for PDF and Markdown."

# PowerPoint opens via LibreOffice conversion — opt-in, since most users want
# an office suite as their .pptx default.
if [[ $_PPTX -eq 1 ]]; then
    xdg-mime default "$DESKTOP_ID.desktop" application/vnd.ms-powerpoint 2>/dev/null || true
    xdg-mime default "$DESKTOP_ID.desktop" \
        application/vnd.openxmlformats-officedocument.presentationml.presentation 2>/dev/null || true
    ok "registered as default for PowerPoint."
fi

# Icons
install -m 644 "$SCRIPT_DIR/icon.svg" \
    "$ICON_BASE/scalable/apps/$DESKTOP_ID.svg"
if [[ $HAVE_RSVG -eq 1 ]]; then
    for size in 16 32 48 64 128 256; do
        rsvg-convert "$SCRIPT_DIR/icon.svg" -w "$size" -h "$size" \
            -o "$ICON_BASE/${size}x${size}/apps/$DESKTOP_ID.png"
    done
    ok "icons (SVG + PNG)  →  $ICON_BASE/"
else
    ok "icon (SVG only)    →  $ICON_BASE/"
fi

# Walker/elephant launcher menu (Omarchy): recent files in the launcher.
# Opt-in via --walker-menu — not every user wants entries in their launcher.
if [[ $_WALKER -eq 1 ]]; then
    ELEPHANT_MENUS="$HOME/.config/elephant/menus"
    if [[ -d "$ELEPHANT_MENUS" ]] && command -v jq &>/dev/null; then
        install -m 644 "$SCRIPT_DIR/extras/sidemark_recent.lua" \
            "$ELEPHANT_MENUS/sidemark_recent.lua"
        systemctl --user try-restart elephant 2>/dev/null || true
        ok "walker menu    →  $ELEPHANT_MENUS/sidemark_recent.lua"
    else
        warn "walker menu skipped: needs ~/.config/elephant/menus and jq"
    fi
fi

# Refresh caches
gtk-update-icon-cache  -f -t "$ICON_BASE"  2>/dev/null || true
update-desktop-database    "$DESKTOP_DIR"  2>/dev/null || true
ok "icon and desktop caches refreshed."

# PATH hint
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    warn "$HOME/.local/bin is not in your PATH."
    warn "Add to ~/.bashrc or ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo -e "\n${GREEN}${BOLD}Done!${NC}"
echo "  Launch:    sidemark [file.pdf]"
echo "  Uninstall: ./install.sh --uninstall"

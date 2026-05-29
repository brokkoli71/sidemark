#!/bin/bash
# install.sh — user-local install for PDF Editor
# Usage:  ./install.sh            install
#         ./install.sh --uninstall remove everything
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/pdf-editor-omarchy"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"
DESKTOP_ID="de.hspitz.pdfeditor"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; exit 1; }
step() { echo -e "\n${BOLD}$1${NC}"; }

# ── uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    step "Uninstalling PDF Editor…"
    rm -rf  "$INSTALL_DIR"
    rm -f   "$BIN_DIR/pdfeditor"
    rm -f   "$DESKTOP_DIR/$DESKTOP_ID.desktop"
    rm -f   "$ICON_BASE/scalable/apps/$DESKTOP_ID.svg"
    for size in 16 32 48 64 128 256; do
        rm -f "$ICON_BASE/${size}x${size}/apps/$DESKTOP_ID.png"
    done
    gtk-update-icon-cache  -f -t "$ICON_BASE"  2>/dev/null || true
    update-desktop-database    "$DESKTOP_DIR"  2>/dev/null || true
    ok "Uninstalled."
    exit 0
fi

# ── dependency check ───────────────────────────────────────────────────────────
step "Checking dependencies…"

check_py() {
    python3 -c "$1" 2>/dev/null || \
        fail "Missing: $2  →  sudo pacman -S $3"
}

command -v python3 >/dev/null 2>&1 || \
    fail "python3 not found  →  sudo pacman -S python"

check_py "import gi" \
    "python-gobject" "python-gobject"
check_py "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" \
    "gtk4" "gtk4"
check_py "import gi; gi.require_version('Adw','1'); from gi.repository import Adw" \
    "libadwaita" "libadwaita"
check_py "import gi; gi.require_version('Poppler','0.18'); from gi.repository import Poppler" \
    "poppler-glib" "poppler-glib"
check_py "import cairo" \
    "python-cairo" "python-cairo"
check_py "import gi; gi.require_version('GtkSource','5'); from gi.repository import GtkSource" \
    "gtksourceview5" "gtksourceview5"

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
install -m 755 "$SCRIPT_DIR/pdfeditor.py" "$INSTALL_DIR/pdfeditor.py"
ok "pdfeditor.py  →  $INSTALL_DIR/"

# Wrapper so 'pdfeditor' works from any shell / Exec line
cat > "$BIN_DIR/pdfeditor" <<EOF
#!/bin/sh
exec /usr/bin/python3 "$INSTALL_DIR/pdfeditor.py" "\$@"
EOF
chmod 755 "$BIN_DIR/pdfeditor"
ok "wrapper        →  $BIN_DIR/pdfeditor"

# Desktop entry
install -m 644 "$SCRIPT_DIR/de.hspitz.pdfeditor.desktop" \
    "$DESKTOP_DIR/$DESKTOP_ID.desktop"
ok ".desktop file  →  $DESKTOP_DIR/"

# Default handler
xdg-mime default "$DESKTOP_ID.desktop" application/pdf          2>/dev/null || true
xdg-mime default "$DESKTOP_ID.desktop" text/markdown            2>/dev/null || true
xdg-mime default "$DESKTOP_ID.desktop" text/x-markdown          2>/dev/null || true
ok "registered as default for PDF and Markdown."

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
echo "  Launch:    pdfeditor [file.pdf]"
echo "  Uninstall: ./install.sh --uninstall"

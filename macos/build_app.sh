#!/usr/bin/env bash
# build_app.sh — build a macOS .app bundle for Sidemark using PyInstaller.
#
# SPIKE: This is a starting point and will likely need iteration.
# Known gaps:
#   - GLib/GTK typelib files must be bundled manually (see TODOs below).
#   - The Quartz GTK backend requires specific GDK_BACKEND=quartz env var.
#   - Icons, .desktop-equivalent Info.plist entries need manual tweaking.
#
# Usage:
#   cd <repo-root>
#   bash macos/build_app.sh
#
# Requirements:
#   brew install pygobject3 gtk4 libadwaita gtksourceview5
#   pip3 install pyinstaller pymupdf

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "==> Installing PyInstaller and pymupdf..."
pip3 install --quiet pyinstaller pymupdf

echo "==> Running PyInstaller..."
cd "$REPO_ROOT"
pyinstaller \
    --windowed \
    --name Sidemark \
    --osx-bundle-identifier de.hspitz.sidemark \
    --icon icon.svg \
    sidemark.py

# TODO: Copy GLib typelib files so GObject introspection works inside the bundle.
# These live under $(brew --prefix)/lib/girepository-1.0/ and must be placed at
# Contents/Resources/lib/girepository-1.0/ inside the .app.
# Example:
#   GIR_DIR="$(brew --prefix)/lib/girepository-1.0"
#   BUNDLE_GIR="dist/Sidemark.app/Contents/Resources/lib/girepository-1.0"
#   mkdir -p "$BUNDLE_GIR"
#   cp "$GIR_DIR"/{Gtk-4.0,Adw-1,GtkSource-5,GLib-2.0,GObject-2.0,Gdk-4.0}.typelib \
#      "$BUNDLE_GIR/"

# TODO: Bundle GTK data dirs (themes, icons) for full theme support.
# GLib schemas and icon caches are also required for a fully self-contained app.

echo ""
echo "==> Build complete (if no errors above)."
echo "    App bundle: dist/Sidemark.app"
echo ""
echo "NOTE: The bundle may not run without further typelib/data-dir tweaks."
echo "      See TODO comments in this script for next steps."

# Sidemark

[![AUR version](https://img.shields.io/aur/version/sidemark-git)](https://aur.archlinux.org/packages/sidemark-git)
[![CI](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml/badge.svg)](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml)

Sidemark is a lightweight PDF annotator for Linux with a live Markdown notes panel. Open a PDF — lecture slides, papers, or any document — draw directly on it, and write structured notes beside it.

![Screenshot](screenshot.png)

## Features

- **Draw annotations** with a configurable pen — strokes are saved as PDF ink annotations and remain individually erasable by right-click-dragging
- **Highlighter mode** (`Ctrl+H`) — wide translucent strokes that survive save/reload like any other annotation
- **Live markdown notes** linked to PDF pages, with anchor markers to pin notes to specific spots
- **Quick page navigation** via drag to pan, Shift+drag to easily Zoom to region and Shift+click to zoom back
- **Add and delete pages** — insert blank pages with same dimensions
- **Text selection** — Alt+drag highlights words and copies them to the clipboard, or switch to select-text mode (Ctrl+M) so a plain drag selects instead of drawing
- **Text search** — Ctrl+F opens a search bar that spans both the PDF and your Markdown notes; matches highlight across all pages, and Enter / ↑↓ cycle through them (starting at the current page and wrapping around)
- **Outline sidebar** — Ctrl+T shows the PDF's table of contents; click to jump to a chapter. A switcher flips between the outline and page thumbnails; PDFs without an outline show thumbnails directly. Drag a thumbnail onto another to reorder pages
- **Formats** — Opens `.pdf`, `.pptx` (auto-converts via LibreOffice), and `.md` files; drag a file from your file manager onto the window to open it
- **Recent files** — header menu, XDG recent-files integration, and a walker (Omarchy) launcher menu
- **Design Scheme** — Picks up accent color and dark/light mode from Omarchy, GNOME, or KDE automatically

## Installation

### AUR (Arch Linux / Omarchy)

```bash
yay -S sidemark-git
```

### install.sh (any Linux)

```bash
git clone https://github.com/brokkoli71/sidemark
cd sidemark
./install.sh
```

Installs the app, creates a launcher entry, and registers it as the default handler for PDF and Markdown files. Optional flags: `--walker-menu` (launcher recent-files menu, see below) and `--register-pptx` (also become the default handler for PowerPoint files, which open via LibreOffice conversion).

```bash
./install.sh --uninstall
```

### Run directly (no install)

```bash
git clone https://github.com/brokkoli71/sidemark
cd sidemark
python sidemark.py [file.pdf]
# Add -v / --verbose for debug logging
```

**Dependencies:**

Arch / EndeavourOS:
```bash
sudo pacman -S python python-gobject gtk4 libadwaita python-pymupdf python-numpy python-cairo gtksourceview5
```

Ubuntu / Debian:
```bash
sudo apt install python3 python3-gi python3-gi-cairo python3-numpy \
  gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 \
  libgtk-4-1 libadwaita-1-0 libgtksourceview-5-0
pip install pymupdf
```

## Shortcuts

### Annotation

| Input | Action |
|-------|--------|
| Left-drag | Draw stroke |
| Right-drag | Erase stroke (including from previous sessions) |
| `Ctrl+H` | Toggle highlighter — wide translucent strokes, own color/width in pen settings |
| `Ctrl+Z` | Undo the last action chronologically — a stroke, an erase drag, or a burst of notes typing — no matter where the cursor is |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo the last undone action |
| `Ctrl+M` | Toggle draw / select-text mode — in select mode a plain left-drag highlights text instead of drawing (text cursor shows the mode) |
| `Alt+drag` | Select & copy text (word-level highlight) — works in either mode |

### Pages

| Key | Action |
|-----|--------|
| `PageDown` | Next page (keeps zoom when zoomed, like mouse buttons 8/9) |
| `PageUp` | Previous page (keeps zoom when zoomed) |
| `Ctrl+Shift+N` | Add blank page after current |
| `Ctrl+Shift+Delete` | Delete current page |
| `Ctrl+T` | Toggle outline / page-thumbnail sidebar (Outline ⇄ Pages switcher when the PDF has both) |
| Drag thumbnail → thumbnail | Reorder pages (in the page-thumbnail sidebar) |

### Zoom & pan

| Input | Action |
|-------|--------|
| Scroll | Pan |
| Scroll past page edge | Flip to next / previous page (keeps zoom) |
| `Ctrl+scroll` | Zoom in/out (cursor-anchored) |
| Pinch (two-finger) | Zoom in/out (anchored on the pinch center) |
| `Ctrl+drag` / Middle-drag | Pan |
| Mouse thumb button (hold) | Pan by moving the mouse; scroll while holding to zoom |
| `Shift+drag` | Zoom to region |
| `Shift+click` | Fit page |

### Notes

| Key | Action |
|-----|--------|
| `Ctrl+B` | Bold selection |
| `Ctrl+I` | Italic selection |
| `Ctrl+E` | Inline code selection |
| `Ctrl+D` | Duplicate the current line (or every line the selection spans) |
| `Alt+↑` / `Alt+↓` | Move the current line (or selected lines) up / down |
| `/date` `/time` `/now` | Type the snippet then Space/Enter — expands to today's date, the time, or both |
| `Ctrl+\` | Toggle notes panel |
| `Ctrl+Alt+click` | Place a numbered anchor marker on the PDF at the cursor position in notes |
| `Ctrl+Alt+drag` | Place an anchor **and** a callout box at the drag end — the anchor's note paragraph is rendered on the PDF with an arrow pointing from the anchor |

### Inline math (notes)

Rendered on non-cursor lines; raw syntax restored when you move the cursor back to edit.

| Syntax | Renders as |
|--------|-----------|
| `x^2` or `x^{n+1}` | superscript (until next space, or braced) |
| `x_ij` or `x_{i,j}` | subscript (until next space, or braced) |
| `\alpha` `\beta` … `\omega` | Greek letters (α β … ω) |
| `\sum` `\prod` `\int` | Σ Π ∫ |
| `\infty` `\approx` `\neq` `\leq` `\geq` | ∞ ≈ ≠ ≤ ≥ |
| `\in` `\notin` `\subset` `\cup` `\cap` `\emptyset` | ∈ ∉ ⊂ ∪ ∩ ∅ |
| `\forall` `\exists` `\partial` `\nabla` `\to` | ∀ ∃ ∂ ∇ → |

Stored as plain text in the `.md` sidecar — renders cleanly in Obsidian and any Markdown viewer.

### Search

| Key | Action |
|-----|--------|
| `Ctrl+F` | Open search bar (searches the PDF text **and** the Markdown notes) |
| `Enter` / `↓` | Next match |
| `↑` | Previous match |
| `Escape` | Close search |

### File

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open file |
| `Ctrl+N` | New blank PDF |
| `Ctrl+S` | Save (prompts for name if untitled) |

## Tested distributions

| Distro | Unit tests | Install |
|--------|-----------|---------|
| Arch Linux | ✓ | ✓ CI |
| Ubuntu 24.04 | ✓ CI | ✓ CI |
| Fedora 41 | | ✓ CI |

"✓ CI" = verified on every push via GitHub Actions. Arch unit tests run locally (Omarchy is the primary development environment).

## Autosave

While there are unsaved changes, Sidemark snapshots the document and notes every 60 seconds to `~/.local/state/sidemark/autosave/` — the original file is never modified until you explicitly save. If Sidemark closes uncleanly, reopening the file offers to recover the snapshot. Snapshots are removed on save or discard, and pruned after 30 days.

## Recent files

Opened and saved files are tracked in `~/.local/share/sidemark/recent.json` (newest first, 15 entries) and exposed three ways:

- **In-app** — the clock-arrow button next to *Open* lists them.
- **XDG recent files** — opens are registered in `recently-used.xbel`, so GTK/GNOME file dialogs and KDE (KF6 `KRecentDocument`, hence krunner's recent-documents results) pick them up automatically.
- **walker / Omarchy launcher** (opt-in) — `./install.sh --walker-menu` drops `extras/sidemark_recent.lua` into `~/.config/elephant/menus/` (needs `jq`). Reach it via walker's provider list (`/` by default), or bind a prefix in `~/.config/walker/config.toml`:

  ```toml
  [[providers.prefixes]]
  prefix = "p:"
  provider = "menus:sidemarkrecent"
  ```

For other launchers (rofi, fuzzel, …) `sidemark --list-recent` prints `name<TAB>path` lines and exits without loading GTK.

## Notes format

Notes are saved alongside the PDF as `<filename>-notes.md` using invisible `<!-- page:N -->` markers, so the file renders cleanly in any Markdown viewer or Obsidian vault. Anchor markers (`<!-- anchor:X:Y -->`) and callout markers (`<!-- callout:X:Y -->`) are stored the same way — invisible in external viewers. Inside Sidemark, anchors appear as numbered circles on the PDF canvas; a callout additionally renders its anchor's note paragraph in a box at the callout position, with an arrow from the anchor. Callouts are included in Ctrl+E exports.

# Sidemark

[![AUR version](https://img.shields.io/aur/version/sidemark-git)](https://aur.archlinux.org/packages/sidemark-git)
[![CI](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml/badge.svg)](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml)

Sidemark is a lightweight PDF annotator for Linux with a live Markdown notes panel. Open a PDF — lecture slides, papers, or any document — draw directly on it, and write structured notes beside it.

![Screenshot](screenshot.png)

## Why Sidemark

Most PDF tools treat notes as an afterthought. Sidemark is built around them:

- **Page-linked Markdown notes** — a full Markdown editor sits beside the PDF; notes are automatically scoped to whichever page you're on and scroll with it
- **Anchor markers & callouts** — pin a note to a precise spot on the page; callouts render the note text directly on the PDF with an arrow, so context stays visible even without the notes panel open
- **Portable plain-text notes** — notes are saved as a standard `.md` sidecar file, Obsidian-compatible and readable anywhere, with no proprietary format
- **Unified search** — `Ctrl+F` searches the PDF text and your Markdown notes in one pass

## Features

### Annotations

- **Draw** with a configurable pen — strokes are saved as native PDF ink annotations and are individually erasable by right-click-dragging
- **Highlighter** (`Ctrl+H`) — wide translucent strokes with their own color and width setting, preserved across save/reload like any annotation
- **Undo / redo** (`Ctrl+Z` / `Ctrl+Y`) — works across both the canvas and notes; undo a stroke, an erase, or a burst of typing in the order you made them

### Notes

- **Live Markdown** with syntax highlighting, inline math (`x^2`, `\alpha`, `\sum` …), and formatting shortcuts (`Ctrl+B`, `Ctrl+I`, `Ctrl+E`)
- **Anchor markers** (`Ctrl+Alt+click`) — numbered circles placed on the PDF that link to the corresponding paragraph in your notes
- **Callout boxes** (`Ctrl+Alt+drag`) — anchor plus a box rendered on the PDF at the drag endpoint, with an arrow from the anchor; included in exports
- **Date / time snippets** — type `/date`, `/time`, or `/now` then Space to expand

### Navigation

- **Pan & zoom** — scroll to pan, `Ctrl+scroll` or pinch to zoom (centered on the cursor), `Shift+drag` to zoom to region, `Shift+click` to fit page
- **Page flip** — `PageDown` / `PageUp` or mouse thumb buttons; scrolling past a page edge flips automatically
- **Outline & thumbnails** — `Ctrl+T` toggles a sidebar between the PDF's table of contents and page thumbnails; drag a thumbnail to reorder pages
- **Add / delete pages** — insert blank pages with the same dimensions as the current page

### Files & integration

- **Formats** — opens `.pdf`, `.pptx` (auto-converted via LibreOffice), and `.md` files; drag a file from your file manager onto the window
- **Recent files** — in-app menu, XDG recent-files integration (GTK / GNOME / KDE file dialogs), and an optional walker / Omarchy launcher menu
- **Text selection** — `Alt+drag` selects words and copies to clipboard; `Ctrl+M` switches the primary drag to select mode
- **Design scheme** — inherits accent color and dark / light mode from Omarchy, GNOME, or KDE automatically

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
| `Ctrl+Z` | Undo the last action — a stroke, an erase, or a burst of typing — works across drawing and notes regardless of where the cursor is |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo the last undone action |
| `Ctrl+M` | Toggle draw / select-text mode — in select mode a plain left-drag highlights text instead of drawing (the cursor changes to indicate the active mode) |
| `Alt+drag` | Select & copy text (snaps to whole words) — works in either mode |

### Pages

| Key | Action |
|-----|--------|
| `PageDown` | Next page (keeps current zoom) |
| `PageUp` | Previous page (keeps current zoom) |
| `Ctrl+Shift+N` | Add blank page after current |
| `Ctrl+Shift+Delete` | Delete current page |
| `Ctrl+T` | Toggle outline / page-thumbnail sidebar (Outline ⇄ Pages switcher when the PDF has both) |
| Drag thumbnail → thumbnail | Reorder pages (in the page-thumbnail sidebar) |

### Zoom & pan

| Input | Action |
|-------|--------|
| Scroll | Pan |
| Scroll past page edge | Flip to next / previous page (keeps zoom) |
| `Ctrl+scroll` | Zoom in/out (centered on the cursor) |
| Pinch (two-finger) | Zoom and pan together — the points under your fingers stay fixed on the page |
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
| `Ctrl+Alt+click` | Place a numbered anchor on the PDF, linked to the note paragraph at the current cursor position |
| `Ctrl+Alt+drag` | Place an anchor **and** a callout box at the drag end — the anchor's note paragraph is rendered on the PDF with an arrow pointing from the anchor |

### Inline math (notes)

Renders automatically on lines where the cursor isn't; move the cursor to a line to edit the raw syntax.

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

Opened and saved files are tracked in `~/.local/share/sidemark/recent.json` (newest first, 15 entries) and accessible three ways:

- **In-app** — the clock-arrow button next to *Open* lists them.
- **XDG recent files** — opens are registered in `recently-used.xbel`, so GTK/GNOME file dialogs and KDE (including krunner's recent-documents results) pick them up automatically.
- **walker / Omarchy launcher** (opt-in) — `./install.sh --walker-menu` drops `extras/sidemark_recent.lua` into `~/.config/elephant/menus/` (needs `jq`). Reach it via walker's provider list (`/` by default), or bind a prefix in `~/.config/walker/config.toml`:

  ```toml
  [[providers.prefixes]]
  prefix = "p:"
  provider = "menus:sidemarkrecent"
  ```

For other launchers (rofi, fuzzel, …) `sidemark --list-recent` prints `name<TAB>path` lines and exits — useful for scripting or building your own menu.

## Notes format

Notes are saved alongside the PDF as `<filename>-notes.md` using invisible `<!-- page:N -->` markers, so the file renders cleanly in any Markdown viewer or Obsidian vault. Anchor markers (`<!-- anchor:X:Y -->`) and callout markers (`<!-- callout:X:Y -->`) are stored the same way — invisible in external viewers. Inside Sidemark, anchors appear as numbered circles on the PDF canvas; a callout additionally renders its anchor's note paragraph in a box at the callout position, with an arrow from the anchor. Callouts are included in Ctrl+E exports.

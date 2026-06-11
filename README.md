# Sidemark

[![AUR version](https://img.shields.io/aur/version/sidemark-git)](https://aur.archlinux.org/packages/sidemark-git)
[![CI](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml/badge.svg)](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml)

Sidemark is a lightweight PDF annotator for Linux with a live Markdown notes panel. Open a PDF — lecture slides, papers, or any document — draw directly on it, and write structured notes beside it.

![Screenshot](screenshot.png)

## Features

- **Draw annotations** with a configurable pen — strokes are saved as PDF ink annotations and remain individually erasable by right-click-dragging
- **Live markdown notes** linked to PDF pages, with anchor markers to pin notes to specific spots
- **Quick page navigation** via drag to pan, Shift+drag to easily Zoom to region and Shift+click to zoom back
- **Add and delete pages** — insert blank pages with same dimensions
- **Text selection** — Alt+drag highlights words and copies them to the clipboard
- **Text search** — Ctrl+F opens a search bar; highlights all matches across all pages, navigate with Enter / ↑↓
- **Formats** — Opens `.pdf`, `.pptx` (auto-converts via LibreOffice), and `.md` files
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

Installs the app, creates a launcher entry, and registers it as the default handler for PDF and Markdown files.

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
| `Ctrl+Z` | Undo last draw or erase (an erase drag undoes as one) |
| `Alt+drag` | Select & copy text (word-level highlight) |

### Pages

| Key | Action |
|-----|--------|
| `PageDown` | Next page |
| `PageUp` | Previous page |
| `Ctrl+Shift+N` | Add blank page after current |
| `Ctrl+Shift+Delete` | Delete current page |

### Zoom & pan

| Input | Action |
|-------|--------|
| Scroll | Pan |
| Scroll past page edge | Flip to next / previous page (keeps zoom) |
| `Ctrl+scroll` | Zoom in/out (cursor-anchored) |
| `Ctrl+drag` | Pan |
| `Shift+drag` | Zoom to region |
| `Shift+click` | Fit page |

### Notes

| Key | Action |
|-----|--------|
| `Ctrl+B` | Bold selection |
| `Ctrl+I` | Italic selection |
| `Ctrl+E` | Inline code selection |
| `Ctrl+\` | Toggle notes panel |
| `Ctrl+Alt+click` | Place a numbered anchor marker on the PDF at the cursor position in notes |

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
| `Ctrl+F` | Open search bar |
| `Enter` / `↓` | Next match |
| `↑` | Previous match |
| `Escape` | Close search |

### File

| Key | Action |
|-----|--------|
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

## Notes format

Notes are saved alongside the PDF as `<filename>-notes.md` using invisible `<!-- page:N -->` markers, so the file renders cleanly in any Markdown viewer or Obsidian vault. Anchor markers (`<!-- anchor:X:Y -->`) are stored the same way — invisible in external viewers, but displayed as numbered circles on the PDF canvas inside Sidemark.

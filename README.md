# PDF Editor for Omarchy

A minimal GTK4/libadwaita PDF viewer and annotation tool, designed for [Omarchy](https://omarchy.com) but usable on any Linux desktop.

![Screenshot](screenshot.png)

## Features

- **View PDFs** with pan and zoom (scroll, Ctrl+scroll, or drag-to-zoom)
- **Draw annotations** directly on pages with a configurable pen (color, width)
- **Undo** strokes one at a time
- **Live markdown notes** in a resizable sidebar with syntax highlighting and Ctrl+B/I/E shortcuts
- **Save in place** — annotations are written back into the PDF
- **Open markdown files** directly — notes-only mode with no PDF required
- Picks up background/accent colors from `~/.config/omarchy/current/theme/colors.toml`

## Installation

### AUR (Arch Linux / Omarchy)

```bash
yay -S pdf-editor-omarchy-git
```

### install.sh (any Linux)

```bash
git clone https://github.com/brokkoli71/pdf-editor-omarchy
cd pdf-editor-omarchy
./install.sh
```

This installs the app, creates a launcher entry, and registers it as the default handler for PDF and Markdown files. To uninstall:

```bash
./install.sh --uninstall
```

### Run directly (no install)

```bash
git clone https://github.com/brokkoli71/pdf-editor-omarchy
cd pdf-editor-omarchy
python pdfeditor.py [file.pdf]
```

**Dependencies** (Arch / EndeavourOS):

```bash
sudo pacman -S python python-gobject gtk4 libadwaita poppler-glib python-cairo gtksourceview5
```

## Usage

Open a file from the command line or via the **Open** button. Use **New** to create a blank A4 PDF.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save |
| `Ctrl+Z` | Undo last stroke |
| `Ctrl+\` | Toggle notes panel |
| `PageDown` | Next page |
| `PageUp` | Previous page |
| `Ctrl+B` | Bold selected text in notes |
| `Ctrl+I` | Italic selected text in notes |
| `Ctrl+E` | Inline code selected text in notes |

## Zoom & pan

| Input | Action |
|-------|--------|
| Scroll | Pan |
| Ctrl + scroll | Zoom in/out (cursor-anchored) |
| Ctrl + drag | Pan |
| Shift + drag | Zoom to region |
| Shift + click | Fit page |

## Notes

Notes are saved alongside the PDF as `<filename>-notes.md`, using invisible `<!-- page:N -->` markers so the file renders cleanly in any Markdown viewer or Obsidian vault.

# PDF Editor

A minimal GTK4/libadwaita PDF viewer and annotation tool for Linux.

![Demo](demo.gif)

![Screenshot](screenshot.png)

## Features

- **Draw annotations** with a configurable pen — strokes are saved as PDF ink annotations and remain individually erasable after reopening
- **Erase strokes** by right-click-dragging over them — works on strokes from previous sessions too
- **Add and delete pages** — insert blank pages with the same dimensions as the current page
- **Text selection** — Alt+drag highlights words and copies them to the clipboard
- **Live markdown notes** sidebar with syntax highlighting, Typora-style rendering, and Ctrl+B/I/E formatting shortcuts
- **Open markdown files** directly — notes-only mode with no PDF required
- **Obsidian integration** — one-click button to open the notes file in Obsidian
- **New blank PDF** on startup — start drawing immediately, save with a name when ready
- Opens `.pdf`, `.pptx` (auto-converts via LibreOffice), and `.md` files
- Picks up accent color and dark/light mode from Omarchy, GNOME, or KDE automatically

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

Installs the app, creates a launcher entry, and registers it as the default handler for PDF and Markdown files.

```bash
./install.sh --uninstall
```

### Run directly (no install)

```bash
git clone https://github.com/brokkoli71/pdf-editor-omarchy
cd pdf-editor-omarchy
python pdfeditor.py [file.pdf]
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
| `Ctrl+Z` | Undo last stroke |
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

### File

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save (prompts for name if untitled) |

## Notes format

Notes are saved alongside the PDF as `<filename>-notes.md` using invisible `<!-- page:N -->` markers, so the file renders cleanly in any Markdown viewer or Obsidian vault.

# PDF Editor for Omarchy

A minimal GTK4/libadwaita PDF viewer and annotation tool, designed for [Omarchy](https://omarchy.com) but usable on any Linux desktop.

![Screenshot](screenshot.png)

## Features

- **View PDFs** with pan and zoom (scroll, Ctrl+scroll, or drag-to-zoom)
- **Draw annotations** directly on pages with a configurable pen (color, width)
- **Undo** strokes one at a time
- **Per-page notes** in a resizable sidebar, saved to a sidecar `.md` file
- **Save in place** — annotations are written back into the PDF
- Picks up background/accent colors from `~/.config/omarchy/current/theme/colors.toml`

## Requirements

- Python 3
- GTK 4 + libadwaita
- `python-gobject` (PyGObject)
- `poppler-glib` (via gi typelib `Poppler 0.18`)
- `python-cairo`

On Arch / EndeavourOS:

```
sudo pacman -S python python-gobject gtk4 libadwaita poppler-glib python-cairo
```

## Usage

```
python pdfeditor.py [file.pdf]
```

Open a file from the command line or via the **Open** button.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+S` | Save |
| `Ctrl+Z` | Undo last stroke |
| `Ctrl+\` | Toggle notes panel |
| `PageDown` | Next page |
| `PageUp` | Previous page |

## Zoom

| Input | Action |
|-------|--------|
| Scroll wheel | Pan vertically |
| Ctrl + scroll | Zoom in/out (cursor-anchored) |
| Shift + drag | Draw a region to zoom into |
| Shift + click | Step back through zoom history |

## Notes

Notes are saved alongside the PDF as `<filename>-notes.md`, using invisible `<!-- page:N -->` markers so the file renders cleanly in any Markdown viewer.

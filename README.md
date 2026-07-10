# Sidemark

[![AUR version](https://img.shields.io/aur/version/sidemark)](https://aur.archlinux.org/packages/sidemark)
[![CI](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml/badge.svg)](https://github.com/brokkoli71/sidemark/actions/workflows/ci.yml)

Sidemark is a lightweight PDF annotator for Linux with a live Markdown notes panel. Open a PDF — lecture slides, papers, or any document — draw directly on it, and write structured notes beside it.

![Screenshot](screenshot.png)

> If Sidemark is useful to you, please ⭐ [star it on GitHub](https://github.com/brokkoli71/sidemark) and 🗳️ [vote for it on the AUR](https://aur.archlinux.org/packages/sidemark) — it's the main way other people discover the project.

## Why Sidemark

Sidemark was built for taking lecture notes. It works with two plain files and nothing else: your document stays a `.pdf` and your notes are a `.md` sidecar you can open in any editor. Annotations are written straight back into the PDF as native ink, so what you draw and write stays in formats you already use everywhere.

- **Just PDF and Markdown** — strokes save as native PDF ink annotations; notes save as a standard `.md` sidecar that's Obsidian-compatible and readable anywhere
- **Markdown notes built for lectures** — a full Markdown editor sits beside the page, scoped to whichever page you're on, with fast inline math for the things that actually come up in a lecture: indices, exponents, and Greek letters (`x^2`, `\alpha`, `\sum` …)
- **Open PowerPoints directly — and present on them** — a `.pptx` converts to PDF automatically with each slide's **speaker notes imported into the notes sidebar**; presenter view (`F5`) mirrors the slide to a second screen and shows your ink live while you teach
- **Anchor notes to the page** — `Ctrl+Alt+click` drops a numbered marker that links an exact spot on the PDF to the matching paragraph in your notes; callouts render the note right on the page with an arrow
- **Rearrange pages by drag-and-drop** — reorder, import, and export pages from the thumbnail sidebar by dragging (drag pages out to a file manager to export, drop a PDF in to insert), inspired by Apple's Preview
- **GoodNotes-style lasso** — loop around existing ink to select it, then drag to move, recolour, or delete it as a single undo step

## Features

### Annotations

- **Draw** with a configurable pen — strokes are saved as native PDF ink annotations and are individually erasable by right-click-dragging
- **Straight-line snap** — hold still mid-stroke to lock to a straight line; move while holding to aim, release to commit
- **Highlighter** (`Ctrl+H`) — wide translucent strokes with their own color and width, saved like any annotation. Long-press the tool for **mark text**, which lays clean highlight bands over the words you drag across — still ink, so erase and undo work unchanged
- **Lasso ink** (lasso tool, or `Ctrl+Shift+Alt+drag`) — loop around strokes to select them (GoodNotes-style), then drag to **move**, drag a corner handle to **resize**, `Ctrl+D` to **duplicate**, `Delete` to remove, or pick a new colour/width to **recolour** — each a single undo step
- **Undo / redo** (`Ctrl+Z` / `Ctrl+Y`) — works across both the canvas and notes; undo a stroke, an erase, or a burst of typing in the order you made them

### Notes

- **Live Markdown** with syntax highlighting, inline math (`x^2`, `\alpha`, `\sum` …) and formatting shortcuts (`Ctrl+B`, `Ctrl+I`, `Ctrl+E`). Symbols render for display only — the `.md` always keeps the source `\commands`, so notes round-trip cleanly through other editors. `Ctrl+±` / `Ctrl+scroll` zooms the notes font (remembered between sessions)
- **Anchor markers** (`Ctrl+Alt+click`) — numbered circles placed on the PDF that link to the corresponding paragraph in your notes
- **Callout boxes** (`Ctrl+Alt+drag`) — an anchor plus its note paragraph rendered in a box on the PDF, arrow included; drag the anchor or the box to reposition. Renders the same inline math and Markdown as the notes; included in exports
- **Standalone text boxes** (`Ctrl+Alt+right-click`) — drop typed text straight on the page, no anchor; edit it in the notes panel, drag it to reposition; included in exports
- **Date / time snippets** — type `/date`, `/time`, or `/now` then Space to expand
- **Choose where notes live** — each PDF gets a `<filename>-notes.md` sidecar, created only once you actually write something; pick **Notes file…** from the ☰ menu to point several PDFs at one shared Markdown file (remembered per PDF)
- **Text-first mode** — open a bare `.md` (or **New text page**, `Ctrl+Alt+N`) and the window becomes one endless A4 sheet of live Markdown you can **draw on** with the same pen, highlighter, eraser and lasso — straight-line snap, smoothing, move/resize/duplicate included (`Alt+drag` draws without leaving the text tool; ink rides along with the text you anchor it to). The file stays **pure Markdown** — ink lives in a `<name>-ink.json` sidecar — and **Export as PDF** renders text and ink to A4 pages. Launching Sidemark without a file opens a persistent scratchpad page

### Navigation

- **Pan & zoom** — scroll to pan, `Ctrl+scroll` or pinch to zoom (centered on the cursor), `Shift+drag` to zoom to region, `Shift+click` to fit page
- **Page flip** — `PageDown` / `PageUp` or the mouse **back/forward side buttons** (they work even while typing notes); scrolling past a page edge flips automatically
- **Follow links** — `Alt+click` a footnote, citation, or cross-reference to jump to its target (scrolling to the exact spot, even on the same page); `Alt+Left` jumps back to where you were reading. External URLs open in your browser
- **Outline & thumbnails** — `Ctrl+T` toggles a sidebar between table of contents and page thumbnails; drag thumbnails to reorder pages, drop a PDF between them to insert its pages, or drag pages **out to a file manager** to export them as a standalone PDF (annotations and notes included), like macOS Preview
- **Add / delete pages** — insert blank pages with the same dimensions as the current page
- **Presenter view** (`F5`) — mirrors the current page fullscreen on a second screen, your ink updating **live** while you draw, and can be driven from either side (clicker-friendly). Your own window switches to a presenter layout — current slide, a **peek at the next one**, a presentation timer and large prev/next buttons — none of it visible to the audience

### Files & integration

- **Formats** — opens `.pdf`, `.pptx` (auto-converted via LibreOffice) and `.md` files (a `<name>-notes.md` sidecar reopens its PDF; other text files open as a text-first page); drag files from your file manager onto the window
- **OCR for scans** — a scanned PDF with no text layer triggers an offer to **make it searchable** in the background (also on demand from the ☰ menu). Needs the optional [`ocrmypdf`](https://ocrmypdf.readthedocs.io) tool — `./install.sh --with-ocr` installs it
- **Share to phone (live)** — the **QR-code button** opens a **live view** in a phone's browser that follows along as you draw, annotate and flip pages, with a download of the fully exported PDF — so an audience can watch on their own phones while you teach. Works on the same Wi-Fi, or from anywhere via [Tailscale](https://tailscale.com); the QR code needs the optional [`qrencode`](https://fukuchi.org/works/qrencode/) tool
- **Tabs** — files open as tabs (the strip appears only with more than one document); `Ctrl+W` closes, `Ctrl+Shift+T` reopens, and tabs drag out into their own window or between windows. Sidemark runs as a **single instance**: every launch lands as a tab in the window you were last using (`SIDEMARK_NEW_WINDOW=1` gives each launch its own window)
- **Recent files** — in-app menu, XDG recent-files integration (GTK / GNOME / KDE file dialogs), and an optional walker / Omarchy launcher menu
- **Text selection** — `Alt+drag` selects words in reading order and copies them (`Ctrl+M` makes plain drags select instead of draw); long-press the select tool for a **rectangular** marquee, handy for tables and code
- **Design scheme** — inherits accent color and dark / light mode from Omarchy, GNOME, or KDE automatically
- **Tool switch** — a segmented header control picks the active tool: pen, highlighter, eraser, lasso, text-select, pan, zoom and anchor. Each tool is the modifier-free version of a gesture, and holding a gesture's modifier lights its button up — so the shortcuts are discoverable
- **Responsive header** — the compact single-row toolbar folds progressively as the window narrows (file actions live in the ☰ menu), so the core controls stay reachable at any width

## Installation

### AUR (Arch Linux / Omarchy)

```bash
yay -S sidemark        # latest release
yay -S sidemark-git    # latest development version (master)
```

### install.sh (any Linux)

```bash
git clone https://github.com/brokkoli71/sidemark
cd sidemark
./install.sh
```

Installs the app, creates a launcher entry, registers it as the default handler for PDF and Markdown files, and installs bash tab-completion for the `sidemark` command. If OCR support isn't already present it offers to install it (see below). Run `./install.sh --help` for all flags; the main ones are `--with-ocr` (install OCR support for scanned PDFs without prompting), `--walker-menu` (launcher recent-files menu, see below) and `--register-pptx` (also become the default handler for PowerPoint files, which open via LibreOffice conversion).

```bash
./install.sh --uninstall
```

### Command line

```bash
sidemark [OPTIONS] [FILE]      # FILE: a .pdf, .pptx, .md, or text file
sidemark --help                # full option list
sidemark --page 5 lecture.pdf  # open at a given page
```

Tab-completion for the `sidemark` command's options and files is installed automatically (start a new shell to pick it up). To complete `./install.sh`'s own flags, `source extras/install.sh.bash` from the repo (both work in zsh after `autoload -U +X bashcompinit && bashcompinit`).

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
| Hold still mid-stroke | Snaps the stroke to a straight line (GoodNotes-style) — keep holding and move to aim it, release to commit |
| Right-drag | Erase stroke (including from previous sessions) |
| `Ctrl+H` | Toggle highlighter — wide translucent strokes, own color/width in pen settings |
| `Ctrl+Shift+drag` | Draw one highlighter stroke without switching tool (reverts on release) |
| Lasso tool / `Ctrl+Shift+Alt+drag` | Loop around strokes to select them, then drag to move · corner handle to resize · `Ctrl+D` to duplicate · `Delete` to remove · change colour/width to recolour · `Escape` to clear |
| `Ctrl+Z` | Undo the last action — a stroke, an erase, or a burst of typing — works across drawing and notes regardless of where the cursor is |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo the last undone action |
| `Ctrl+M` | Toggle draw / select-text mode — in select mode a plain left-drag highlights text instead of drawing (the cursor changes to indicate the active mode) |
| `Alt+drag` | Select & copy text (snaps to whole words) — works in either mode |
| Long-press select tool | Switch text selection between reading-order (default) and rectangular |
| Long-press highlighter tool | Switch highlighter between free-hand (default) and mark-text (drag over words to highlight whole lines) |

### Pages

| Key | Action |
|-----|--------|
| `PageDown` | Next page (keeps current zoom) |
| `PageUp` | Previous page (keeps current zoom) |
| Mouse forward / back buttons | Next / previous page — works anywhere in the window, even while editing notes |
| `Alt+click` | Follow the link under the cursor — a footnote, citation, or cross-reference jumps to its target (URLs open in your browser) |
| `Alt+Left` | Jump back to where you were before following a link |
| `Ctrl+Shift+N` | Add blank page after current |
| `Ctrl+Shift+Delete` | Delete current page |
| `F5` | Toggle presenter view — mirror the page fullscreen on a second screen (`Esc` to close) |
| `Ctrl+T` | Toggle outline / page-thumbnail sidebar (Outline ⇄ Pages switcher when the PDF has both) |
| Click / `Ctrl+click` thumbnail | Click selects a single page; `Ctrl+click` adds or removes a page from the multi-page selection |
| Drag thumbnail → thumbnail | Reorder pages (in the page-thumbnail sidebar) |
| Drop a PDF → between thumbnails | Insert that PDF's pages at the drop point (a drop line shows where) |
| Drag thumbnail(s) → file manager / desktop | Export the dragged page(s) as a standalone PDF (notes appended), like macOS Preview |

### Zoom & pan

| Input | Action |
|-------|--------|
| Two-finger drag (touchpad) / scroll wheel | Pan — a touchpad pans smoothly in any direction (no axis lock) |
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
| `(` `[` `{` `"` … | Surround the selection with the bracket / quote pair |
| `Ctrl+I` | Italic selection |
| `Ctrl+E` | Inline code selection |
| `Ctrl+D` | Duplicate the current line (or every line the selection spans) |
| `Ctrl++` / `Ctrl+-` | Bigger / smaller notes font (also `Ctrl+scroll`) — handy for reading presenter notes; remembered between sessions |
| `Ctrl+0` | Reset the notes font to the default size |
| `Alt+↑` / `Alt+↓` | Move the current line (or selected lines) up / down |
| `/date` `/time` `/now` | Type the snippet then Space/Enter — expands to today's date, the time, or both |
| `Ctrl+\` | Toggle notes panel |
| `Ctrl+Alt+click` | Place a numbered anchor on the PDF, linked to the note paragraph at the current cursor position |
| Drag an anchor | Move a placed anchor to a new spot (a click without dragging still jumps to its note) |
| Drag a callout box | Move a placed callout box to a new spot — the arrow re-aims from its anchor automatically |
| `Ctrl+Alt+drag` | Place an anchor **and** a callout box at the drag end — the anchor's note paragraph is rendered on the PDF with an arrow pointing from the anchor |
| `Ctrl+Alt+right-click` | Drop a **standalone text box** on the page (no anchor) — type into the placeholder in the notes panel; drag the box to reposition |

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
| `\hat{x}` `\bar{x}` `\tilde{x}` `\vec{x}` | accents x̂ x̄ x̃ x⃗ (also `\dot` / `\ddot`; braces optional: `\hat x`) |

Inside an inline `` `code` `` span nothing above is applied — the text renders verbatim (so `` `snake_case` ``, `` `2^10` `` or `` `\alpha` `` stay literal), matching how a Markdown viewer treats code.

Stored as plain text in the `.md` sidecar — renders cleanly in Obsidian and any Markdown viewer.

### Links (notes)

Wiki-style `[[…]]` links in the notes jump to another slide — in the same deck or a different document — which is handy for pointing "this builds on that earlier slide." They render as a styled link (brackets hidden); **Ctrl+click** follows one:

| Syntax | Follows to |
|--------|-----------|
| `[[#page=12]]` or `[[#12]]` | page 12 of the current document |
| `[[lecture2.pdf]]` | opens `lecture2.pdf` (in a tab) |
| `[[lecture2.pdf#page=5]]` or `[[lecture2.pdf#5]]` | opens `lecture2.pdf` at page 5 |
| `[[lecture2.pdf#page=5\|the proof]]` | same, but shows *the proof* in the text |

Typing `[[` opens an **autocomplete popup** (open tabs, recent files, *This
page*), and `|display text` sets an Obsidian-style alias. The `.md` keeps the
plain `[[…]]` text, so links round-trip through Obsidian.

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
| `Ctrl+O` | Open file (in a new tab) |
| `Ctrl+N` | New blank PDF (in a new tab) |
| `Ctrl+Alt+N` | New text page — endless Markdown paper you can draw on |
| `Alt+Drag` | On a text page: draw with the pen while the text tool is active |
| `Ctrl+Scroll` / pinch | On a text page: zoom the sheet — paper, text and ink together (`Ctrl+0` resets; `Shift+click` with a drawing tool fits the width) |
| `Ctrl+S` | Save (prompts for name if untitled) |
| `Ctrl+W` | Close the current tab (prompts to save unsaved changes; closes the window with the last tab) |
| `Ctrl+Shift+T` | Reopen the most recently closed tab |

PDF-level shortcuts — `PageUp` / `PageDown` (page flip), the mouse back/forward side buttons, `Ctrl+\` (toggle notes), `Ctrl+W` (close tab), `Ctrl+Shift+T` (reopen closed tab) — work no matter which side has focus, so flipping pages while typing notes works as expected.

## Tested distributions

| Distro | Unit tests | Install |
|--------|-----------|---------|
| Arch Linux | ✓ | ✓ CI |
| Ubuntu 24.04 | ✓ CI | ✓ CI |
| Fedora 41 | | ✓ CI |

"✓ CI" = verified on every push via GitHub Actions. Arch unit tests run locally (Omarchy is the primary development environment).

## Autosave

While there are unsaved changes, Sidemark snapshots the document and notes (text-first pages included) every 60 seconds to `~/.local/state/sidemark/autosave/` — the original file is never modified until you explicitly save. If Sidemark closes uncleanly, reopening the file offers to recover the snapshot. Snapshots are removed on save or discard, and pruned after 30 days.

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

Notes are saved alongside the PDF as `<filename>-notes.md` (or a custom file you pick via **Notes file…**, remembered per PDF; the file is created lazily, only once you write something) using invisible `<!-- page:N -->` markers, so the file renders cleanly in any Markdown viewer or Obsidian vault. Anchor markers (`<!-- anchor:X:Y -->`) and callout markers (`<!-- callout:X:Y -->`) are stored the same way — invisible in external viewers. Inside Sidemark, anchors appear as numbered circles on the PDF canvas; a callout additionally renders its anchor's note paragraph in a box at the callout position, with an arrow from the anchor. Callouts are included in exports.

When you **export with notes** (☰ menu), every page keeps its on-page marks and each annotated page is followed by its notes (short notes from several pages are grouped onto shared notes pages; options in the export dialog).

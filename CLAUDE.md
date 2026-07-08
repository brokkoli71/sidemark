# CLAUDE.md — working on Sidemark

> **Maintain this file.** It is every future session's first impression of the
> project — when your work makes it stale (new module, changed architecture,
> new convention or workflow, a gotcha worth recording), update it in the same
> change. Edit in place and keep it lean: replace outdated facts rather than
> appending, and don't let it grow into a changelog — detailed *why* belongs
> in `ideas.csv`, session state in `notes/` handoffs.

## What this is

Sidemark is a **single-file GTK4/libadwaita Python app** (`sidemark.py`, ~10k
lines): a PDF annotator with a live Markdown notes panel, built for lecture
notes and presenting. One window, three document modes (see below). The only
other source module is `deck.py` — the Sidemark Deck presentation editor,
lazy-imported by sidemark.py. Dependencies: PyGObject/GTK4/Adw/GtkSource,
PyMuPDF (`fitz`), cairo, numpy; LibreOffice headless is an *optional* backend
(pptx→pdf conversion). Files stay plain: `.pdf` + `.md` sidecar notes,
`<name>-ink.json` ink sidecars, `.smdeck` (plain JSON) for decks.

## Architecture in one minute

- `PDFCanvas` — the PDF page canvas (ink, lasso, anchors, zoom/pan).
- `MarkdownNotesView` — the live-Markdown editor (math substitution `\alpha`→α,
  `x^2` scripts; source text stays intact — display-only rendering).
- `TextPageView` — text-first mode: endless A4 Markdown sheet you can draw on.
- `deck.py`: `DeckModel` (slides as dicts, JSON I/O), `DeckView` (slide canvas
  ONLY — the window supplies all chrome), `DeckPresenterWindow`,
  `render_slide()` (shared by canvas/thumbnails/presenter/PDF export),
  `deck_from_images()` (PPTX import: builds a deck of full-bleed slide pictures).
- **PPTX import**: opening a `.pptx` imports it as an *editable deck* (not a
  flat PDF). `_convert_pptx_then_open` → LibreOffice pptx→pdf →
  `_rasterize_pdf_slides` (PyMuPDF, `PPTX_IMPORT_WIDTH`px PNG per page) →
  `deck_from_images` + `_extract_pptx_notes` → `_open_deck_model(model, title,
  path=None)` mounts it untitled+dirty. Slides are pictures (text not
  editable-as-text — MVP; structured import is ideas.csv row 99).
- `DocumentSession` — one open document (one tab). The window
  (`PDFEditorWindow`) owns an `Adw.TabView` of sessions and **proxies the
  active session's attributes onto itself** via `_session_prop` — window code
  reads `self.canvas`, `self._deck_view` etc. and transparently follows the
  active tab. When adding per-document state, add it to `DocumentSession.STATE`
  / `WIDGETS`.
- **Modes**: `DocumentSession.doc_mode` ∈ `"pdf" | "text" | "deck"` — one
  unified window UI; a mode is a document type wearing a different set of
  tools. `_text_mode`/`_deck_mode` are compatibility boolean properties.
  Header chrome per mode is declared in the `_MODE_CHROME` table
  (`_update_header_for_mode` applies it; `_mode_*` tool buttons automatically
  apply to their `_pmode_*` popover twins). Tool switching routes through
  `_set_tool_mode` to the mode's view; `_global_undo/redo` and `_on_save`
  dispatch per mode.
- **Sidebar thumbnails** use a provider interface: `_ThumbnailProvider`
  (`count/thumb_size/render/activate/reorder/tooltip/invalidated` + capability
  flags `can_export`, `can_insert_files`, `confirm_reorder`) consumed by the
  generic `_build_thumb_rows`. New sidebar features must be written against
  the provider, not `if mode == ...` branches; PDF-only behaviors are gated by
  capabilities.
- Single-instance app (`Gio.Application`): a second launch forwards to the
  primary. For manual testing always launch with
  `SIDEMARK_STANDALONE=1 /usr/bin/python3 sidemark.py [FILE|--presentation]`.

## Testing & verification

- `./run_tests.sh` runs the whole suite (`test_pdfeditor.py`, ~470 tests)
  inside a **headless Weston compositor** (GTK4 has no offscreen backend —
  never use `GDK_BACKEND=offscreen`). `./run_tests.sh -x -q
  test_pdfeditor.py::TestDeckMode` etc. passes pytest args through;
  `./run_tests.sh --stop` tears the compositor down.
- Tests set `SIDEMARK_TEST=1` and use the system `/usr/bin/python3` (not venv
  shims). Window tests build a real `PDFEditorWindow` inside a throwaway
  `Adw.Application` and pump the main loop (`_settle()` pattern — copy it).
- For visual verification, launch the app (standalone env var above) and
  screenshot with `grim` (Hyprland); focus the window first via
  `hyprctl dispatch focuswindow address:...`. Don't leave repeated windows
  popping up on the user's screen.

## Feature acceptance checklist (every feature)

1. Tests in `test_pdfeditor.py`.
2. A row in `ideas.csv` (the project's decision log — write detailed Notes,
   they are the long-term memory of *why*; see rows 96/97 for the style).
3. README section/bullet.
4. Packaging if files/deps changed: `install.sh`, `PKGBUILD`, and
   `aur/sidemark/PKGBUILD` (deck.py is installed beside sidemark.py by all
   three); bash completion in `extras/sidemark.bash`; `.desktop` keywords.

## Conventions & gotchas

- **Commits**: Conventional Commits WITH scope (`feat(deck):`, `fix(nav):`);
  changelog via git-cliff. End commit messages with the Claude co-author
  trailer. Pragmatic granularity: when WIP is co-mingled, one commit is fine.
- **Branches**: Deck work lives on the experimental `deck` branch — do NOT
  merge/push Deck to `master` without asking (it may become an extension).
- **Wayland file DnD** needs `Gtk.DropTargetAsync` + a drag-motion handler
  returning an action, or the drop never fires (portal transfer).
- **GTK4 popovers**: never popdown one popover and popup a sibling on the same
  widget synchronously — defer to the "closed" signal.
- The codebase favors long, explanatory comments about *why* (and records
  hard-won platform quirks inline) — match that style.
- Logging: `logger` writes a per-session file under
  `~/.cache/sidemark/logs/`, auto-deleted on clean exit, kept on errors.
- `deck.py` must stay importable standalone (tests import it); sidemark
  injects its machinery after the lazy import (`_deck_module()` sets
  `deck.logger` and `deck.notes_to_markup`).

## Current state (2026-07)

On the `deck` branch, freshly merged with `origin/master` (two features, both
already correct for decks): (1) *open launched file as a tab in the last-used
window* (`_open_target`/`open_file_in_tab`) — decks ride along automatically
because `.smdeck`/`.pptx` route through `open_file`→`_do_open_file`; (2)
*verbatim `code` spans + per-version instance id* — the code-span change is in
`_notes_to_pango_markup`/`_split_code_spans`, which is `deck.notes_to_markup`,
so **slide textboxes now render `code` verbatim too**, no deck code changed.
When merging master, always audit each feature for deck impact (see memory
`feedback_merge_check_deck_impact`).

Shipped since v2: PPTX→deck import (image + speaker
notes, row 98), the deck presenter's next-slide preview + present-button
fix (row 101), and **Phase 2 — deck themes**:
- *Part 1, native themes (row 102):* `DeckModel.theme` in the `.smdeck`
  (`FORMAT_VERSION` 2, v1 back-compat via `deck._normalize_theme`), textboxes
  carry a `role` (title/subtitle/body), `render_slide(cr, slide, theme, …)`
  reads bg/fonts/colors, two built-in themes (`Classic`, `Midnight`) in
  `deck.THEMES`, and a theme-picker menu-button in `_build_deck_bar` calling
  `DeckView.set_theme` (undoable). No colour-editing UI by design.
- *Part 2, PPTX theme import (row 103):* `_extract_pptx_theme` (sidemark.py,
  OOXML parse → unit-free "design" dict, reuses the `_extract_pptx_notes`
  zip/rels walk) + `deck.build_imported_theme` (design → theme with
  fallbacks/contrast-guard/geometry). Colours resolve theme `clrScheme`
  through the master's `<p:clrMap>`; fonts from `fontScheme`; title/body
  geometry from the master placeholders (EMU→fraction). Imported decks now
  carry a theme so added slides match. This clrMap+scheme+placeholder chain is
  the machinery structured PPTX text import (row 99) will reuse.
- *Part 3, PPTX background import (row 107):* `_extract_pptx_theme` resolves the
  page background with real inheritance (slide→layout→master, nested `_bg_spec`)
  and extracts **picture backgrounds** (`blipFill` → media → PNG via
  `fitz.Pixmap`) as `design["bg_image"]` (PNG bytes; gradients → first-stop
  colour). `build_imported_theme` base64s it into the theme; `render_slide`
  paints it full-bleed (`EXTEND_PAD`) via `deck._theme_bg_surface` (cache under
  `_bg_surface`, which `to_json` strips — themes are now `_clean`ed on save).
  NB: import fidelity of **fonts** depends on the source typeface being
  installed locally (else Pango substitutes); the user's own decks also have no
  importable background (plain `bg1` master), so this helps other decks, not
  those — see ideas.csv row 107.

**Next up:** Cairo smart-arts, build-step animations, Claude-generated
LaTeX/TikZ figures (roadmap in `~/.claude/plans/linked-launching-allen.md`).
Nearer-term follow-ups: structured PPTX text import (row 99, now unblocked by
the placeholder machinery), a "missing fonts" heads-up on import, and deck-bar
collapse polish (row 100).

Open cosmetic polish: ideas.csv row 100 (row 100 follow-up #2's
present-button half is now fixed; the deck bar still doesn't fold, it
scrolls).

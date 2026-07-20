# CLAUDE.md — working on Sidemark

> **Maintain this file.** It is every future session's first impression of the
> project — when your work makes it stale (new module, changed architecture,
> new convention or workflow, a gotcha worth recording), update it in the same
> change. Edit in place and keep it lean: replace outdated facts rather than
> appending, and don't let it grow into a changelog — detailed *why* belongs
> in `ideas.csv`, session state in `notes/` handoffs.

## What this is

Sidemark is a **single-file GTK4/libadwaita Python app** (`sidemark.py`, ~12.6k
lines): a PDF annotator with a live Markdown notes panel, built for lecture
notes and presenting. One window, two document modes (PDF + text — see below).
There is no other source module on this branch. Dependencies:
PyGObject/GTK4/Adw/GtkSource, PyMuPDF (`fitz`), cairo, numpy. Files stay plain:
`.pdf` + `.md` sidecar notes, `<name>-ink.json` ink sidecars. The `.md` file
names its PDF with an `![[name.pdf]]` embed line at the top.

## Architecture in one minute

- `PDFCanvas` — the PDF page canvas (ink, lasso, anchors, zoom/pan).
- `MarkdownNotesView` — the live-Markdown editor (math substitution `\alpha`→α,
  `x^2` scripts; source text stays intact — display-only rendering). `code`
  spans and `[[wiki links]]` render verbatim (no LaTeX/scripts/bold inside).
- `TextPageView` — text-first mode: an A4-styled Markdown sheet (a
  `MarkdownNotesView` as white paper) you can draw on. Ink lives in a
  `<name>-ink.json` sidecar.
- `DocumentSession` — one open document (one tab). The window
  (`PDFEditorWindow`) owns an `Adw.TabView` of sessions and **proxies the
  active session's attributes onto itself** via `_session_prop` — window code
  reads `self.canvas`, `self._notes_view` etc. and transparently follows the
  active tab. When adding per-document state, add it to `DocumentSession.STATE`
  / `WIDGETS` (kept in sync with the `_session_prop` proxy list).
- **Input chords**: module-level `chord_tool()` is THE modifier-chord grammar
  (Ctrl=pan, Alt=ink↔text flip, Ctrl+Shift=highlighter, Ctrl+Shift+Alt=lasso,
  Ctrl+Alt=anchor pdf-only, Shift=zoom pdf/ink-only; Shift+Alt unassigned).
  **One exception, and it is not a fork**: with the *lasso tool* in hand Shift
  ADDS to the selection instead of zooming. `chord_tool` answers "which TOOL
  does this chord stand in for", and Shift+lasso is still the lasso — Shift
  modifies it. Nothing is lost because `Alt+Shift+drag` stays the portable zoom
  chord, which is exactly why the grammar says Shift-alone must never be
  load-bearing. Both press routers special-case `tool != "lasso"`.
  Buttons: left=tool, right=eraser, middle=navigation (Shift+middle=zoom
  region — the portable zoom chord), thumb=middle's ergonomic stand-in
  (hold=pan, Shift+hold=zoom region, scroll-while-held=zoom). Gesture
  routing, the
  transient tool-button highlight and tooltips must all derive from it —
  never grow a second mapping. Chord routing merges window-tracked held
  modifiers (`_chord_state`) so keyboard+touch works; see ideas.csv row 115.
- **Modes**: a tab is either a PDF or a text-first page, tracked by
  `doc_mode` (`"pdf"` | `"text"`) on the session
  (`_enter_text_mode`/`_leave_text_mode`; `_text_mode` survives as a
  compatibility boolean property). Which header chrome each mode shows is
  declared in the `_MODE_CHROME` table (widget name → modes tuple; `_mode_*`
  tool buttons drive their `_pmode_*` popover twins automatically) — when
  mode behavior changes, extend the table instead of adding per-mode `if`s.
  The table takes further modes without reshaping (row 107).
- **Pasted images** — an image is an OBJECT, modelled and behaving like ink:
  bytes + rect + `rotate` (+ a crop rect one day, row 119), anchored the same
  way its surface anchors ink, editable forever, never a flattened stamp. The
  clipboard is one shared layer (`SIDEMARK_MIME`, `clipboard_content_for`,
  `paste_objects`, `pasted_extent`). Both modes ship (row 118) — **read "Image
  UX is one contract" below before touching either.** On a PDF the
  `<name>-ink.json` sidecar is the truth and the PDF's optional-content layer
  is a render target regenerated on save; `attach_images()` is THE entry point
  after `canvas.load()` (it loads or adopts, then takes the layer back OUT of
  the open document — leave it in and every image renders twice).
- **`[[wiki links]]` (the linking workflow)** — this is the feature the project
  was designed around and it has shipped (ideas.csv row 99). In notes,
  `[[target]]` is a clickable link (Ctrl+click follows, hover shows a hand).
  `_parse_note_link()` resolves the body into `{path, page, label}`:
  `[[#page=N]]` jumps within the current document; `[[file]]` /
  `[[file#page=N]]` opens another document via `open_file_in_tab`. Rendering
  keeps the brackets hidden off the cursor line but leaves link/`code` contents
  verbatim — the parsing lives in `_notes_to_pango_markup` / `_split_markup`
  (`_MD_LINK_RE`, negative lookbehind so the `![[embed]]` line is left alone).
  When extending linking, keep link targets un-mangled and test both same-doc
  and cross-doc forms.
- Single-instance app (`Gio.Application`, `HANDLES_COMMAND_LINE`): a second
  launch forwards its argv to the primary, which opens the file as a tab in the
  last-used window (`_open_target`/`open_file_in_tab`). For manual testing
  always launch standalone: `SIDEMARK_STANDALONE=1 /usr/bin/python3
  sidemark.py [FILE]` (the env var sets `NON_UNIQUE` so it bypasses the running
  instance — Ctrl+R reload uses the same trick to re-read the code).

## Testing & verification

- `./run_tests.sh` runs the whole suite (`test_pdfeditor.py`) inside a
  **headless Weston compositor** (GTK4 has no offscreen backend — never use
  `GDK_BACKEND=offscreen`; needs `weston` installed). Pytest args pass through
  (`./run_tests.sh -x -q test_pdfeditor.py::SomeTest`); `./run_tests.sh --stop`
  tears the compositor down.
- **Iterate with `./run_tests.sh --fast`** (~3 s): it skips the `window`-marked
  tier (classes that build real windows; auto-marked by `conftest.py` from the
  class source — a misclassified test still *passes*, it just lands in the
  wrong speed tier). Run the full suite once before committing.
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
   they are the long-term memory of *why*; see rows 96–99 for the style).
3. README, **only if a user must know about it** — and then at most 1–3 lines
   at the altitude of "what it does for you", folded into an *existing* bullet
   or table row where one fits. The README is the sales pitch and quick
   reference for humans, not the feature log: sub-behaviors, edge cases,
   internal names, and anything a user would discover on their own belong in
   the `ideas.csv` Notes (and code comments), not here. Bug fixes, refactors,
   and dev-workflow changes get **no README text at all**. When in doubt, ask:
   would a new user's decision or daily use change without this sentence? If
   not, leave the README alone.
4. Packaging if files/deps changed: `install.sh`, `PKGBUILD`, and
   `aur/sidemark/PKGBUILD`; bash completion in `extras/sidemark.bash`;
   `.desktop` keywords.

## Conventions & gotchas

- **Commits**: Conventional Commits WITH scope (`feat(notes):`, `fix(nav):`);
  changelog via git-cliff. End commit messages with the Claude co-author
  trailer. Pragmatic granularity: when WIP is co-mingled, one commit is fine.
- **Wayland file DnD** needs `Gtk.DropTargetAsync` + a drag-motion handler
  returning an action, or the drop never fires (portal transfer).
- **GTK4 popovers**: never popdown one popover and popup a sibling on the same
  widget synchronously — defer to the "closed" signal.
- **Event reachability — a correct handler can still never run.** When a
  gesture "does nothing", test the PATH, not the handler (all of these tested
  fine in isolation while being unreachable in the app):
  - `GtkScrolledWindow` installs its OWN capture-phase scroll controller and
    STOPS scroll it can use, so a Ctrl+scroll zoom must be captured on an
    ancestor **above** it (`MarkdownNotesView.attach_zoom_scroll`,
    `TextPageView._on_sheet_scroll`). The ScrolledWindow itself is too late —
    same widget + same phase run in add order and GTK's is first.
  - A drawing tool makes the ink overlay the event **target**
    (`ink.set_can_target`), which cuts the ScrolledWindow out of the path
    entirely — so `TextPageView` owns plain scrolling too, not just zoom.
  - Window shortcuts that must beat a focused editor live in capture-phase
    controllers (`_on_global_key`, `_on_undo_key`, the sheet's own). `_on_key`
    is **bubble** and loses to whatever has focus — put a new app-level
    shortcut in capture unless the editor should win (Ctrl+C, Delete, arrows).
  - **Capture on a CHILD only fires while focus is inside that child.**
    Ctrl+C/Ctrl+V on `TextPageView` looked right and died the moment you
    picked a tool from the toolbar — that focuses the *button*, so the sheet
    was off the key path. Clues: a sibling shortcut in the SAME handler still
    worked (Ctrl+D), i.e. the handler was fine, focus was not. App-level keys
    belong on the WINDOW's capture controller, which fires whatever has focus;
    let it ask the surface (`wants_paste()`, `has_lasso_selection()`) instead
    of the surface owning the key.
- **`save()` rebinds `self.document` — anything holding the OLD one is stale.**
  `save()` reopens the file, so every cached PyMuPDF object from before it
  belongs to an orphaned document. `self.page` is the one that bites: the page
  render (`_rerender_now`) renders `self.page`, so a stale one kept painting
  the layer `_write_image_layer` had just baked in — an image rendered twice,
  invisible until you moved it (the object sits on its own ghost) and cleared
  by a reload (`_load_page` rebinds). **The file on disk was correct the whole
  time**, so neither the tests nor another viewer could see it. If you cache a
  `Page`/xref across a save, rebind it there, and test the RENDER path
  (`canvas.page.read_contents()`), not just the file.
- **A text page has no `_path`.** A `.md` opened without a PDF lives in
  `_notes_path`; `_path` is the PDF. Code reading `_path` alone silently
  no-ops in text mode (this is what broke Ctrl+R) — use
  `self._path or self._notes_path`. If a feature really is PDF-only, say so
  loudly (`_on_export`, `_ocr_current`) rather than returning in silence.
- **One table, not two**: `chord_tool` (chords), `zoom_factor_for_scroll`
  (scroll→zoom rate), `erase_radius` (what counts as touching ink),
  `clipboard_content_for`/`paste_objects` (the clipboard), `draw_image` (how a
  pasted image looks), `recognize_shape`/`rect_bbox_of`/`even_divider_positions`
  /`draw_snap_label` (the extended-dwell shape snap, row 121) are shared by both
  canvases on purpose. Duplicating a *decision* is how the PDF and text sides
  drift; duplicating *mechanics* is fine — they have genuinely different
  substrates (a scale-transform canvas vs a reflowing ScrolledWindow).
- **The extended dwell (`_snap_to_shape`, row 121).** Holding still mid-stroke
  no longer only makes a line: `recognize_shape()` also cleans a closed loop
  into an axis-aligned **rectangle/ellipse**, and a straight line drawn inside a
  rectangle becomes an evenly-spaced **grid divider** (re-spacing its siblings,
  one undo entry — PDF's `("grid", …)` op / the sheet's grouped
  `("reshape", …)`). Recognised shapes are ordinary **strokes** (polylines), no
  new object kind — they lasso/erase/round-trip for free. The **line is always
  the fallback**, so the `shape_snap` setting's "lines"/"off" can't regress the
  classic snap. Rectangles are detected geometrically (`rect_bbox_of`), so grid
  snapping survives a reload with no stored tag.
- **Geometry you STORE must not go through the int-truncating coord helpers.**
  `window_to_buffer_coords`/`buffer_to_window_coords` only take ints, so a
  per-point conversion rounds every point on the way in *and* out. Invisible
  for a move (all points shift alike); it made a *rotated* stroke go lumpy, and
  compounded on every re-anchor. `TextPageView._overlay_to_buffer_f` /
  `_buffer_to_overlay` take the origin once and add the float delta — use them
  anywhere the result is persisted. Plain `_overlay_to_buffer` (int) is for
  hit-testing only. Symptom to recognise: shapes degrade a little per edit.
- The codebase favors long, explanatory comments about *why* (and records
  hard-won platform quirks inline) — match that style.
- **Image UX is one contract, not two implementations.** Text pages defined it
  and the PDF side matches it (row 118); anything new (crop, row 119) lands
  ONCE, for both. Reuse the shared pieces (`clipboard_content_for` /
  `paste_objects` / `SIDEMARK_MIME` / `pasted_extent`, `draw_image`,
  `_texture_from_png` / `_png_from_texture`) and keep the *decisions* below.
  The row 116 audit's lesson applies exactly: every behavior that reused a
  shared helper held parity; the one place that reimplemented (the eraser) was
  the one with a live bug. If one mode genuinely cannot do one of these, say so
  loudly in `ideas.csv` — do not let it drift silently.
  - **Ctrl+V pastes at the POINTER** when it is over the surface, else the
    centre of the view — never at the caret. Pasting must work with any tool;
    with a pen or lasso in hand there is no useful caret (`paste_point()`).
  - **Paste size is `paste_scale()`** — the smallest of four caps: a third of
    the page per axis, half the VISIBLE window per axis, and the image's own
    pixels on screen (`native / zoom`). The window cap is the one that matters
    when zoomed in, where a third of the page can be several screens wide; the
    page caps are what stop a screenshot landing as a page-filling slab.
  - **A selection is editable with ANY tool** (`selection_grab_at()`), and a
    paste comes back selected — so a fresh paste drags immediately, with the
    pen or the caret still in hand. On the sheet this MUST be claimed on the
    capture-phase gesture above the overlay: with the caret the ink overlay is
    not targetable, so `_on_ink_begin` never runs (the reachability trap).
  - **A lasso click selects what is under it** (`_object_at`, ink before
    images — ink paints on top); **Shift adds** to the selection, by clicking
    or circling (`_merge_selection`, which merges by IDENTITY: strokes and
    images are plain dicts, so `==`/`in` compare by VALUE and would silently
    collapse a duplicate).
  - **Sizes are DOCUMENT units** — store at the base scale, never the zoom you
    happened to paste at (the pen-width lesson, row 116).
  - **Ink draws ON TOP of images; text is not covered by them.** On a text page
    that means images live on the view's `BELOW_TEXT` layer, not the ink
    overlay. Get this wrong and it also breaks the PDF export, which
    rasterises the view (one cause, two symptoms). On a PDF page it means
    `_draw_images` runs between the page blit and the strokes.
  - **Ctrl+C on a lasso selection wins over text copy** (text selection keeps
    it otherwise) and publishes BOTH: our objects (private mime) and a
    `COPY_RENDER_SCALE`× supersampled PNG. In-app paste is lossless — ink
    comes back as editable INK, an image as an image — every other app gets a
    picture. This is a hard user requirement, not a nicety.
  - **Lasso verbs**: select / move / resize / rotate / `Ctrl+D` duplicate /
    `Del`. Rotation is a knob on a stalk above the box; Shift snaps to
    `ROTATE_SNAP_DEG`. A tilt is stored as an ANGLE and applied at render — it
    is never baked into the pixels, so repeat rotations never degrade.
  - **Resize is 8 handles (row 122)**: 4 corners scale uniformly, 4 side
    midpoints stretch ONE axis (aspect changes). One shared policy —
    `lasso_handle_points`/`lasso_handle_anchor`/`lasso_scale_factors`
    /`lasso_handle_cursor` — drives both canvases and both the hit-test and the
    painter. Scale is per-axis `(fx, fy)` about an anchor (opposite edge for a
    side, opposite corner for a corner); the `("lasso_scale", …, fx, fy, ax,
    ay)` op and stroke width (`sqrt(fx*fy)`) follow. A non-uniform stretch of a
    *rotated image* stretches along page axes, not the image's (the rect can't
    skew — same limit as rotation).
  - **One undo entry per gesture**, even when it moved ink and images together
    (the `("group", [ops])` op).
  - **The eraser ignores images** (lasso + `Del` removes them); **recolour
    skips images** — there is no pen colour on a photograph.
  - Gate on `has_lasso_selection()`, never on `self._selected` — that list is
    STROKES, and reading it as "the selection" is what made an images-only
    selection unpickable, unmovable and undeletable. Same for `_selection_bbox()`:
    one box, used by the frame AND the hit-tests, or they drift apart.
- Logging: `logger` writes a per-session file under `~/.cache/sidemark/logs/`,
  auto-deleted on clean exit, kept on errors.

## The deck branch (parked — not a concern)

An experimental Sidemark **Deck** presentation editor lives on the `deck`
branch (checked out at `../pdfeditor/`, with its own CLAUDE.md as the reference
*if* it is ever picked up again). It adds `deck.py`, a `deck` document mode,
PPTX→deck import and themes.

**Treat it as dormant.** Do not let it shape decisions on master: don't weigh
merge cost when designing or refactoring, and don't audit master's changes
against it. It may be revived some day, may become a separate extension, or may
never land at all — that call is deferred indefinitely. The one rule that
stays, because it is free: **do NOT merge/push Deck into `master` without
asking.**

## Current state (2026-07-17)

The `[[wiki links]]` linking workflow shipped (row 99) along with verbatim
`code` spans (row 96), a per-version single-instance id (row 97), and
opening launched files as a tab in the last-used window (row 98). A July
review pass added autosave/crash recovery for text-first pages (row 101), a
bug-fix batch — lasso now catches snapped straight lines, link-follow
survives a deleted current file, display-wide CSS providers no longer leak
per closed tab/window (row 102) — the `--fast` test tier (row 103), lasso
resize handles + Ctrl+D duplicate (row 104), and text-page pinch zoom +
Shift+click fit (row 105). The `doc_mode`/`_MODE_CHROME` mode framework landed
(row 107), and parity items 1–3 landed on it (row 108): text pages
now have the lasso (select/move/resize/duplicate/recolour, re-anchoring the
marks — closes row 95), straight-line snap and stroke smoothing. Text pages
also reached tool/gesture parity with the PDF canvas (row 106 items 4–5 + a
workflow pass, row 113): pan and zoom-to-region are now both toolbar tools
*and* gestures (Ctrl/middle-drag + plain-drag-with-tool pan, thumb-button pan,
two-finger drag pans as well as zooms; Shift+drag / zoom-tool zoom-to-region,
Shift+click fits width), and Alt is the caret's ink escape (Alt+left pen,
Alt+right eraser). Tool tooltips carry the Alt-hold hint per mode.

Text/PDF tool parity (row 106) is now **complete**: pan + zoom tools/gestures,
thumb-button and two-finger pan, Alt-left/right ink escape, Ctrl+H + lasso
verbs, and the Ctrl+Shift+drag temp-highlighter all work on text pages (item 7,
presenter/share for text mode, is **won't-do** per the user). The ranked
backlog (rows 110 window-reuse [not-a-bug], 109 word-level Ctrl+Z, 112 per-doc
width) and the workflow model (row 113) are done.

A July pass unified the input model into one chord grammar (row 115, see
"Input chords" above): text pages gained the lasso chord and right-drag erase
under the caret (a clean right-click re-pops the TextView menu itself — it
must claim at press), the transient tool-button highlight works fully in text
mode and lights during button gestures, and chord routing merges tracked
modifiers for touch. All of row 115 is verified on real hardware.

A **pdf/text parity audit** then closed row 116 and a run of bugs the same
method found — its method is worth reusing (walk `chord_tool` × {pdf, text},
compare sign/magnitude/**anchor**/clamping, and ask "does a test drive BOTH
sides?"). Its lesson: every behavior where the sheet reuses a `PDFCanvas`
pure helper held parity; the one it reimplemented (the eraser) was the one
with a live bug. Landed: segment-based erasing on text pages (a snapped
straight line is 2 points, so vertex-only hit-testing could never erase its
middle), shared `zoom_factor_for_scroll` / `erase_radius` / `ZOOM_RECT_MIN_PX`
/ `SCALE_MIN/MAX`, cursor-anchored sheet zoom, Ctrl+scroll and plain scroll
fixed in both the sheet and the notes panel (see "Event reachability"), Ctrl+R
in text mode (see the `_path` gotcha), the pen as a **document** width so ink
never depends on the zoom you drew at, sheet zoom to 16×, `Alt+Shift+drag` as
the portable keyboard zoom chord (the only one reaching zoom under the caret),
and Escape stepping back out of a zoom-to-region (both surfaces keep a zoom
stack).

**Row 118 (pasted images) is DONE — both halves, verified in the app.** A text
page's images sit under the text, ride their paragraph on a `GtkTextMark`, and
round-trip through the `-ink.json` sidecar as base64. A PDF page's images live
in a `-ink.json` sidecar beside the PDF (the **truth**) and are rendered into
an optional content group (OCG) layer inside the PDF on save (a **regenerated
render target**, for other viewers). Both paste with Ctrl+V (any tool, at the
pointer), lasso-select / move / resize / rotate / duplicate / delete, and copy
with Ctrl+C as the real objects plus a supersampled PNG. Row 120 then tuned the
UX on top: `paste_scale()` caps a paste at a third of the page *and* half the
visible window, a selection is editable with any tool, a lasso click selects
what is under it and Shift adds to the selection. Along the way row 118 fixed a
latent bug in ALL ink editing (the coord helpers truncated stored geometry to
whole pixels) and a doubled-render bug whose cause is worth knowing: `save()`
rebinds `self.document`, and anything still holding the old one — `self.page`
above all — renders a dead document (see the gotcha above).

**If you touch the PDF image layer, read row 118 first** — its traps silently
corrupt documents and every one is now guarded by a test that was checked to
fail when the trap is reintroduced. The short version: `/OC` marks ownership
(the only marker that survives a round-trip — it is what tells our images from
the document's own); `uniquify_png()` before an insert, or PyMuPDF DEDUPLICATES
byte-identical images onto one xref and ownership dies on the first reopen
(real workflow: copy a figure out of the PDF, paste it back); strip-and-
regenerate over `clean_contents()`, never `delete_image()`, which only blanks
the xref and LEAKS a ghost placement per save; re-place unchanged images with
`insert_image(xref=...)` (no re-encode; ~0.45 s/save at 100 heavy images).
`attach_images()` after `load()` also takes the layer back OUT of the open
document — it is a render target, and left in it renders every image twice.
Do NOT trust `get_image_info(xrefs=True)` or `get_image_rects()` — they resolve
placements by visual match and lie about xrefs; the content stream + Resources
dict are ground truth.

## Next session (2026-07-20 handoff)

**Row 121 (shape & grid recognition) just landed — code-verified, needs an
in-app pass.** The extended dwell now recognises rectangles/ellipses and snaps
grid dividers (see "The extended dwell" under Conventions). Pure classifier,
grid spacing and the PDF grid-divider commit/undo/redo are unit-tested
(`TestShapeRecognition`, `TestGridDivider`); the full suite is green. Gestures
still need the real app — hand the user the checklist. **One thing was
deliberately deferred:** Ctrl+Z on a just-snapped shape *removes* it rather than
reverting to the raw freehand (a two-step un-snap); the `shape_snap` setting +
the 500 ms dwell are the escapes for now.

**Row 118 (pasted images) is DONE and verified** — the OCG layer's tests were
re-run after the refactor, the save/reopen/move round-trip was driven
end-to-end, and the user confirmed it in the real app. Row 120 (the image UX
pass: paste-size caps, any-tool editing of a selection, lasso click-select,
Shift multi-select) landed on top of it and is verified too. Both are written
up; the README gained a line for image paste and one for the grown lasso.

**Still open: row 119 (crop)** — the last piece of the image feature. Its design
is settled in row 118 and must not be re-litigated: a field on the model
applied at render, never a destructive re-encode. It lands ONCE for both modes
(the model and draw path are shared). Read row 118's traps first if you touch
the PDF layer at all.

**Open follow-ups:** text-page items in rows 92–94 (text-snapping highlighter,
pagination/print view, margin inks that don't reflow) and row 100's link
authoring (link-to-here + backlinks). Backlog: **row 111** (duplicate-download
dialog), **row 117** (the suite is flaky under full-run load — one test fails
per full run while passing in isolation and on a clean tree; parked for its own
session), plus older rows 26/27/64.

**Verifying GUI work here:** there is no key-injection tool on this machine
(no `wtype`/`ydotool`), so gestures, Ctrl+Z and Ctrl+V cannot be driven by an
agent — script what you can against the model, prefer setups that make a bug
visible in a plain screenshot, and hand the user a short numbered checklist for
the rest. Don't close app windows with `hyprctl dispatch closewindow`: it can
raise an unsaved-changes dialog you then cannot dismiss.

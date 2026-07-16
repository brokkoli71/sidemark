# CLAUDE.md — working on Sidemark

> **Maintain this file.** It is every future session's first impression of the
> project — when your work makes it stale (new module, changed architecture,
> new convention or workflow, a gotcha worth recording), update it in the same
> change. Edit in place and keep it lean: replace outdated facts rather than
> appending, and don't let it grow into a changelog — detailed *why* belongs
> in `ideas.csv`, session state in `notes/` handoffs.

## What this is

Sidemark is a **single-file GTK4/libadwaita Python app** (`sidemark.py`, ~9.6k
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
- **A text page has no `_path`.** A `.md` opened without a PDF lives in
  `_notes_path`; `_path` is the PDF. Code reading `_path` alone silently
  no-ops in text mode (this is what broke Ctrl+R) — use
  `self._path or self._notes_path`. If a feature really is PDF-only, say so
  loudly (`_on_export`, `_ocr_current`) rather than returning in silence.
- **One table, not two**: `chord_tool` (chords), `zoom_factor_for_scroll`
  (scroll→zoom rate), `erase_radius` (what counts as touching ink) are shared
  by both canvases on purpose. Duplicating a *decision* is how the PDF and
  text sides drift; duplicating *mechanics* is fine — they have genuinely
  different substrates (a scale-transform canvas vs a reflowing ScrolledWindow).
- The codebase favors long, explanatory comments about *why* (and records
  hard-won platform quirks inline) — match that style.
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

## Current state (2026-07)

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

**Next up: row 118** — paste images from the clipboard, in both modes. The
parked `deck` branch has a reference implementation worth reading first.

**Open follow-ups:** text-page items in rows 92–94 (text-snapping highlighter,
pagination/print view, margin inks that don't reflow) and row 100's link
authoring (link-to-here + backlinks). Backlog: **row 111** (duplicate-download
dialog), **row 117** (the suite is flaky under full-run load — one test fails
per full run while passing in isolation and on a clean tree; parked for its own
session), plus older rows 26/27/64.

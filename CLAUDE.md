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
- **Modes**: a tab is either a PDF or a text-first page, tracked by
  `doc_mode` (`"pdf"` | `"text"`) on the session
  (`_enter_text_mode`/`_leave_text_mode`; `_text_mode` survives as a
  compatibility boolean property). Which header chrome each mode shows is
  declared in the `_MODE_CHROME` table (widget name → modes tuple; `_mode_*`
  tool buttons drive their `_pmode_*` popover twins automatically) — when
  mode behavior changes, extend the table instead of adding per-mode `if`s.
  This framework was ported from the `deck` branch (row 107); deck layers a
  third `"deck"` mode and a thumbnail-provider interface on top — see "The
  deck branch" below.
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
- The codebase favors long, explanatory comments about *why* (and records
  hard-won platform quirks inline) — match that style.
- Logging: `logger` writes a per-session file under `~/.cache/sidemark/logs/`,
  auto-deleted on clean exit, kept on errors.

## The deck branch (do not merge without asking)

A Sidemark **Deck** presentation editor lives on the experimental `deck`
branch, checked out separately at `../pdfeditor/`. It adds `deck.py`, a `deck`
document mode, PPTX→deck import, and deck themes, and it refactors modes into a
`doc_mode` enum (that refactor is now ported to master — row 107 — so the
remaining deck delta is mostly additive). Whether deck ever merges is
undecided; the port keeps parity work independent of that decision. It may become a separate extension — **do NOT merge/push Deck
into `master` without asking.** Its CLAUDE.md (`../pdfeditor/CLAUDE.md`) is the
reference for that work. When master gains a feature, deck must be audited for
impact when it next merges master.

## Current state (2026-07)

The `[[wiki links]]` linking workflow shipped (row 99) along with verbatim
`code` spans (row 96), a per-version single-instance id (row 97), and
opening launched files as a tab in the last-used window (row 98). A July
review pass added autosave/crash recovery for text-first pages (row 101), a
bug-fix batch — lasso now catches snapped straight lines, link-follow
survives a deleted current file, display-wide CSS providers no longer leak
per closed tab/window (row 102) — the `--fast` test tier (row 103), lasso
resize handles + Ctrl+D duplicate (row 104), and text-page pinch zoom +
Shift+click fit (row 105). The deck branch's `doc_mode`/`_MODE_CHROME` mode
framework is ported to master (row 107) so parity work no longer waits on a
deck-merge decision, and parity items 1–3 landed on it (row 108): text pages
now have the lasso (select/move/resize/duplicate/recolour, re-anchoring the
marks — closes row 95), straight-line snap and stroke smoothing. Text pages
also reached tool/gesture parity with the PDF canvas (row 106 items 4–5 + a
workflow pass, row 113): pan and zoom-to-region are now both toolbar tools
*and* gestures (Ctrl/middle-drag + plain-drag-with-tool pan, thumb-button pan,
two-finger drag pans as well as zooms; Shift+drag / zoom-tool zoom-to-region,
Shift+click fits width), and Alt is the caret's ink escape (Alt+left pen,
Alt+right eraser). Tool tooltips carry the Alt-hold hint per mode.

**Next up (user-prioritised backlog, do before the remaining parity items):**
1. **row 110** — window-reuse bug (launched files should open as tabs in the
   last window, not new windows; the feature exists but misbehaves — needs a
   repro then fix). 2. **row 109** — finer Ctrl+Z granularity in the
   notes/text editor. 3. **row 112** — per-document width for text sheets.
   Then row 106 items 6–7 (temp-highlighter, presenter for text pages) and the
   text-page items in rows 92–94. Backlog: **row 111** (duplicate-download
   dialog).

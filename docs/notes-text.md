# Notes, the Markdown view and the text sheet

> The live-Markdown editor, the divider, links, and the GtkTextView traps.

> Split out of `CLAUDE.md`, which is the starting point and links here.
>
> **This is not a changelog, and it must not become one.** Keep only what is
> TRUE NOW and can be broken by accident: an invariant, a constraint the
> platform imposes, a trap. When behaviour changes, REPLACE the old text —
> never append the new alongside it. Delete anything that has stopped being
> load-bearing.
>
> The *why* — what was tried, what was measured, what was rejected — belongs
> in `ideas.csv`, one row per feature. Link to the row instead of retelling
> it here. Present tense about how it behaves, not past tense about how it
> got here.

- **The divider is the way BETWEEN the modes (row 130)** — drag the notes
  panel to full width and the notes become the sheet; pull in from the sheet's
  LEFT edge and a page comes in beside it. It is a **VIEW state, never a
  conversion**: the PDF is still there behind the sheet, its notes are still
  per page, nothing is written either way, so a drag that crosses the line and
  comes back leaves no trace — which an accidental-drag-shaped gesture has to.
  - **`_full_notes_view()` is the whole distinction**, and it is the rule the
    codebase already had: a text-first page has NO `_path`. With one, the sheet
    is a view of the sidecar — **the whole file, markers and all**, the only
    way one buffer can hold a per-page model. The two paths that move text
    between buffer and model must say which they mean: `_commit_note_for`
    parses the sheet back (`set_from_text`), `_restore_note` fills it
    (`to_text`), and `_on_save`'s verbatim text-page branch is gated on `not
    _path` or a PDF's notes would be flattened onto page 0.
  - **The paned STAYS in text mode**, with the page side collapsed to zero and
    the sheet in the notes' slot (`_sheet_box`) — so the page *slides* in and
    out and the handle itself says there is something to pull. That needs
    `set_shrink_start_child(True)`: with shrink off the handle stops at the
    canvas's minimum and the gesture is unreachable on every window. The
    collapsed handle is the ONLY way back, so it wears a wide, visible grip
    (`page-edge`) — at the default width it is a few pixels hard against the
    window edge. And `_init_pane_position`, a realize-time idle that applies
    the default 62% split, must SKIP a text page: it fires after the mode is
    set, so applying it put the pages back and left the sheet in a corner.
  - **Quiet time stands in for letting go.** GtkPaned has no "drag finished",
    and the obvious substitute is a trap: `EventControllerLegacy` hands
    PyGObject a **NULL event** for some events, so reading
    `event.get_event_type()` crashes on the first move and the gesture never
    fires at all. `_on_pane_position` debounces instead (`PANE_SETTLE_MS`), and
    three things must gate it or it fires on its own: our OWN moves
    (`_mark_pane_programmatic` — an animation walks through the position that
    means the opposite switch), a window that is not mapped or is still being
    laid out, and a session whose tab has since closed. Miss the second and a
    headless test loop makes blank PDFs until it dies.
  - On a text-first page the pull makes an **untitled temp PDF**
    (`_blank_pdf_file`, shared with `New`/`--new`) — an accidental pull litters
    nothing beside the `.md`.
  - The view is remembered per document in `recent.json` beside the reading
    position (`_recent_full_notes`), for the same reason: it is a view state
    about a document, and a sidecar must not appear because you looked at one.
  - **The CARET crosses with you, both ways (row 162)** — the sheet opens at
    the page you were reading, and closing it turns to the page the caret is
    in. A page index and a character offset are two coordinate systems for the
    same notes, and `note_offset_for_page` / `note_page_at_offset` are the one
    marker table both read: two readings is how the caret comes back on a
    different page than it left. **Two of the answers cannot come from the
    offset**, which is why the session remembers the page the sheet was opened
    at *and* the offset it was opened at (`_full_notes_from` /
    `_full_notes_caret`): a run's shared body (row 129) says the RUN and not
    which of its pages you were reading, and a caret that never MOVED has
    learnt nothing since — which is also the only honest answer for a page with
    no notes, whose caret was parked in somebody else's section. Otherwise the
    caret's own marker wins. **On the way out, `_restore_note()` must run
    BEFORE the jump**: `go_to_page` commits the notes panel, and a panel still
    holding what it had before the sheet opened writes that back over the sheet
    edit. The jump is active-session only, or a background tab's page change
    paints the front tab's chrome. Scrolling the sheet there is
    `TextPageView.scroll_to_offset`, not `scroll_to_mark` — the view is a
    non-scrollable child and holds no scroll of its own.
  - **Replacing the sheet's text destroys every ink anchor.** A text page
    anchors each stroke and image to a `GtkTextMark`, and `set_text` deletes
    every mark in the buffer — so the drawings land in a heap at offset 0. The
    switch changes the text (a PDF's sheet has the page markers, a text page's
    does not), so it cannot be avoided; `_set_buffer_text` carries the anchors
    across. Two things about how, both learned from real ink:
    - It re-anchors the SAME objects (`anchor_records`/`reanchor`) rather than
      reloading them: `load_ink` rebuilds the dicts and clears the ink undo
      stack, so routing this through it makes every Ctrl+Z after a page load
      do nothing.
    - It passes a **`line_map`** (a `difflib` block diff of the two texts), and
      that is not optional. Most ink sits on BLANK lines, which all hash alike,
      so the sidecar's "nearest line with the same hash" rule can only guess
      between them — and it guesses PER STROKE, so a two-line shift moved the
      strokes of one drawing by different amounts and tore it apart. Measured
      on 29 real strokes: 23 changed paragraph and the vertical shift ranged
      over 161 px; with the map, one rigid translation and none changed.
      (The hash rule stays as the fallback — it is what handles a file edited
      outside Sidemark, where there is no old text to diff against.)
    Every path that replaces a notes buffer wholesale must go through
    `_set_buffer_text`.

- **The PDF export writes text in an EMBEDDED font, never base-14** (row 191).
  The built-in PDF fonts are Latin-1, so all 64 glyphs `_MD_SYMBOLS` produces
  and all six `_MD_ACCENTS` marks used to reach the file as `?` — the maths
  lost at the one moment the notes leave the app. `_export_font()` returns the
  `(fontname, fontfile)` pair and **both arguments must be passed**; with no
  font installed it returns the base-14 fallback, so the export degrades to `?`
  rather than failing. `ttf-dejavu` is a dependency for this reason.
  - **Measure with the font you draw with.** `insert_textbox` renders
    *nothing* when the text does not fit its rect, so a height measured in a
    different font is an EMPTY box, not a clipped one — it fails silently and
    only in the file. `_fit_export_box()` is the one helper the callout and the
    text box both size themselves with, `_export_measure_font()` is
    `_NotesWriter`'s measuring twin, and a new drawing call needs its own
    measurement to come from the same place.
  - `_draw_page_marks` is shared with the phone-share render, so this is two
    surfaces: the exported PDF and the PNG a phone sees live.
  - **Subsetting is gated on the export's font count**, not run always
    (`_SUBSET_FONT_LIMIT`). `subset_fonts()` walks every font on every page and
    **holds the GIL throughout**, so a big export froze the window for ~27 s
    even though the export already runs on a worker thread — moving work off
    the main thread does not help here. The saving is flat (~800 KB, our own
    font) while the cost follows the SOURCE's fonts, so past a modest size it
    is pure wait. The limit is set against `STALL_WARN_MS`. It must still run
    after all the text is written, since it keeps only referenced glyphs.

- **HTML comments are hidden by default** (`MarkdownNotesView.show_comments`,
  the ☰ switch, persisted as `show_comments`). Not a special case for ours: a
  Markdown viewer renders no comment, and Sidemark's own per-page bookkeeping
  lives in them — on a sheet showing a whole sidecar they would be most of what
  is on screen. A whole-line comment hides its NEWLINE too, or a hidden marker
  still costs a blank line. Revealed on the cursor line like every other
  marker, rendered as nothing else (a marker is full of things that would read
  as maths), and never removed from the file.

- **`[[wiki links]]` (the linking workflow)** — this is the feature the project
  was designed around and it has shipped (ideas.csv row 99). In notes,
  `[[target]]` is a clickable link (Ctrl+click follows, hover shows a hand).
  `_parse_note_link()` resolves the body into `{path, page, anchor, label}`.
  Rendering keeps the brackets hidden off the cursor line but leaves
  link/`code` contents verbatim — the parsing lives in
  `_notes_to_pango_markup` / `_split_markup` (`_MD_LINK_RE`, negative
  lookbehind so the `![[embed]]` line is left alone). When extending linking,
  keep link targets un-mangled and test both same-doc and cross-doc forms.
  - **A LINK POINTS AT A NAME (row 165)** — a bookmark (row 134) or an outline
    heading, the two things here that have both a name and a page:
    `[[#Eigenvalues]]`, `[[l2.pdf#Chapter Two]]`. That is not a syntax
    preference, it is the fix for the defect: nothing re-keys a `#page=N` when
    a slide is inserted, a chapter is dragged (row 123) or two decks are
    merged, while a bookmark follows its page. **The page forms stay readable
    for ever** — written notes are full of them — they are just never what the
    picker offers.
  - **`document_anchors()` indexes a document that is not open** (headings from
    the PDF, bookmarks parsed straight out of the sidecar's markers), cached on
    BOTH files' mtimes because what changes anchors is often not ours. The OPEN
    document is read LIVE (`_own_anchors`) instead, or a bookmark you have just
    made — precisely the one you are linking to — would not resolve until you
    saved.
  - **`_resolve_note_link` is the ONE resolver**, for following, for the
    strike-through and for the hover preview. Two would drift, and a link that
    looks live and does nothing is the worst of the three.
  - **A dead link is struck through, never under the caret**: a target is
    unresolvable for most of the time you spend typing one. The per-view memo
    EXPIRES (5 s) rather than being invalidated — another window edits what it
    resolves against — and `_populate_toc` drops it explicitly, since that runs
    on exactly the edits (bookmark, rename, outline) a dead link is waiting for.
  - **Discoverability is part of the feature, not a garnish**: ☰ *Link to a
    page…* / Ctrl+K inserts `[[]]` and opens the picker, so the entry teaches
    the syntax by leaving it on screen; ☰ *Copy link to this page* is row 100's
    link-to-here. A `#` in the query switches the picker to that file's names.
  - **The way BACK is part of following one**: `_link_return` remembers where
    the click came from and where it landed, and the notes header shows
    "↩ Back to p.N" only while you are still on the page the link put you on.
    Asking "am I where the link left me?" is what retires the offer by itself
    — no timer, and never a stale jump to somewhere you left ten pages ago.
  - **The picker anchors to the LINE, not to a point** (`_position_link_popup`,
    `halign=START`). Given a zero-height anchor GTK centres the popup on it and
    flips it back over the very text you are reading — the `[[` you just typed.
  - **PREVIEWS ARE DEFERRED (row 166), and the row is worth reading before
    rebuilding them.** Hovering a link and `![[embeds]]` both shipped and were
    both pulled the same day after a GTK abort ("Byte index N is off the end of
    the line") in the notes editor. **That abort is now understood and fixed**
    — it was `get_iter_at_location`, not the previews (see the crash note under
    Logging) — so the precondition for rebuilding them is met. The design that
    survives is in row 166, including which parts were sound: the marker, not
    the caret; a tag's `pixels-below-lines` rather than a paintable; a tooltip
    rather than a popover.
  - **`![[target]]` is an EMBED: the same link, showing the page it leads
    to**, under its line (`_MD_EMBED_RE`, `_sync_inline_preview`,
    `_snapshot_inline_preview`). Obsidian's syntax for exactly this, and
    already Sidemark's own — a sidecar's first line is `![[lecture.pdf]]`.
    `[[!target]]` would be neither, and was asked for; say so. **The MARKER
    decides, never the caret**: a preview that came and went as you moved
    through the text could not be read. `_MD_ANY_LINK_RE` is what most of the
    grammar wants (both forms follow, both render their body verbatim); the
    `!` only changes how many brackets are hidden and whether a page is drawn.
    The picker INSERTS the embed form — you asked for a link to a page, and
    the page is what you meant — and typing `[[` yourself keeps the plain one.
    - The space is a **tag** (`pixels-below-lines`, one per height via
      `_gap_tag`), so the notes below MOVE DOWN and the picture never lands on
      your writing. It cannot be a paintable in the buffer, which is what
      "render it like `\alpha`" would mean: symbol rendering swaps text for
      text, while a paintable is a real character — and this buffer IS the
      `.md` source, spliced back through a character-to-character index map.
    - Renders are memoised on the target string (`_rehighlight` runs per
      keystroke, over every line) and dropped by `forget_previews()` when the
      document changes. One per LINE — a second embed on the same line is just
      a link, because a line has one gap.
    - The sheet is the same widget, so text-first mode gets it unchanged.
  - **Every other link answers on HOVER, and that preview is a TOOLTIP
    (`query-tooltip`) — not a shortcut.**
    A popover is a real surface: shown near the pointer it takes the crossing,
    the view gets a LEAVE, the preview hides, the pointer is back on the link
    — and it oscillates. The symptom was a preview that appeared once in a
    while, mostly when you clicked. Tooltips keep away from the pointer by
    construction, never take it, and time themselves. `set_tip_area` is what
    stops it re-rendering the target page on every motion event.
  - *Not built: backlinks (row 100 item 4) — it needs a cross-file index and an
    invalidation story, and should wait until naming targets proves itself.*

- **Linked page notes (row 129)** — a page can CONTINUE the one before it, and
  a run of linked pages shares ONE body stored once on the run's first page
  (`NotesModel._links`, `run_start`/`run_pages`/`run_end`; the sidecar marker
  is `<!-- page:13 continued -->` with an empty body, the one case where an
  empty page is still written out). **The trap is which text you ask for**:
  `get(idx)` RESOLVES through the run — for the human looking at that page —
  while `own_text(idx)` is what the page stores and is `""` on a continued
  page. Everything that walks all pages (export, share render, page marks,
  search) must read `own_text`, or a run prints on every slide instead of
  once. `set(idx)` writes to the run start, so undo, the merge import and the
  drag-export need no special casing. Re-keying degrades to UNLINKED rather
  than re-linking two unrelated slides; the load-bearing one is
  `shift_for_delete`, where deleting a run's START hands the body to the next
  page in the run instead of dropping the whole run's text. PDF-only, and the
  UI is one checkbox in the notes header (`_update_notes_link_ui`). A run of
  pages that carries nothing else is written as ONE **range** marker
  (`<!-- page:13-40 continued -->`) — the fact is about the run, not about each
  page; the reader expands a range and still accepts the per-page form, so old
  files need no migration, and a bookmark (a property of ONE page) breaks the
  range and takes its own marker. **The tick
  CASCADES**: `link_forward` carries the run through every following page that
  has nothing of its own, stopping at the first page that already has notes
  (`link()` would append it into the run — the one outcome you cannot see
  coming from a checkbox); `unlink_forward` breaks the run at that page and
  every page after it. One tick covers a run of slides, one untick undoes it.
  **A page inserted INSIDE a run joins it** (`shift_for_insert`), and that is
  not a nicety: the body is stored once, so breaking the link at the gap does
  not split the notes in two — it DELETES them from every page after the
  insertion point, and the pages look fine until you read them. It shipped that
  way in both apps, asserted by a test phrased "the tail is cut loose", which
  reads like a decision until you count the pages it empties. The question is
  asked BEFORE the shift and can only be true when the old page at `idx` was
  itself continued; at a run's START the blank pages precede it and the run just
  moves. The invariant to test re-keying against is **no page loses its text**,
  never "page N equals page M" — an insert inside a run legitimately gives the
  NEW page the run's text too, and an equality has to be relaxed to allow that,
  which is exactly how the loss went unnoticed.
  **Ctrl+Z reaches it**, as a `("links", snapshot)` op on the shared
  `_undo_timeline` — a whole-model `snapshot()`/`restore()`, because linking
  MERGES two bodies and no page/text pair describes that. A snapshot is keyed
  by page index, so any re-paging (insert/delete/reorder) DROPS the link ops
  (`_drop_link_timeline_ops`) rather than replaying them onto moved slides.

- **A viewport SCROLLS TO ITS FOCUS WIDGET, and the sheet's is the whole
  paper.** `TextPageView`'s Box wrapper makes the text view a non-scrollable
  child, so `GtkScrolledWindow` wraps it in a `GtkViewport` — whose
  `scroll-to-focus` defaults to TRUE. The only focusable child is the
  full-height view, never fully visible, so revealing it means jumping to the
  top of the paper. It fires on a focus CHANGE, which is why it hid behind the
  tool switch (picking a tool focuses the button; GTK's press handler then
  calls `grab_focus()` on the view) and why the tool code looked innocent. The
  sheet moved mid-press and GTK resolved the pointer against the moved view,
  landing the caret pages from the click. Turned off in `__init__` — if you
  ever re-wrap the sheet, turn it off again.

- **`buf.get_text(…, include_hidden_chars)` must be TRUE wherever the text is
  compared against an OFFSET.** A `GtkTextIter`'s line offset always counts the
  invisible characters; text fetched without them is a shorter string, so the
  offset points somewhere else in it — one place per hidden marker earlier on
  the line. The notes editor hides markers on every line the caret is not on,
  which is *most* lines, so this fails exactly where the feature is used and
  works while you test it with the caret in place. It killed two things at
  once (row 165): `_link_target_at` could not match `[[…]]` at all once the
  brackets were hidden — no hand cursor and no Ctrl+click — and
  `_cursor_line_and_col` shifted the `[[` query, so the picker stopped opening
  on any line that already had a link or a rendered symbol. Read it back the
  way the buffer stores it, or convert the offset first; never mix the two.

- **A GtkTextMark displaced by an EDIT moves without a `mark-set` signal.**
  Rewriting a line under the caret (`_buf_replace_line`, how the live-Markdown
  renderer un-renders `α`→`\alpha`) deletes and re-inserts it, and `insert` +
  `selection_bound` sit inside that range: the delete collapses them onto the
  line start and the re-insert drags them to the line END on their right
  gravity. Any such rewrite must carry both marks across by hand — save
  COLUMNS (ints) before, re-derive iters after, since the edit invalidates
  every iter. Row 128 is what this costs when you don't: the caret jumped, and
  because a pen press always jitters, GTK's follow-up drag-update moved
  `insert` alone (`move_mark_to_pointer_and_scroll`) and a plain click became a
  selection running to the end of the text. Two lessons for debugging it: a
  mark that moves with no `mark-set` is riding an edit, and instrumenting the
  real app was the only thing that showed it — every static reading of the
  handlers tested clean.
  **It is EVERY mark on the line, not just those two.** GtkTextView anchors a
  live double/triple-click selection drag to a pair of anonymous marks and
  re-derives the selection between them on each motion event — collapsed by
  the rewrite, they re-select nothing, so a selection made over rendered maths
  disappeared a moment later with no signal naming the culprit.
  `_buf_replace_line` walks the line by COLUMN collecting marks (never with
  `forward_char`, which reports failure when it lands on the buffer's end
  iterator — a real position, and the one the last line's marks sit on).

- **The sheet and the notes view never call a TRAPPED `GtkTextView`
  geometry/iter API raw** (row 181). Each one is wrapped exactly once and the
  wrapper's docstring holds the trap; the full list, and the sweep of the APIs
  found NOT to need one, is the rule comment above `iter_at_buffer_xy`. Today:
  `iter_at_buffer_xy` (the process abort), `line_bounds` (display vs logical
  line), `_buf_replace_line` (marks riding an edit), `_set_buffer_text` (marks
  = ink anchors), `_overlay_to_buffer_f` / `_buffer_to_overlay` (int
  truncation). The reason it is a rule: this buffer IS the `.md`, rendered in
  place, so nearly every line carries invisible characters and every paragraph
  wraps — precisely what those APIs are careless about, and they fail silently
  or fatally rather than raising. Adding a wrapper is cheap; finding out you
  needed one costs a session.

- **Geometry you STORE must not go through the int-truncating coord helpers.**
  `window_to_buffer_coords`/`buffer_to_window_coords` only take ints, so a
  per-point conversion rounds every point on the way in *and* out. Invisible
  for a move (all points shift alike); it made a *rotated* stroke go lumpy, and
  compounded on every re-anchor. `TextPageView._overlay_to_buffer_f` /
  `_buffer_to_overlay` take the origin once and add the float delta — use them
  anywhere the result is persisted. Plain `_overlay_to_buffer` (int) is for
  hit-testing only. Symptom to recognise: shapes degrade a little per edit.
  A neighbouring trap: **`translate_coordinates` returns the POINT** — or
  nothing at all when the widgets share no root — never a `(ok, x, y)` triple,
  whatever the C signature suggests. Unpacking three raises at the first call.
- The codebase favors long, explanatory comments about *why* (and records
  hard-won platform quirks inline) — match that style. The test is whether the
  comment still earns its space once the change is old: an invariant, a
  constraint the platform imposes, or a trap does; the *story of the edit* does
  not. Write "this must come from `_selection_bbox()` or the frame drifts from
  what a grab hits", not "this used to be recomputed from the strokes". Present
  tense about how it behaves, not past tense about how it got here — the
  sequence is `ideas.csv`'s and git's job.

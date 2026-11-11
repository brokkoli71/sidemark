# Pages: navigation, bookmarks, search, the strip

> Everything keyed to a page index.

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

- **View history — back / forward through where you were (row 195).** ONE
  `ViewHistory` per surface (`PDFCanvas.history`, `TextPageView.history`), so
  one per tab; Alt+Left/Right, Escape and the mouse side buttons all walk it.
  It records only what calls it, so the rule is at the CALL SITES:
  - **A user movement calls `history.moving()` BEFORE it changes the view**
    (scroll, pan, pinch, zoom, page flip, a scrollbar press). A run of calls
    without a `VIEW_HISTORY_BURST_S` pause is one step — call it on every
    update of a long gesture too, or a slow drag splits into several.
  - **A discrete jump calls `history.jump()`** (links, `window._go_to_page`
    — thumbnails, outline, bookmarks, undo landing on another page — search
    hits, zoom-to-region, fit).
  - **Anything the APP does calls neither**: open (including reopening at the
    remembered page, which is why that path calls `canvas.go_to_page`
    directly), resize re-fits, reload, the phone driving the page. A new
    movement path that forgets its call is invisible to back; one that calls
    it from an automatic path fills the history with places nobody went.
  - Restores run through `set_state`, i.e. ordinary movement code;
    `_restoring` is what keeps a back from recording itself.
  - Side buttons: walking the history is always browser-style (8 back, 9
    forward); page turning has its own swap (Settings → "Swap for page
    turning", default on: 8 = next, as through 0.7.0). Settings picks pages or
    history, Ctrl gives the other; a text page has no pages, so there it is
    always history. `side_button_step(btn, pages)` alone decides the direction
    — the window and the projector both ask it.

- **Link preview on hover (row 197)** — resting on an internal link, where a
  click would follow it (`link_hover_active`), draws what it points at on the
  canvas after `LINK_PREVIEW_DELAY_MS`. Drawn by `_draw`, NOT a popover (rule
  2, and it follows zoom and pan for free); cleared by leaving the link, a
  press, following it and a page load.
  - `_link_destination` is THE resolution of a link, shared by following and
    previewing. **PyMuPDF reports a NAMED destination — every LaTeX `\cite`
    and `\ref` — y-UP, a GOTO y-down**; it converts the named one through the
    target page's `transformation_matrix`. Read raw, a citation lands at the
    mirror height of its entry.
  - `link_preview_clip(page, to, kind)` — `kind` is `link_target_kind` of
    the hyperref destination NAME (`cite.*`, `figure.*`/`table.*`,
    `equation.*`/`AMS.*`): the name says what the target is, which no layout
    rule can. It works on LINES grouped into ROWS, never blocks (PyMuPDF
    splits a hanging first line into its own block, revtex's justified lines
    into pieces), and on a two-column page only in the column the point is in
    (`_page_gutter`, found from body-width lines only). A citation stops at
    the next label row; an equation where text resumes at the column edge; a
    float is found by its caption (`_float_clip`: from a hypcap anchor down
    to the caption's end, or from a caption anchor up through graphics and
    the text inside them); a rule touching the strip walks a table
    (`_walk_table`). Padding stops at the neighbouring row.
  - **Sharp at fractional scaling:** the card is rendered at `_device_px`,
    the WINDOW SURFACE's scale (`_device_scale`: 1.5 at 150 %). Neither the
    cairo context `_draw` gets (GTK records it and rasterises later; its
    device scale reads 1.0) nor `get_scale_factor()` (rounds up to 2) says
    it. The card is painted at a device-pixel corner of the screen
    (`snap_to_device`, from `_surface_origin`). Either one off and the text
    is resampled soft. Rendering cost is the page's vector content, not its
    resolution (an entry ~2 ms, a dense six-panel figure ~60–100 ms at any
    density), so there is no low-res-first pass.
- **The hover card (rows 197/199/200)** — ONE card at a time,
  `PDFCanvas._link_preview`, typed "link" (an internal target, rendered),
  "ref" (a web link to an article) or "backlinks" (pinned). A hover card stays
  while the pointer is on its link OR on the card; `CARD_HIDE_GRACE_MS`
  covers the gap between them, and `_update_link_hover` leaves everything
  alone while the pointer is on the card (a link under it is covered). A
  pinned card stays until a press elsewhere, Escape (window `_on_key`, before
  the history step), or a page change. **Every press on a card or a backlink
  icon is the card's** — `_card_press` runs in `_on_drag_begin` before the
  tool router; a press elsewhere CLOSES the card and does nothing else (no pen
  dot from a dismissing click) — except on the card's own link, which still
  follows.
  Clicking a link card's rendered target follows the link (`follow`).
- **Backlinks (row 199)** — who links HERE. `_backlink_index` maps a link
  KEY (`_link_key`: the destination name when there is one — every `\cite`
  of a key shares it — else page + height) to every source link, built once
  per document on first use (~25 ms for a 22-page paper) and rebuilt when the
  page count changes. A link preview's footer says "Also cited on p. …";
  every target on the page with sources wears a faint "↩ n" icon at its left
  (`_draw_backlink_icons`; regions from `_targets_on_page`, found on an IDLE
  the first time — tens of ms a draw must not spend). Clicking it pins the
  list; a row jumps via `follow_goto` (a history step) and flashes the
  citation. No icons on the projector canvas (`interactive=False`), while
  presenting, or with Settings → Reference badges off (`PDFCanvas.show_badges`,
  row 208, read once at start-up); hidden badges keep no hit boxes.
- **Reference lookup (row 200)** — Settings → *Look up cited papers*
  (`reference_preview`, OFF by default: it sends the link to outside
  services). Only a link IN the document is looked up — the URI inside the
  cited entry (`_entry_reference_uri`, arXiv/DOI first) or the hovered web
  link — never a title search. `fetch_reference` (arXiv API / Crossref /
  Semantic Scholar to fill gaps / a page's `citation_*` meta tags; never a
  `.pdf`) runs on a worker thread through `REFERENCES`, which caches answers
  on disk (`REF_CACHE_DIR`) and misses for `REF_MISS_TTL_S`. Parsers are pure
  and take an injectable `get`, so tests never touch the network. The User-
  Agent names the app only.

- **Zotero (row 205)** — active only when the open PDF sits under a Zotero
  data directory's `storage/` (`ZoteroLibrary.find_for`). Zotero locks
  `zotero.sqlite` while it runs (a plain read-only open is refused), and a
  new item sits in its WAL until a checkpoint. So when the database OR the
  WAL changes, both are COPIED aside and the copy read (`_open_snapshot`);
  an immutable open of the live file is only the fallback — it cannot see
  the WAL. The page re-stats the library at most every 2 s while drawn and,
  if it changed, re-matches its entries (`_zotero_targets` cleared), so a
  paper added in Zotero turns its "i" into a Z. A library paper's card also
  shows its Zotero NOTES (row 210; `zotero_note_text`, newest first, trash
  left out, at most `CARD_NOTES`) — read-only: Zotero offers no local way to
  edit a note (writing its database while it runs would be overwritten or
  corrupt it; only the zotero.org web API edits), so "Show in Zotero" is the
  way to change one. "Show in Zotero" opens the paper IN A COLLECTION
  (`ZoteroLibrary.select_uri`): the one selected in Zotero now (asked of
  its connector, `zotero_selected_collection`, off the main loop) if the
  paper is in it, else one it shares with the paper being read
  (`item_for_pdf`), else its first; an unfiled paper can only be shown in
  the library.
- **A paper card is as few lines as say it (row 211)**: title (click: the
  paper), who/where/when, TL;DR, Zotero notes, ONE action row (`actions`
  segments laid out by `_layout_actions`: the Z badge, Abstract, PDF / Web,
  Show in / Add to Zotero), the abstract once opened. Once the paper is
  known the rendered entry gives way to one line, "[81] · also cited on
  p. …", which goes to the entry; it stays the card while the paper is
  unknown and for figures, tables and equations. Matching (`ZoteroLibrary.match`): DOI, arXiv id, whole title in the
  entry, then volume + first page + year + first author for title-less
  physics entries. A match fills the card's paper section locally (no
  lookup, no switch) and adds Open the PDF (`on_open_document` → a tab in
  the document's window) and Show in Zotero (`zotero://select/…`). A cited
  entry in the library also wears a red "Z" left of its ↩ icon (matched once
  per page in `_prepare_targets`, `_zotero_targets`); clicking it opens the
  PDF. Every OTHER cited entry wears an "i" there instead (never both):
  `_open_info_card` builds the card a hovered citation shows
  (`_build_link_card`, with every citing page in its footer), pinned. It
  fakes the link as a GOTO already in page space plus a `_key` — as NAMED its
  point would be converted a second time. **A link belongs to an entry only if its MIDDLE is inside the entry's
  strip** (`link_inside`): strips are padded, and the next entry's link
  reaching a point in made [60] look up [61]. A link straight to a `.pdf`
  is never looked up (`reference_lookupable`). **A citation's link and its
  Zotero match are looked for in the WHOLE entry** (`link_preview_clip(…,
  max_h=page height)`; `_targets_on_page` stores cite regions that way too):
  a 70-author entry's DOI sits past any height cap. What the card SHOWS of
  it is at most `CITE_PREVIEW_MAX_H`, fading out when cut (`cut`).
- **Add to Zotero (row 209)** — offered on a paper's card when the document is
  in Zotero, the paper was identified by a lookup and is not in the library.
  Through Zotero's CONNECTOR server (`ZOTERO_CONNECTOR`, 127.0.0.1:23119,
  what its browser extension uses): `saveItems` with one item whose `id` is
  the connector key, then — for arXiv, the one place a free PDF is known —
  the PDF downloaded and uploaded to `saveAttachment` with an `X-Metadata`
  header (`sessionID`, `parentItemID` = that `id`, `title`, `url`). Checked
  against Zotero's own server_connector.js: `saveItems` answers 201 only
  after the item is saved, so the attachment always finds its parent. Never
  exercised against the real library in tests (`post`/`get` are injected).
- **Card and icon size follow the PAGE** (row 207): `_page_f()` (the page
  zoom, clamped 0.55–2.5) scales the cards' text, row heights and widths
  and the ↩ / Z icons. The rendered target strip keeps its own readable
  scale (`_preview_scale`).

- **Page backgrounds (rows 139 + 140)** — `draw_page_background` rules a blank
  page (plain/lines/squares/dots) into the page CONTENT at creation: a
  background you write on is one you hand in, so it must print and export.
  `blank_pdf_file()` is THE blank page — New, `--new`, row 130's edge pull and
  `add_blank_page` all come from it, so a document does not go plain at page 2.
  Note `insert_page` cannot do this at all (it makes an *empty* page and the
  ruling is content), which is why an added page is built by cairo at the
  current page's size and merged with `insert_pdf`. *ceiling: fixed at creation
  — re-ruling later needs the row 118 optional-content machinery to know which
  pages are ours, and you can just make the page again.* PDF-only so far (the
  text sheet was offered and declined).

- **Merge import (row 123)** — dropping several documents at once makes ONE
  document with a chapter per file. `merge_documents()` is the single pipeline
  behind both entry points (thumbnails → insert chapters at the gap; window →
  "Open All / Merge…" into a new file). Notes and image sidecars are re-keyed by
  each chapter's page offset; ink needs no code (strokes are native PDF
  annotations). **Before touching it, read row 123's traps** — the load-bearing
  one is that a source's image layer MUST be stripped before `insert_pdf`, or
  every pasted image renders twice forever (`take_source_images`). Chapters
  reorder by dragging an outline row: `chapter_spans` → `move_page_range` (one
  `select()`, one re-key) → `resort_toc` (`select()` re-pages the outline but
  does not re-order it). A drop lands the same way wherever it falls: the
  sidebar (outline rows, thumbnails, the empty space below either) imports at
  that gap, everywhere else — including a text-first page — opens or merges
  into a new file.

- **Bookmarks (row 134)** — a page can be marked, and the marks live in the
  SAME sidecar marker as the row-129 link flag: `<!-- page:13 bookmark -->`,
  `bookmark="Eigenvalues"` when renamed, composing with ` continued`. A name is
  escaped on write, and escaping `>` is what makes `-->` unrepresentable, so a
  name can never terminate the comment it lives in. **A bookmark needs no
  adjacency rule** — that is the whole difference from a link: a link is a
  relationship BETWEEN pages so every re-key must ask whether it survived, a
  bookmark is a property OF one page so it just follows it (and a deleted page
  takes its bookmark with it). **The label is derived, never stored** (the
  page's first `own_text` line, else its chapter, else nothing) so editing
  notes cannot leave a label describing what used to be there; renaming stores
  one that then wins. `_sync_bookmark_chrome` is the single place deciding
  where the verb lives — the header button when there is room, the ☰ menu entry
  when the header has collapsed, never both — and `_new_bookmark_list` is the
  single builder behind both list widgets. PDF-only.
  - **Making one and destroying one are both a moment, and both are handled
    there** (row 152). Adding opens the name field with the derived label in it
    and SELECTED (`_prompt_bookmark_name`), so the first keystroke replaces it
    — a bookmark you must go and rename later is one you name never. **The
    field IS the add**: nothing touches the model until Enter, so Escape leaves
    the page unmarked and the file not even dirty, and there is no
    create-then-undo to get wrong. (Storing the mark on the click and letting
    the popup edit it kept Ctrl+B a stricter one-key verb, and was rejected in
    the hand.) The header toggle flips itself on the click that opens the
    field, so cancelling must put it back; and with no chrome on screen to
    anchor the popover to, the bookmark is created UNNAMED rather than silently
    not at all. Committing the suggestion unchanged stores NO name, or the
    derived label would freeze into the file. Removing asks first, through one confirmation
    every path routes into (`_drop_bookmark` asks, `_do_drop_bookmark` acts) —
    with no "don't ask again", unlike the page-drop dialog: the name is stored
    nowhere else, and an opt-out is one stray click from losing the guard for
    good. A cancel must re-sync the header toggle, which already flipped
    itself on the click that opened the dialog.
  - **A second click RENAMES, it does not remove.** The page is already marked,
    so the useful verb is "say what this is" — the list opens scrolled to that
    page with the name selected. A stray click on a toggle must not raise a
    confirmation dialog. In the outline a **double-click** renames: F2 works
    too, but only once the list has keyboard focus, and a single click
    activates the row and takes focus with it, so by hand the key alone meant
    "click, click again, then F2". A rename's **focus-leave is delivered after
    the fact**, so `finish` is guarded by that edit's own token AND checks the
    entry still has its box as parent — a list rebuilt in between otherwise
    leaves it removing a child from a box that no longer owns it. **And the
    leave DEFERS to an idle**: ending the edit destroys the very widget GTK is
    moving focus out of, and doing that inline makes GTK go on walking a
    widget with no parent — `gtk_widget_get_parent` assertions without end
    (millions, not a handful). Enter and Escape are not inside a focus change
    and stay immediate.
  - **Where you are in the outline is a LINE when no entry names your page**
    (row 153). On an entry's own page that row gets the solid bar and a bold
    title; anywhere else a rule carrying the page number is inserted BETWEEN
    the two entries you fall between, and the containing entry keeps only a
    faint tint (a tint alone was too little to find while presenting). The line
    counts every row — chapters, sub-entries and bookmarks alike — because "the
    entry above me" is whatever is actually above me in the list; ignoring ★
    rows would point at a chapter several screens up. It is inserted and
    removed, never moved, so a stale second line is impossible, and it is
    neither selectable nor activatable: a row you could click would be a
    destination that does not exist.
  - **The document's OWN headings rename and delete the same way** (row 159):
    double-click, or right-click for both verbs. The sidebar is one list to the
    reader, so `_begin_rename` no longer knows what it is renaming — the ROW
    carries `_rename_read`/`_rename_write`, and `_attach_row_menu` builds the
    menu for both kinds. A heading writes into the PDF's outline (`set_toc`),
    so it dirties the FILE. An empty title is refused (a heading with no text
    is unreachable in other readers, and deleting is its own verb), and
    deleting **promotes the subtree by one level explicitly** — leaving that to
    `normalize_toc` promotes only the first orphan and hangs its siblings
    under it. The menu also changes an entry's PAGE and adds a heading or
    sub-heading **on the page of the row you clicked**, never the page you are
    viewing; a sibling lands after that row's own children. A double-click
    opens title and page together, the page field standing IN PLACE OF the page
    number (a row showing the page twice makes the untypeable one look
    editable), and both are written in one `set_toc` — separate writes rebuild
    the sidebar under the edit that is still running. **A move between the two
    fields is not "clicking away", and testing that needs the WINDOW's focus
    widget**: a `GtkEntry` delegates focus to an internal `GtkText`, so
    `entry.has_focus()` is False even while you type in it — the guard silently
    never fired and clicking the page field ended the rename.
  - **Bookmarks are outline entries too, and can BE the outline** (row 153).
    The Outline/Pages switch appears when the document has a TOC *or*
    bookmarks — a lecture deck rarely has a TOC and is exactly what you
    bookmark your way around — and a "Bookmarks" tick-box appears beside it
    only when there are some (persisted app-wide as `outline_bookmarks`). They
    are **merged INTO the outline's own order**, never sorted with it: each one
    is emitted before the first entry starting after its page, and a bookmark
    on the same page as an entry follows it. Re-sorting the whole list by page
    looks identical on a well-formed outline and silently hands a chapter drag
    the wrong span on one whose entries are out of page order, because
    `chapter_no` indexes `chapter_spans` in TOC order.

- **Search finds the FIRST match synchronously and the rest in the background
  (rows 154 + 155).** A keystroke may spend `SEARCH_SYNC_MS`; the remaining
  pages go to the idle loop in `SEARCH_CHUNK_MS` slices — scanning every page
  before returning is most of a second per character on a large PDF, and the
  next keystroke throws it all away. Pages are scanned from where you ARE,
  forwards, then wrapping, so the first hit found is the one you would have
  gone to anyway. What holds it up:
  - **The match list is ordered by PAGE and rebuilt as hits arrive**, never in
    the order the scan found them, and the current match is re-found by
    IDENTITY (`_rebuild_search_matches`) — a hit landing ahead of it renumbers
    the label without moving you.
  - **The count says when it is still climbing** (a trailing `…`) and **"not
    found" waits for the scan to finish**, or every long document flashes red
    at a term that is in it.
  - **Stepping off the end of a partial result set finishes the scan first**
    (`_step_search` → `_finish_search_scan`): wrapping to match 1 with pages
    unread silently skips everything between here and the end.
  - A tail of `SEARCH_SYNC_TAIL` pages is finished on the spot — deferring it
    costs more than doing it, and it keeps an ordinary document's count final
    the moment you stop typing.
  - **The idle slice reads its state through the ACTIVE-session proxies**, so
    it parks when its tab goes to the back and `_activate_session` restarts it
    (`_scan_session` is the check). Without that, a background tab's scan
    writes its hits into the front document's table.
  - **A notes hit is not scrolled to by `scroll_to_iter` on the sheet** — the
    sheet holds no scroll of its own, so `_select_note_match` takes
    `TextPageView.scroll_to_offset`; and on a PDF's full-notes sheet the hit's
    offsets are into ONE page's notes while the buffer is the whole sidecar, so
    they are rebased through `note_offset_for_page(run_start(page))`.
  - **Ctrl+F keeps the term and SELECTS it** rather than clearing: typing
    replaces it, Enter searches it again. `grab_focus` selects only when focus
    ARRIVES, so the explicit `select_region` is there for the case the feature
    is *for* — pressing Ctrl+F with the caret already in the entry. `_hide_search`
    keeps the text and drops the results, which is why Enter on a reopened bar
    re-runs the search instead of stepping through nothing.

- **The page counter and the bookmark toggle belong to the WINDOW, not the tab**
  (row 156), so `_sync_page_chrome()` is the one place that points them at the
  active document's page — called by the canvas's page-change callback *and* by
  `_activate_session`. Every structural change (add, delete, insert, reorder,
  merge) fires the callback through `PDFCanvas._load_page`, so those were never
  the problem; a TAB SWITCH changes which page is in front without any page
  changing, and so fires nothing at all.

- **The thumbnail strip selects like a text editor** (row 171): SHIFT marks the
  region from the anchor to the clicked page, CTRL adds or removes one, and a
  plain click starts again from there — one anchor shared by both, so Ctrl to
  pick a start then Shift to take the run works, and Ctrl+Shift adds a second
  region. It is ONE capture-phase handler (`_on_thumb_select_pressed`) and
  cannot be left to `GtkListBox`: the row carries a `DragSource` in the same
  phase which swallows a stationary press, so Ctrl never deselected and Shift
  never reached the listbox's range code at all. A Shift press is CLAIMED, so
  the row is not activated — marking a region is not a request to go to its last
  page — while a plain press falls through and still turns the page. The anchor
  is per session and dropped by `_populate_thumbnails`, since the rows it
  pointed into are gone.

- **Hidden pages (row 158)** — a page can be set aside: still in the document
  and still editable, but **skipped when paging, skipped when presenting, and
  left out of an export** (the PowerPoint "hide slide" meaning, not a sidebar
  filter). Right-click a thumbnail for Hide/Unhide. The flag lives in the same
  page marker as the row-129 link and the row-134 bookmark (` hidden`) and
  **ranges** like `continued` does — hiding a block of slides is one fact about
  a run — so the coalescing rule is now "same attributes, no body, not
  bookmarked" rather than a special case; a bookmark still breaks a range
  because it names ONE page. Like a bookmark it needs no adjacency rule.
  - **Skipping is resolved ONCE per navigation.** `_flip_visible` is the single
    entry that consults `next_page_for` (scroll-past-edge included);
    `_flip_page` takes a plain delta. Resolving in both is how paging from 1
    over a hidden 2–4 lands on 7 instead of 5.
  - **Only RELATIVE navigation skips.** A thumbnail click, a link and a
    bookmark still open a hidden page — that is what keeps it reachable and
    editable, and the dimmed row in the strip is the only way to select it
    again and bring it back.
  - The canvas has no notes model, so it asks the window through
    `next_page_for`, which takes the **session** rather than reading the
    active-tab proxies.
  - `_pages_acted_on` is the one rule for which pages a per-page verb applies
    to — the multi-selection when the clicked row is in it, else that row alone
    — shared with the drag-export so the two cannot disagree. The menu offers
    the verb that CHANGES something (Hide unless all are hidden), so a mixed
    selection needs no thought. PDF-only, like bookmarks.

- **Reopen where you left off** lives in `recent.json` (`_recent_page` /
  `_remember_recent_page`), NOT the sidecar: a sidecar appears only once you
  write something, and storing the reading position there would drop a `.md`
  beside every PDF you merely glanced at. Two traps: loading fires a page-0
  change, so `_restoring_page` stops it erasing the position it is about to
  restore, and the write must NOT re-order the list or "recent" comes to mean
  "whatever tab I scrolled in last".

- **An empty launch reopens the LAST DOCUMENT, and the scratchpad is the
  recents list's last resort (row 161).** `_open_last_document` walks
  `recent.json` and stops at the scratchpad entry — nothing below it is newer.
  Which makes the list the ONLY way into the scratchpad, so
  `_seed_scratchpad_recent` (called from `open_new_window`) creates the file
  and seeds it at the **TAIL**, never the front — at the front it would be "the
  last document" and every launch would reopen it, which is the behaviour this
  replaces — and `_add_recent` **pins** it against `RECENT_MAX`, dropping the
  oldest ordinary file instead. `_scratchpad_path()` is the one name for it.
  **"Recent" means last USED, not last opened**: every close refreshes the
  entry (`_touch_recent`, from `_remember_closed` for a tab and from
  `_destroy_all` for a window — oldest first, ACTIVE tab last), or opening A
  then B and working in A hands you back B.
  **And the walk SKIPS what is already on screen** (`_documents_open_elsewhere`,
  every window of the app): a second bare launch is a request for ANOTHER
  document, and the newest recent is by then the one in front of you. If the
  scratchpad itself is open, the fallback is a fresh blank sheet rather than a
  second view of one file — two windows saving one `.md` loses writing.

- **The thumbnail strip scrolls for a REASON, never as a side effect.** Opening
  it, or turning the page, brings the current page into view; a rebuild
  (`_populate_toc` runs on every bookmark, link, rename) puts the strip back
  exactly where it stood — `_thumb_centred_page` is the "is this a new page or
  the same one?" answer, and a document change clears it. Both paths go through
  a RETRY (`_scroll_thumb_into_view` / `_restore_toc_scroll`): the reveal is an
  animation and a rebuild has no allocation yet, so at the instant you are
  asked every row reports y=0 and the adjustment has nothing to scroll — which
  is why the strip used to open at page 1 whatever page you were on.

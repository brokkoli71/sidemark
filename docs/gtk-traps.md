# GTK traps

> Handlers that are correct and never run, and widgets that outlive their signal.

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

- **Wayland file DnD** needs `Gtk.DropTargetAsync` + a drag-motion handler
  returning an action, or the drop never fires (portal transfer).

- **GTK4 popovers**: never popdown one popover and popup a sibling on the same
  widget synchronously — defer to the "closed" signal. And **tear one down with
  `_drop_popover`, never a bare `unparent()` in its own `closed` handler**:
  `unparent()` can itself emit `closed`, so the one-liner re-enters and
  unparents a widget that no longer has a parent. That prints
  `gtk_widget_get_parent: assertion 'GTK_IS_WIDGET (widget)' failed` and is not
  fatal, which is exactly why it ships.

- **cairo's `save()`/`restore()` does not save the PATH.** A painter that ends
  with `show_text()` leaves a current point behind, and the next `arc()` joins
  onto it with a straight line — which is how a ghost line appeared from the
  snap label to the snap ring. End a shared painter with `new_path()`, and
  start an arc with `new_sub_path()`.

- **Never destroy a widget from inside the signal that is acting on it.** A
  `focus-leave` handler that removes the entry it belongs to (and rebuilds the
  list holding it) leaves GTK walking a widget it has just unparented — a
  `gtk_widget_get_parent` assertion **per iteration, for ever**: 3.5M lines and
  a killed process in one measurement, not the handful you see scroll past.
  Defer the teardown to a `GLib.idle_add`; the signal itself must only note
  what to do. **And a deferred widget is still live until then**: when its
  successor takes focus it emits `focus-leave`, so a leave handler must check
  it belongs to the CURRENT widget (`FormFieldEditor._focus_left`) or it acts
  on the new one. The same shape hides in "closed" handlers (see the popover note
  below) and in any `clicked` that repopulates the list its button sits in.

- **Event reachability — a correct handler can still never run.** When a
  gesture "does nothing", test the PATH, not the handler (all of these tested
  fine in isolation while being unreachable in the app):
  - **A `GtkTextView` installs its own `GtkDropTarget` for `gchararray`**, and
    a file manager offers `text/plain` beside the uris — so the editor matched
    first and every file dropped on the notes panel or the text sheet
    disappeared, while the window's own target sat there working. Two targets
    on one widget are tried in the order they were ADDED, so the built-in has
    to be *replaced*, not supplemented (`attach_file_drop`).
  - `GtkScrolledWindow` installs its OWN capture-phase scroll controller and
    STOPS scroll it can use, so a Ctrl+scroll zoom must be captured on an
    ancestor **above** it (`MarkdownNotesView.attach_zoom_scroll`,
    `TextPageView._on_sheet_scroll`). The ScrolledWindow itself is too late —
    same widget + same phase run in add order and GTK's is first.
  - A drawing tool makes the ink overlay the event **target**
    (`ink.set_can_target`), which cuts the ScrolledWindow out of the path
    entirely — so `TextPageView` owns plain scrolling too, not just zoom.
    **Which is why the sheet has to coast on its own (row 175)**: GTK's kinetic
    scrolling belongs to the ScrolledWindow that never sees the event. The
    `KINETIC` flag on our controller is the whole of the "touchpad only" scope
    — GTK emits `::decelerate` only after a CONTINUOUS scroll, so a wheel notch
    keeps its instant `WHEEL_PAN_STEP` — and it hands over a velocity and
    nothing else, so the curve is ours: `KineticGlide`, exponential with
    `GLIDE_TAU_MS`, one class rather than a handler per surface. **That
    velocity is px/SECOND, whatever `::decelerate`'s documentation says**
    (`GLIDE_VEL_PER_MS`): GTK computes it as delta×1000 over event times in ms,
    and its own ScrolledWindow feeds it to a solver clocked in seconds. Reading
    the doc instead of the code is how the sheet shipped coasting ~1000× too
    far while the sidebar beside it felt right — and the two read the SAME
    signal, so when one surface scrolls unlike the other, suspect what is being
    READ before the curve. τ is now GtkScrolledWindow's own friction of 4/s
    (250 ms), because every other scrollable surface here IS a plain
    ScrolledWindow and one app settles at one rate; the 2.5×
    `MAGIC_SCROLL_FACTOR` it puts on both its drag delta and its flick velocity
    is deliberately not copied, since the sheet pans a surface delta
    one-for-one — **the invariant is that a flick carries on at the speed your
    fingers were moving the sheet**, not the number. `SIDEMARK_GLIDE_DEBUG=1`
    logs what each flick actually reports. **The sheet also applies GTK's
    `MAGIC_SCROLL_FACTOR` itself** (`SURFACE_SCROLL_FACTOR`, 2.5) — a plain
    ScrolledWindow does not pan a surface delta one-for-one, so without it the
    sheet answered a finger differently from the notes panel beside it. It goes
    on the drag AND the velocity, or letting go changes surfaces. The PDF
    canvas is a stated exception: a scroll there flips the page past an edge
    and `TOUCHPAD_FLIP_THRESHOLD` is tuned against raw 1-px deltas. Two more
    things it must get right. The frame step is the **integral** of the decaying velocity
    across the frame (`glide_frame`), never `v × dt` decayed afterwards, or a
    coast travels further at 30 fps than at 60 — and a dropped frame under a
    relayout is exactly when that shows. And it **stops on anything that is a
    hand**: a fresh scroll, a press, a pinch, a programmatic
    `scroll_to_offset`, plus the end of the document, which it learns from the
    adjustment refusing to move (`_glide_scroll` reports that) rather than by
    measuring the extent. PDF pages are deliberately not in this: a scroll
    there flips the page at a boundary (`_handle_boundary_flip`), and a
    momentum glide into one would fling you through slides — a different
    feature needing its own judgement, not a parity gap to close quietly.
  - Window shortcuts that must beat a focused editor live in capture-phase
    controllers (`_on_global_key`, `_on_undo_key`, the sheet's own). `_on_key`
    is **bubble** and loses to whatever has focus — put a new app-level
    shortcut in capture unless the editor should win (Ctrl+C, Delete, arrows).
  - **A widget in the wrong CONTAINER is unreachable the same way.** Ctrl+F
    "did nothing" on a text page because the search revealer lived inside the
    PDF column (`_paned`), which text mode HIDES — the handler ran and revealed
    something nobody could see. It lives above both modes now (`s._body`).
    Whenever a feature is dead in exactly one mode, check where its widget
    hangs before reading its handler.
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

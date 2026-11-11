# Input: buttons, chords, stylus, touch

> Everything about how a press becomes a tool.

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

- **Button bindings — there is no "active tool" (row 132).** Every mouse
  button, alone or under modifiers, HAS a tool, and pressing it uses that tool:
  left draws, right erases, middle pans, at the same time. `Bindings` is THE
  table — **one per document mode** (`DEFAULT_TABLES`; persisted in
  settings.json under `button_bindings` as `{"pdf": …, "text": …}`), ONE
  instance per window shared by every canvas and sheet.
  PDF defaults (the user's table, 2026-07-31): left pen, middle lasso, right
  eraser, plus four chords — Ctrl+left pan, Ctrl+right text cursor, Shift+left
  zoom-to-region, **Alt+left text cursor** (Alt is how you follow a PDF link,
  and following a link IS the caret's click). The **thumb is unbound** (most
  mice have none). Nothing else is bound out of the box; the rest is the
  user's to bind.
  - **The MODEL is unified, the tables are not.** One class, one popover, the
    same chord ids, and a mode's table is the PDF one unless there is a reason
    (`TEXT_BINDING_OVERRIDES` is the whole list of reasons: a sheet is for
    TYPING, so left is the caret and Shift+left extends a selection; Alt+left
    then carries the pen so it stays reachable without rebinding). One table
    for both modes shipped first and did not survive the text page — what the
    left button should do is exactly what the mode decides.
    `bindings.mode` is what the CHROME acts on — toolbar, badges, tooltips,
    popover, binding surface — and `_update_header_for_mode` keeps it on the
    active tab. **Routing never reads it**: a canvas passes its own
    `doc_mode`, or the sheet's press router would follow whichever tab the
    header thinks is in front. Migration: a saved flat table becomes the PDF
    one and seeds the text one with the overrides on top; `Reset` resets the
    mode you are looking at, and the popover heading names it. `chord_tool()` is **vestigial** — the old fixed grammar, now
  with no production callers and only tests referencing it. Do not route
  anything new through it; `Bindings` is the table. It is kept because the
  older modifier chords it encodes are the obvious defaults to *offer* if a
  binding preset ever lands.
  - **The toolbar is the binding surface**: click a tool with the button you
    want it on — including a FINGER and the pen's eraser barrel
    (`toolbar_binding_for`), which is the same law, not an extension of it.
    Plain left-click is the exception — it stays "put this on the left
    button", so picking a tool feels unchanged, and a pen TIP tap goes down
    that path because a tip press IS a left press. **Claiming the press does
    not swallow the click**: `GtkButton`'s own gesture is CAPTURE-phase too and
    was added first, so `clicked` fires anyway — `_binding_press` is the flag
    that makes the two paths exclusive, not the claim.
  - **The stripes are a LIVE readout, and they are the ONLY signal.** Holding
    a modifier repaints them from the chord table (`_live_buttons_for`), so the
    bar always shows what each button would do right now; an unbound chord
    shows nothing, because that is what pressing it would do. There is no
    second highlight — a glow that lit one tool without naming a button said
    something different from the stripe beside it about the same table.
  - **Routing, badges and tooltips all read the table** (`_refresh_tool_bindings`
    generates both). A second mapping is how the bar comes to claim one thing
    while the mouse does another. Each canvas has ONE press router
    (`PDFCanvas._on_drag_begin` → `_begin_tool`, `TextPageView._on_press_begin`);
    the sheet's router **claims EVERY press** (row 181) — for a tool, for the
    caret, or for nothing at all when the chord is unbound, which is exactly
    what a PDF page does and what the toolbar's empty stripe promises. It never
    denies. That is the whole of lever 3: a press that is negotiated with GTK
    is a press whose outcome depends on which controller got there first, and
    every text-mode input bug came through that seam. Consequences worth
    knowing: the paper-edge width drag is a BRANCH of the caret's tool rather
    than a gesture of its own (a second gesture on that widget can no longer
    see a press at all), the right-button context menu is opened by hand
    (`_reopen_context_menu`), and an unbound chord no longer falls through to
    the editor to move the caret.
  - **`canvas.tool` is derived**: the tool of the button being pressed, else
    what LEFT would do. Assigning to it binds left. `highlighter` /
    `select_mode` are derived the same way — **never assign them alongside a
    tool change**, or `select_mode = False` hands the caret's button back to
    the pen.
  - **Three exceptions, all AT the router, none a fork of the table**: with
    the lasso on the plain button Shift ADDS to the selection (Shift+lasso is
    still the lasso); a live selection is grabbable with any tool (row 125)
    — which on the sheet must be claimed on the capture gesture or a pasted
    image is unmovable; and a plain LEFT press (a pen tip is left) on an
    anchor or callout grabs it whatever left holds (`_page_mark_at`, row 15;
    the web port's `_onDown` is the same rule). Left only, so a finger bound
    to pan still pans across a callout. The `GestureClick` beside the drag
    decides nothing: it fires for a finger too and cannot tell it from left.
  - **PDF text selection is ONE model, `PageText` (row 196)**: the page's
    characters in MuPDF reading order. Hit-testing (`caret_at` — the LINE
    first, then the character), the painted bands, the copied text and the
    highlighter's "mark text" strokes all read the same segments, so what is
    painted is what is copied and what is marked. Never derive a second
    geometry (word boxes) for one of them. Multi-click counting is
    `count_click`, shared with the text sheet. Shift+click extends only when
    a Shift chord routes to the text cursor — by default Shift+left is zoom.
  - **The thumb is a real button**: button 10 never reaches a `GestureDrag`, so
    its press is replayed through the same router with a `_SyntheticDrag`.
    That is what lets it hold any tool instead of a hardwired pan.
    **Settings → Thumb button** can switch it off (`Bindings.thumb_enabled`,
    row 194): every READ of the table skips thumb chords, so it routes,
    paints and lists nothing, and `_bind_chord` refuses it — while the stored
    table keeps them for when it is switched back on. Filter at the reads,
    never at the press handlers, or one path forgets.
  - **A stylus's ends ARE mouse buttons (row 135).** `button_for_event()` maps
    the hardware to a button identity — tip→left, eraser barrel→right, other
    barrel+tip→middle, finger→`BTN_FINGER` — so the pen needs NO table of its
    own and every consumer (table, stripes, badges, tooltips, popover, the
    binding surface) is untouched. The shipped defaults are therefore already
    the pen workflow, and the eraser barrel wears the right button's colour,
    which is how the bar teaches the mapping. If you find yourself adding a
    source dimension to `Bindings`, you have taken the superseded design
    (`notes/stylus-input-plan.md`). *ceiling: tip and left-click are one
    identity and cannot hold different tools.*
    - **Read `get_device_tool()`, NEVER `device.get_source()`** — GTK delivers
      the LOGICAL pointer for a stylus, whose source reports `MOUSE`. The
      obvious API is the wrong one. A touch is the one with an event sequence.
    - **A finger pans, and that IS the palm rejection.** On a convertible the
      palm lands *before* the tip, so whatever a stray touch runs must not be
      the pen. libinput already drops a palm contact itself in ~110 ms and
      sends nothing at all during a pen stroke — so do **not** add proximity
      timers or `TOUCH_CANCEL` handling; re-measure first if a palm ever draws.
    - **The barrel button must be tracked and DENIED, not read off the
      gesture** (`track_barrel`): pressed before the tip it claims the
      `GestureDrag` and the tip's press then never produces a drag-begin at
      all. Same reason the window tracks held keys for touch.
    - **TWO FINGERS ARE A FACT ABOUT THE HAND, NEVER ABOUT THE GESTURE**
      (`TouchLatch` / `attach_touch_latch`, rows 148 + 150). `GestureZoom`
      cannot be what tells a surface a second finger landed: it only fires once
      it *recognises*, and it needs two sequences the press routers may already
      have taken — the sheet's router CLAIMS the first one, so the pinch there
      can be starved and never begin at all. So the count comes off the raw
      touch stream, on its own capture-phase legacy controller, for
      `track_barrel`'s reason. Two invariants: `multi` latches on the second
      touchdown and clears only when the **last** finger lifts (a pinch ends
      while a finger is still down, and `GtkGestureDrag` is single-point, so
      the survivor arrives as a brand-new press — routed through the table like
      any other, that is a dot with `finger: pen`); and a second finger
      **abandons** whatever the first started (`_on_second_finger`, on both
      surfaces) rather than committing it. Never gate any of this on the pinch
      gesture, and never clear `_post_pinch` while a finger is still down.
      - **The latch shipped DEAD twice, and neither cause was in its logic.**
        (1) PyGObject hands `EventControllerLegacy` a **NULL event** for some
        events — the pane-drag trap again — and dereferencing it raises, which
        in a signal handler is silent apart from a traceback: the latch simply
        stopped counting. `handle(None)` is a no-op and the controller's own
        `get_current_event()` is the second chance at the same event. (2)
        Capture controllers of ONE widget run in the order they were **added**,
        and a gesture that CLAIMS returns STOP — so a latch attached after the
        sheet's press router never saw the touches the router had taken. It is
        the **first** controller on both surfaces. Anything reading a raw event
        stream can be correct, unit-tested and never once run.
      - **The sheet's pinch is the latch's own arithmetic** (row 150): it keeps
        each finger's POSITION beside the count, and `_on_touch_frame` drives
        zoom and pan from centroid + spread. `GestureZoom` stays for the
        **touchpad**, which pinches with no touch sequences at all, and stands
        down while the latch holds fingers — one pinch, two ways in. Do NOT
        release the router's claim to feed the gesture instead: the claim is
        what keeps the `TextView` off the press, so releasing it gives the
        caret a click mid-pinch, which is the symptom. Four things the first
        working version got wrong, all of them about the sheet being a
        REFLOW and not a scale factor:
        - **Once per FRAME, not once per event** (`add_tick_callback`). A zoom
          here is a font change and a full relayout, and the panel reports
          several times faster than that lays out.
        - **The driver keeps its own float scroll** and re-applies it at the
          top of the next frame. A `GtkAdjustment` clamps against the extent
          of the layout it HAS, and the relayout to a new zoom is async — so
          the value set during a zoom-in is clamped to the old extent, which
          is the sheet snapping to the top-left. (The PDF canvas never meets
          this: its offsets are plain floats.)
        - **Incremental, measuring each frame against the last**, so a step
          that lands short cannot drag the whole gesture off.
        - **A lift RE-BASES rather than continuing** — one anchor
          (`_touch_anchor`: the centroid, or the one finger left), so a lift
          changes how many fingers make it and nothing else.
      - **The second finger has to be SWALLOWED on the sheet**
        (`consume_extra`): a `GtkGestureDrag` is single-point and *ignores* a
        second sequence, which is not denying it, so it sailed past the router
        into the `GtkTextView` and marked text. The FIRST sequence is never
        consumed — the router holds it, and eating its release leaves that
        drag live forever. The canvas swallows nothing: its `GestureZoom`
        needs both sequences and works.
      - **Abandoning at the touchdown is not enough** — GTK can cancel the
        first finger's drag when another controller takes the sequence, which
        fires `drag-end` with the tool still in hand. So the guard is also at
        the COMMIT (`_press_end`: two fingers down means no commit, whatever
        route got there), and ink a finger committed *earlier in this hand* is
        taken back off the page with its undo op (`_touch_strokes` /
        `_drop_touch_strokes`). Armed by `_capture_device == "touch"`, never by
        the touch count — a palm resting during a PEN stroke must arm nothing —
        and cleared when the glass empties or at any fresh press, so ink is
        revocable only for the hand that drew it.
        *Row 181's lever 3 removed the old hazard here: the router no longer
        DENIES, so the `TextView` never takes the first sequence and its touch
        text-selection popup can no longer appear under a pinch. New ceiling in
        its place: our caret tool draws no touch selection handles, so a finger
        selecting text on the sheet has nothing to grab afterwards.*
    - **No tilt** on this class of panel: the axes exist and are flat zero, so
      a presence check passes and the feature silently does nothing.
    - **A new DEFAULT binding reaches nobody who has customised their table**
      — a saved table is the whole truth, which is what makes an unbind stick.
      `settings.json` therefore also stores `button_defaults_seeded`, the
      default keys ever offered; a key not on it is new and seeded once, a key
      on it but absent was cleared on purpose and stays gone. Never merge
      `DEFAULT_BINDINGS` over the saved table on load — that resurrects every
      binding the user removed. This is how `finger: pan` shipped dead.
  - **Links glow exactly when a click would follow one**: `link_hover_active()`
    is "the left button's tool right now is `text`", never "Alt is down". One
    predicate for the glow and for `_open_link_at`, or the modifier promises
    what the table cannot deliver.
  - **There are no keyboard tool shortcuts.** Ctrl+H (highlighter) and Ctrl+M
    (caret) were hold-to-borrow and were REMOVED with row 132 — a key that
    lends a button a tool is a second mapping beside the table, which is the
    one thing this design does not allow. Tools change by binding them. (The
    README documented both for a while after they stopped existing; if a
    shortcut is reintroduced it has to come from the table, not beside it.)
  - `TOOL_MODES` drives the bar order, the `_MODE_CHROME` tool rows and the
    resolver, so a tool cannot be in the bar and missing from the grammar.
    `"select"` is an alias of `"text"` — one I-beam button serves both modes.
  - Chord routing merges window-tracked held modifiers (`_chord_state`) so
    keyboard+touch works; see ideas.csv rows 115 and 132.

- **One table, not two**: `Bindings` (which button runs which tool),
  `zoom_factor_for_scroll` (scroll→zoom rate),
  `erase_radius` (what counts as touching ink),
  `clipboard_content_for`/`paste_objects` (the clipboard), `draw_image` (how a
  pasted image looks), `recognize_shape`/`rect_bbox_of`/`even_divider_positions`
  /`draw_snap_label` (the extended-dwell shape snap, row 121), `CONTEXT_MENU`
  (what the menu tool offers, row 201) and `zoom_trail_click` (what a zoom
  click goes back to, row 202) are shared by both canvases on purpose. Duplicating a *decision* is how the PDF and text sides
  drift; duplicating *mechanics* is fine — they have genuinely different
  substrates (a scale-transform canvas vs a reflowing ScrolledWindow).
  **Before fixing a bug here, grep every caller of the function you are about
  to touch.** A report names one symptom on one path; the fix belongs where all
  callers route through. One guard in the shared helper is a *smaller* diff than
  a guard per caller, and patching only the path the report named leaves every
  sibling caller broken — which is exactly how the eraser drifted (row 116).

- **The menu tool (row 201)** is a tool like any other — bound, not built
  in — and what it offers is decided by what is SELECTED (`CONTEXT_MENU`:
  each entry names a predicate and a verb both surfaces implement; a false
  predicate HIDES the entry). On a PDF page it is the canvas' own
  `Gtk.PopoverMenu`, which `PDFCanvas.do_size_allocate` presents (a bare
  DrawingArea does not place a popover child itself). On a text page it is
  GTK's own text menu with the same entries in its extra menu. Its press runs
  FIRST in `_begin_tool`, ahead of the selection grab, and is the one press
  that does not clear the PDF text selection — the menu is about it. Never
  open it in a presented test window (CLAUDE.md rule 2):
  `context_menu_entries()` arms and returns what would show.
- **Zoom-to-region click (row 202)**: each region zoom pushes (before, after)
  onto its surface's `_zoom_trail`; a click with the zoom tool pops back while
  the view is still `_near_zoom` the trail's last "after"
  (`ZOOM_TRAIL_DRIFT` / `ZOOM_TRAIL_SCALE`), else drops the trail and fits.
  The sheet records the INTENDED view (its adjustments clamp until the async
  relayout), so do not read it back right after `_zoom_to_region`.
- **What survives a restart (row 203)**: the bindings table — every path
  that binds SAVES, the toolbar's plain click included (`_set_tool_mode`) —
  and the select / highlighter variants (`select_style`, `highlight_style`
  in settings.json, applied to every canvas: they are the app's, like the
  pen).

- **The extended dwell (`_snap_to_shape`, rows 121 + 127).** Holding still
  mid-stroke no longer only makes a line: `recognize_shape()` cleans a closed
  loop into an axis-aligned **rectangle**, an **ellipse** (a near-circle snaps
  to a true circle), or an irregular **polygon**, and a straight line drawn
  inside a rectangle becomes an evenly-spaced **grid divider** (re-spacing its
  siblings, one undo entry — PDF's `("grid", …)` op / the sheet's grouped
  `("reshape", …)`). Recognised shapes are ordinary **strokes** (polylines), no
  new object kind — they lasso/erase/round-trip for free. The **line is always
  the fallback**, so the `shape_snap` setting's "lines"/"off" can't regress the
  classic snap. Rectangles are detected geometrically (`rect_bbox_of`), so grid
  snapping survives a reload with no stored tag — **which is why rect must beat
  polygon on ANGLE, not on fit**: a tilted quad through a box's real corners
  fits better than its bbox, so on residual alone the polygon takes the grid
  snap away (`quad_is_axis_aligned`). Every kind needs an entry in the shared
  `SNAP_LABELS`; a missing one is a KeyError mid-gesture.
  - **A HOLD IS NOT A FREEZE (row 160).** A hand holding a pen against glass
    drifts, so both dwells share ONE forgiving tolerance (`HOLD_SLOP_PX`) —
    there is one hold time to learn, so there is one slop. **They measure from
    different origins and that is the design**: circle-to-lasso from where the
    press LANDED, so a slow drag across the page can never become a selection;
    the shape dwell from wherever the pen was last MOVING, because you draw a
    shape and then stop. Never re-base the lasso anchor to be kind to a slow
    drag — that is the case the press origin exists to reject. The dwell's
    anchor is the load-bearing half: `_arm_straight_timer(at)` is called only
    once the pen has travelled past the slop from `_straight_anchor`, because
    re-arming per motion event lets a shaking hand restart the clock for ever
    and the dwell can never fire, whatever the tolerance. Both surfaces re-arm
    from their own motion handler, so a fix to one of them is invisible.
  - **An ELLIPSE stays resizable while the pen is still down (row 179)**, and
    it needs its own verb because it has no control point to keep hold of — it
    is a sampled curve, deliberately excluded from `shape_vertices`. So the
    whole shape scales about the centre the dwell found, by how much further
    out the pen has travelled (`ellipse_resize_state` / `resized_ellipse`,
    shared by both surfaces and ported to the web). The scale is **PER-AXIS**:
    the pen changes the RATIO and not merely the size, which is the box-handle
    verb every other selected shape already has — sideways widens, down
    heightens, the corner does both. It shipped UNIFORM first, on the argument
    that the dwell had already answered what shape this is; the user's answer
    was that stretching is the point, so that argument is dead. What survives
    of it is `circle_axes` having the last word: within its tolerance a circle
    stays a circle under a shaking hand, and past it you get the oval you were
    plainly asking for (*ceiling: a 12% dead zone around square, so the very
    flattest ovals are unreachable from a circle — lift and draw one*). An axis
    the pen had no LEVER on when the dwell fired follows the other
    (`ELLIPSE_LEVER_MIN`), because dividing by a near-zero offset turns a pixel
    of tremor into a large factor — a circle snapped at its equator would
    otherwise flatten the instant it moved. Recording the pen's OWN offset at
    the dwell is what makes the first motion event scale by exactly 1 on BOTH
    axes; a shape settling under a hand that has not moved is the defect. `sample_ellipse` rounds the sample count up to a **multiple of
    four** so the ring lands on all four extremes and its bounding box IS the
    ellipse's box — everything here re-derives geometry from the points rather
    than storing it (the `rect_bbox_of` pattern), and off a multiple of four
    the re-read centre and axes are a hair out. A rectangle and a grid divider
    are still frozen by the dwell.

# Open ends

> What is IN FLIGHT and what is free to pick up. `CLAUDE.md` is the
> starting point and links here; read that first, then this.

> Keep this file the churny one. When something lands, fold its invariants
> into `CLAUDE.md` or the right `docs/` guide and DELETE it from here — this
> is a to-do list, not a changelog. The chronology lives in `ideas.csv`
> and git. Nothing here should describe something that already works.

## START HERE (2026-09-02): two threads are owed a pass IN THE HAND

Neither needs code. Both need the user, and until they get one the work behind
them is unverified rather than finished.

1. **Text mode's lever 3** (row 181): the checklist is at the end of
   `notes/text-mode-parity-plan.md` — twelve ordinary things, and anything that
   feels *nearly* right is a bug. Lever 3 replaced GTK's pointer handling on
   the text sheet (the router claims every press; the caret is a tool) and
   **none of it has been driven by a real pointer, pen or finger**, because
   there is no key-injection tool on this machine. Two answers are wanted back
   rather than just pass/fail: whether a finger selecting text with no
   selection handles is worse than the bubble it replaced, and whether an
   unbound chord doing NOTHING on the sheet (it used to fall through and move
   the caret) reads as right. The plan file keeps the diagnosis — text mode's
   recurring bugs are a GTK SEAM, not a model disagreement, and a translation
   layer or a new text model was asked for and ruled out. Do not re-derive it.
2. **`notes/validation-session.md`**, the older list, in its own order: the
   merge drops (row 123), then the shape/grid dwell (row 121), the divider
   gesture (row 130), and rows 140–142. Row 160's `HOLD_SLOP_PX = 16.0` rides
   along — it was chosen on a 1.5× display, so it is a starting point, not a
   result.

## The share thread (rows 182/186/187/189/190) — what is left

The behaviour is in `docs/share.md` and the reasoning in `ideas.csv`. Only
these are still open.

- **The browser port has NO text-first mode, so a text page falls back to the
  old snapshot viewer** (`_SHARE_VIEWER_HTML`) — a picture that updates, not
  the real editor. This is the app's largest live exception to rule 3 ("both
  modes, always"), and it is stated rather than hidden: a phone gets the real
  pen, its own camera and the real page on a PDF, and a mirror on a sheet.
  Closing it means porting `TextPageView` into `web/` — a feature, not a fix.
  Read `web/CLAUDE.md` before designing it.
- **The public link (row 186) is blocked on TAILSCALE, not on us.** With the
  funnel confirmed on, the node advertising `IngressEnabled` and certs issued,
  their public ingress accepts the TCP connection and closes during the TLS
  handshake, with nothing reaching `tailscaled`. Reproduced against all three
  ingress IPs; the tailnet path works throughout. **Do not go looking for it in
  this code.** Re-test when Tailscale moves; until then the public tier
  provisions cleanly and is unreachable.
  - The half a user CAN act on, and which we do not yet say anywhere:
    **Cloudflare's resolver returns NXDOMAIN for the `.ts.net` name while the
    funnel is up**, while Google and Quad9 both resolve it — and Brave's Secure
    DNS commonly defaults to Cloudflare. One line on the dialog's public entry
    would save the next person an hour. Small, and free to pick up.
- **The installed app (row 190) is UNVERIFIED ON A REAL PHONE.** What WAS
  verified, in headless Chromium: the worker registers and precaches 43 entries
  with no live-session data among them, a real multipart share-target POST
  lands both files and the app collects and empties them, and the desktops list
  saves, labels and refuses a `javascript:` link. What needs a hand and an
  Android device, in this order:
  1. Install the Pages copy (☰ → Install Sidemark, or Chrome's own offer), then
     open it with the phone in flight mode — it should start and draw.
  2. Share a PDF into it from another app's share sheet; then a PDF *and* its
     `.md` together, which should arrive paired.
  3. "Open with Sidemark" on a `.pdf` from the file manager, then Ctrl+S — it
     should write back in place rather than dropping a copy in Downloads.
  4. ☰ → My desktops → Scan a code, against a live share QR. The camera should
     open and the first code should go straight to the session.
  5. The new "Install on phone" share tier: scan it, then install from THAT
     address. The icon should be named for the machine, and opening it should
     land in the live session rather than a blank page.
- **The ONE measurement that would redesign row 190**, and it is ten minutes:
  `web/lna-probe.html`, opened from the Pages copy on a phone against a desktop
  running the new "Install on phone" tier. Chrome has been moving Local Network
  Access toward a user *permission prompt*. Row 182's record says a tailnet
  HTTPS fetch from a public origin was blocked; it does not say whether a
  prompt appeared and was refused or never appeared at all — different
  findings, and only one of them durable. If a prompt appears and can be
  granted, the hosted app can speak to a desktop directly, "My desktops" stops
  being a launcher and becomes a client, and the browser-tab seam goes away.
  **Record the answer on row 190 either way** — a second person re-deriving
  this is the whole cost of not writing it down.
- **Deferred by decision, tracked on row 182**: several phones drawing at once
  (a canvas has one `current_stroke`, which is the obstacle; the viewport boxes
  are already per phone), and a long-press tool picker on the phone beyond the
  Pen/Eraser toggle.
- *ceiling: nothing covers SIGKILL.* A funnel mapping lives in `tailscaled`,
  so `kill -9` leaves a public hostname pointing at a dead port. Every other
  exit is covered. If it ever matters, the fix is a mapping written to disk and
  reaped at next start.

## Handwriting recognition (rows 183/184/185) — the ML Kit route is CLOSED

Four ideas came out of the MyScript investigation on 2026-08-31. The user chose
the TRANSPORT (row 182) first and it shipped; these three are untouched.

**Row 183's gate was checked on 2026-09-02 and Google's Digital Ink models are
NOT usable here — do not design around ML Kit.** Both halves fail
independently, so a licence change alone would not rescue it: the models are
published nowhere as files, carry no standalone licence, and the ML Kit Terms
forbid extracting them ("you may not reverse engineer or attempt to extract the
source code or any related software"); and the SDK is Android/iOS only, a model
is ~20 MB per language, and it is not one `.tflite` but a bundle of protobuf
configs and separate `.conv_model`/`.lstm_model` binaries. The full finding,
including what was checked, is on row 183 — don't re-derive it, and don't
propose an extraction script.

What survives, and the fork the user has to pick:

- **The ARCHITECTURE is open even though the weights are not.** arXiv 1902.10525
  is the published Gboard/ML Kit system — Bézier-curve input encoding into a
  BLSTM/QRNN stack trained with CTC — which independently confirms row 183's
  online-over-image-based call rather than merely asserting it. Open stroke
  models with published weights exist (IAM-OnDB at 6.2% CER, recent work at
  1.61%); none ships a ready-to-drop artefact.
- **(a) One small BLSTM+CTC model converted to ONNX, optdepended on
  `python-onnxruntime-cpu`** — which unlike `tflite-runtime` IS in Arch's
  official repos. Realistic packaging, far lighter than torch; somebody has to
  do the training run, and the payoff is uncertain.
- **(b) Drop general recognition; do the writer-dependent, character-level
  thing** — no ML dependency at all, and it revives the "train on the user's
  own handwriting" option row 183 had set aside. CellWriter (GPL, Linux,
  grid-entry, Unicode, learns your hand) is the existing art, worth reading
  even though it is GTK2-era and unpackaged here. This is row 184's territory.
- **(c) Park it** until a permissively licensed off-the-shelf stroke model
  appears.
- **Rejected already:** Google Input Tools' undocumented endpoint (still live,
  what the deprecated `handwriting.js` used) — a network round trip for an
  OFFLINE notes app, revocable at any time, and it would send every stroke of
  the user's private notes to Google.

Four use cases justify row 183 whichever way it goes, and they are not the same
feature: search reaching ink (Ctrl+F sees only the notes today), lasso ink →
editable Markdown, a text layer on exported handwritten annotations, and maths
recognized into the notes panel's existing `\alpha`/script grammar. None needs
row 182's transport to validate — build against local pen and mouse input.

- **Row 184 — superimposed single-character entry.** Analysed, not built. The
  hard part is SEGMENTATION, not recognition: a pen lift plus a pause segments
  single-stroke letters correctly and mis-segments every multi-stroke one (i,
  j, t, x, f, most capitals), and a timeout long enough to hold those together
  feels laggy on every other letter. What shipped from the same conversation is
  write-and-advance (row 187), whose question — "have you run out of room?" — a
  stroke's own end position answers exactly.
- **Row 185 — beautify ink into a clean version of the recognized letter.**
  Parked, deliberately not sequenced. A rectangle has ONE clean form, which is
  why row 121's snap works; a letter does not, and the question underneath is
  whether the user wants tidier *handwriting* at all rather than real *text*.
  Revisit only once 183 exists and the appetite is clear.

## Answered by the user, waiting to be built

Six ideas were triaged with the user on 2026-08-17 and each one's open
questions were ANSWERED by them — `ideas.csv` rows 175–180 hold the answers,
and re-deriving them is wasted work. In their own order of readiness, minus the
two that have shipped:

- **Rows 176 and 177 are to be PROTOTYPED IN THE WEB before anything ships** —
  row 176, only what is right of the cursor falls back to source, cursor line
  only; row 177, four options for shapes that stretch when text is inserted
  through them, and they especially want to feel the freehand one.
- **Row 178** — draw while scrolling, which they clarified as *scroll while the
  pen is down*.
- **Row 180 (Nextcloud) is blocked on ONE answer from them**: their own
  instance, or anyone's?

**An APPROVED plan from 2026-08-14 is PHASE 0 IN**:
`notes/text-objects-plan.md` (row 168) — type your own text on a page, stored
like a drawing and written into the PDF as REAL selectable text. **Phase 0
landed on its own** (row 191): the export embeds a Unicode font, so notes stop
exporting their maths as `?`. Phases 1–5 are unstarted, and phase 1 should draw
through the `_export_font()` helper phase 0 created rather than resolving a font
of its own. The plan opens with the design decisions already made; read it
before designing anything in that area. Editing the PDF's OWN text was analysed
and REJECTED there with the user's agreement — don't reopen it.

**Two refinements to row 162 shipped on the WEB first and are owed to the
desktop** (2026-08-16, both verified in a browser, both small):

- **The caret must keep its EXACT position, not just its page.** Today
  `_place_full_notes_caret` goes to the START of the page's section and
  `_restore_note` refills the panel from the top, so crossing the divider
  mid-sentence loses your place — which is most of the times you cross it. The
  two buffers hold the SAME body (the panel shows a page's notes, the sheet
  shows them inside the whole file), so the offset within that body is the same
  number on both sides and only where the body STARTS changes. The web's
  `_offsetInBody` is the whole of it: in the panel it is the caret offset less
  the leading whitespace the commit is about to trim, on the sheet it is the
  offset less `note_offset_for_page(run_start(page))`. Clamp it to the body's
  length, or a long caret position walks into the next page's marker; a page
  with no body clamps to 0, which is the marker it was already going to.
- **A linked RUN should light up as a whole in the sidebar.** A run's body is
  stored once (row 129), so inside one there is no single answer to "which page
  am I reading?" — highlighting every page of the run is the only honest thing
  the thumbnail strip and the outline can say, with the page you are actually
  on keeping the solid outline. Only when the run has more than one page, so
  the ordinary case looks exactly as it does now.

## Input: closed, with three things that outlive it

The INPUT thread is closed — row 135's stylus block and §1b's three fixes were
all accepted in the hand on 2026-08-11 (the pen tip, both barrels, a finger
panning, the palm not drawing while you write, toolbar binding by device, and
the row 150 pinch). Hardware is a Goodix GXTP7380 convertible panel and the
measurements behind every decision are in `ideas.csv` row 135, which
`extras/input_probe.py` re-runs (`--followup` for the awkward cases). Three
things outlive the validation:

- **Open, its own session:** on this laptop palm detection stops working while
  the CHARGER IS PLUGGED IN — most likely charger noise on the capacitive
  digitiser rather than anything in Sidemark (the accepted palm result was
  judged on battery). Re-measure on AC vs battery with `extras/input_probe.py`
  before touching code; the question is whether libinput's ~110 ms `TOUCH_END`
  still arrives when plugged in.
- Two rough edges were judged and **deliberately left**: an eraser-barrel *tap*
  opens the sheet's context menu (a right press is deferred there, and forking
  the router on device is worse), and row 126's circle-to-lasso relies on a pen
  *lift* that a mid-stroke barrel flip manufactures. Fix either only if it
  becomes painful in real use — not pre-emptively.
- Row 136 (a hardware-report script + GitHub issue template for other people's
  devices) is the planned follow-up.

The ink/latency thread (rows 139/143/147) is likewise CLOSED — live smoothing,
dot size and shape, pointer hiding, the smear trim and PREDICTION were settled
by grading, not taste, and the pen's remaining ~110 ms was measured to be
UPSTREAM of the compositor. Do not reopen it without a new measurement.

## Loose ends, roughly in order of how ready they are

- **Row 123 (merge import)** — needs real hardware for the drops themselves
  (several PDFs on the window → "Merge…"; several on the page thumbnails →
  chapters at the gap), the `.pptx` path through LibreOffice, and the chapter
  drag in the outline. Unit-tested end to end (`TestMergeImport`,
  `TestChapterReorder`, `TestMergeImportInWindow`), including the two
  corrupting traps. *Deliberately not done:* no drag-reorder inside the import
  dialog — chapters reorder in the outline afterwards, and the dialog says so.
- **Row 121 (shape & grid recognition)** — the classifier, grid spacing and the
  PDF grid-divider commit/undo/redo are unit-tested (`TestShapeRecognition`,
  `TestGridDivider`); the dwell gestures need the app.
- **Row 167's leftover — insert an image from a FILE.** Small: a drop path
  through `classify_import_paths` (a `.png` is still refused) plus a ☰ entry.
  Both modes; the model, rendering and persistence all exist already.
- **Row 147's SECOND half: the text sheet still repaints its ink every frame.**
  The PDF canvas caches committed strokes into a surface and blits them (flat
  ~1.5 ms/frame against 66 ms at 400 strokes); the sheet's ink lives on an
  overlay with a different substrate, so it does not transfer for free — but
  the parity rule says it should follow. Copy the *decisions* (a
  fingerprint-keyed cache, an append painted onto the existing layer), not the
  code. Also unfinished and trivial: two captures under `GSK_RENDERER=gl` vs
  the default would settle whether to set a renderer in the launcher — the
  probe measured 41 fps vs 60, but by feel in the app it was "similar, maybe a
  tiny bit faster", which is not evidence to hardcode on. **Deliberately NOT
  built, needs the user's call:** stroke-onset recovery — it invents ink that
  was never measured.
- **Row 145 — audit the test suite, and restructure `CLAUDE.md` using it as the
  instrument.** The suite is 905 tests / ~140 s, of which the window tier is a
  third of the tests and 87% of the time, and 26 of those are ≤6-line tests
  dragged into the slow tier because `conftest.py` marks per CLASS. The sharper
  half is value, not cost: three specimens found in one pass asserted *what the
  code says* rather than *what the user gets*. Same session: note per area
  whether `CLAUDE.md` helped, was silent, or was stale. **A test audit is a
  biased sample**, so "unused" means unobserved, not useless.
- **Row 150's leftover is CLOSED by lever 3 and needs confirming in the hand.**
  The sheet's router now claims every press, so the `TextView` never takes the
  first sequence and its touch selection bubble cannot appear under a pinch —
  the user's proposal ("that UI belongs to the finger only when the finger's
  tool IS the caret") is satisfied by construction rather than by a rule.
  *ceiling: our caret tool draws no touch selection handles at all, so
  selecting text with a FINGER on the sheet is now a plain drag with nothing to
  grab afterwards. Nobody has judged whether that is worse than the bubble it
  replaces — it needs the panel.*
- **Row 151 — a survivor finger should keep panning.** LOW priority, and the
  one piece of row 150's pinch that was never built: lift one finger of a
  two-finger gesture on a sheet and the gesture ENDS, because the survivor
  reaches the router as a brand-new press (`GtkGestureDrag` is single-point)
  and is re-resolved through the binding table. The latch already re-bases on
  the finger that is left, so what is missing is a decision, not arithmetic —
  and it belongs as a third exception AT the router, beside Shift+lasso and the
  grabbable selection, never as a fork of the table. PDF pages are unaffected
  (a finger there pans already). Only worth doing if it starts to feel wrong in
  the hand.
- **Row 119 (crop)** — the last piece of the image feature. Its design is
  settled in row 118 and must not be re-litigated: a field on the model applied
  at render, never a destructive re-encode, landing ONCE for both modes.
- **Row 121's un-snap** — Ctrl+Z on a just-snapped shape *removes* it rather
  than reverting to the raw freehand. The `shape_snap` setting and the 500 ms
  dwell are the escapes for now.
- **Row 100** — link authoring: link-to-here and backlinks are the two unbuilt
  quarters (aliases and the `[[` autocomplete shipped).
- **Row 166** — link previews on hover and inline `![[embeds]]`. Its
  precondition is gone: the crash that deferred it is fixed
  (`iter_at_buffer_xy`).
- **Rows 92–94** — text-page items: text-snapping highlighter, pagination/print
  view, margin inks that don't reflow.
- **Row 111** — duplicate-download dialog.
- **Row 117** — the suite is flaky under full-run load: one test fails per full
  run while passing in isolation and on a clean tree. Wants its own session.
  TWO causes are now known, and both read exactly like a race, so check them
  first: a stalled frame clock (see `docs/testing.md`) and shared settings
  state (a test that rebound a chord leaked its table into every window built
  after it — already fixed, so re-measure the flakiness before digging).
- **Rows 26/27/64** — older, unranked.

## Won't do

Presenter/share for text mode (row 106 item 7); a vertex truly BOUND to a point
on another edge (row 131) — the positional edge snap gives the useful half
without needing stored constraints and stable per-stroke ids. Both the user's
call.

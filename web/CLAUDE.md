# CLAUDE.md — working on Sidemark for the web

> The root `CLAUDE.md` is the desktop app and is still the authority on every
> *design decision* this port inherits. This file is the browser side only:
> what differs, what is not here, and how the port is kept honest. Maintain it
> the same way — replace stale facts, keep it lean, put the long *why* in
> `ideas.csv`.

## What this is

A faithful browser port of Sidemark's page, pen and notes. No build step and no
dependencies at runtime: `index.html` plus ES modules under `src/` and four
vendored libraries under `vendor/`. Serve it from any static server.

```sh
cd web && python3 -m http.server 8000     # → http://localhost:8000
```

It is a **port, not a reimplementation**. The ink pipeline, the button binding
table and the stylus mapping are the real designs with the real constants, and
they are checked against `sidemark.py` by exported vectors (below) rather than
by reading them.

### The question it was built to answer

**Does the pen still feel right on the web?** Everything else about a browser
port is tractable work; this is the part that either survives the substrate or
does not. It was judged in the hand and the answer was yes, so the rest
followed. Don't reopen it without a new measurement.

## Layout

- `index.html` — the app. `demo.html` — the six-step tour (below).
  `input-probe.html` — the device measurement harness.
- `src/` — one module per concern; `app.js` wires them. `ink.js`, `bindings.js`,
  `lasso.js`, `shapes.js`, `notes-model.js` and `mathrender.js` are the ported
  ones and are under conformance test.
- `vendor/` — pdf.js (+ its worker), pdf-lib, CodeMirror 6. Vendored, not
  fetched; `package.json`'s `vendor:cm` records how the CodeMirror bundle is
  rebuilt.
- `test/` — node runners plus the exported vector JSON. `package.json` has
  `test`, `check` (a `node --check` sweep) and `vectors` (regenerate from the
  Python oracle).
- `build.py` — flattens everything into ONE self-contained
  `dist/sidemark.html`, for handing to someone as a single file. pdf.js's
  worker is the one thing that cannot be inlined as a script, so its source is
  embedded as a string and turned into a Blob at startup — which is why
  `doc.js` looks for `__SIDEMARK_PDF_WORKER__`. Needs `esbuild` (fetched with
  npx if absent): a hand-rolled bundler would have to resolve imports and
  topologically sort them, and getting that subtly wrong fails silently.

## The tour

`demo.html` teaches the parts you would never find on your own — the dwell,
welding, tools on buttons — by driving a real Sidemark beside it and waiting
until you have actually done each one. It runs the app with `?sandbox=1`, which
reads your settings but writes nothing back, so a tour costs the visitor
neither their session nor their button table.

It teaches **Export**, not a drag that cannot land (see the DnD note below).

## Conformance — how the port is kept honest

The pipeline was tuned by measurement, and several of its traps pass a casual
eye while broken: the doubled Laplacian smooths a circle correctly *and*
amplifies Nyquist; a dot with a per-point profile looks fine until you notice
it is a teardrop. So the port is not checked by reading it.

```sh
../extras/export_ink_vectors.py > test/vectors.json   # the Python is the oracle
node test/conformance.mjs
npm test                                              # all the runners
```

| suite | checks | over |
|---|---|---|
| `conformance.mjs` | 766 | 32 strokes, 24 of them real captures |
| `merge.mjs` | — | chapters, ink re-keying, insert-at-gap |
| `notes.mjs` | 405 | 12 sidecar shapes, byte-identical round trips, page↔offset |
| `math.mjs` | 226 | 37 lines of maths source |
| `lasso.mjs` | 184 | handle points, anchors, scale factors, chip, polygon |
| `shapes.mjs` | 100 | 13 strokes through the recogniser |
| `inkpdf.mjs` | — | annots, appearance, profile, regeneration, foreign ink |
| `crossing.mjs` | 27 | where the caret and its selection land crossing the divider |
| `anchors.mjs` | 12 | legacy text boxes read as callouts, the new callout's placeholder, covered dots |
| `paging.mjs` | 885 | insert/delete/reorder re-key notes, runs, bookmarks, hidden, outline |
| `pwa.mjs` | 124 | the precache list vs the directory, the manifest, the share-target handoff, what a scanned code may be |
| `render.mjs` | 38 | fit against a container with no size, render scale, the preview pass, two renders racing, prefetch |
| `wiring.mjs` | — | callbacks supplied, bare calls resolve, DOM ids exist |

The ink vectors are **real captured strokes** from `notes/*.jsonl` — the same
hand and digitiser the constants were tuned against. The oracle earned its
place on the first run by catching an invented constant (`ERASE_SLACK_PX`
guessed at 4.0 against the real 3.0).

Agreement is to 1e-6 document units, and there is **no algorithmic
divergence** — an earlier 2e-6 gap was the vectors' own rounding perturbing the
input, and it vanished when the exporter's precision went up.

Re-run it after any edit to `src/ink.js`. It is what tells you whether you
changed the shape of the ink or just the code.

**Export rounding is load-bearing.** Each exporter rounds its inputs FIRST and
derives every expected output from exactly the points it publishes. Rounding
after the fact feeds the port slightly different input than the oracle used —
invisible for continuous outputs, but it flips DISCRETE decisions (it changed
which point RDP called a corner on a near-tie) and the port then gets blamed
for the exporter's arithmetic.

## The wiring guard

`test/wiring.mjs` exists because of a specific failure: four page-menu entries
shipped DEAD. They were callbacks a module reads that `app.js` never supplied,
and functions referenced that were never defined — none of which is a syntax
error, so `node --check` passed and the entries simply did nothing when
clicked. The cause was string-anchored edits whose anchors had drifted, and
`String.replace` returns the original silently when it finds nothing.

It checks three things, each of which has caught a real bug: every callback a
module READS is supplied where it is constructed, every bare call resolves, and
every `getElementById` names something that exists in the page. That last one
matters more than it sounds — a missing id throws on the line that touches it,
which aborts module evaluation, so one mistyped element stops everything after
it from initialising.

## Verifying gestures

The conformance suites cover geometry, not wiring — and wiring is where the
real bugs live. Gestures are checked by constructing a real `Surface` and
dispatching real `PointerEvent`s at it, so the actual press router is under
test and the app needs no debug hooks. Four things that harness taught, all of
which will bite again:

- **Pin the view first.** `draw()` re-fits on its first frame, so an `await`
  between computing a screen coordinate and pressing it lets a frame callback
  move the target out from under the test.
- A synthetic event has no `getCoalescedEvents()`, which is why the reader
  falls back to `[e]` rather than assuming the method exists.
- **`element.click()` is not a click.** It dispatches only a click, where a
  hand sends pointerdown first — and pointerdown is what closes a menu. A whole
  context menu was dead to real clicks while a synthetic test reported every
  entry working. Send the full sequence.
- **A synthetic click carries no user activation**
  (`navigator.userActivation.isActive` is false), so no script can open a file
  picker. Anything gated on a gesture needs `Input.dispatchMouseEvent` over
  CDP, or a hand.

Unlike the desktop, this side CAN be driven by an agent through Chrome — see
the memory note on handing off GUI checks. When the Chrome extension is not
connected, `test/drive.mjs` is the fallback and needs nothing installed: node's
global `WebSocket` speaks CDP to a headless Chromium directly.

```sh
python3 -m http.server 8321 --bind 127.0.0.1 &
chromium --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/x about:blank &
node test/drive.mjs http://127.0.0.1:8321/index.html "$(cat probe.js)"
```

The probe is evaluated in the page and returns JSON, so it reads the LIVE model
through `window.__sidemark` rather than a screenshot — which answers "do these
two hold the same page?", where a picture answers only "is there a sidebar?".
Two things it can do that a synthetic click cannot: a real `DragEvent` carrying
a `DataTransfer` of `File`s exercises the whole open/pair path, and
`__sidemark.setSplit(0)` opens the notes sheet without a drag that carries no
user activation.

## A document is two files, and the browser cannot find the second one

The desktop opens a `.pdf` and reads the `.md` beside it. A browser is handed
FILES, never the directory they came from, so the sidecar can only arrive if the
user selects it too — which is why the open picker offers `.md` alongside `.pdf`
(inserting pages does not: a `.md` means nothing there) and why the button, the
drop overlay and the README all say to pick both.

`pairSources` is the ONE pairing rule, shared by the drop and the picker: base
name first, but one PDF and one `.md` opened together are paired whatever they
are called — the desktop lets you choose a notes file by hand and those are
often named for the course. It also carries the sidecar's **handle**, which is
the real payoff: with it a later save writes the notes back in place instead of
`saveDocument` returning `notesPending` and asking for a second gesture.

## The caret crosses the divider (row 162)

Dragging the notes to full width makes them one sheet, and going either way is a
translation between two coordinate systems for the same notes — a page index and
a character offset. `noteOffsetForPage` / `notePageAtOffset` are the one marker
table both directions read; two readings of it is exactly how the caret comes
back on a different page than it left.

**And it is the EXACT position, not the page.** Both buffers hold the SAME body,
so the offset within it is the same number on both sides and only where the body
STARTS changes — `_offsetInBody` is the whole of the translation. Landing at the
top of the right section is still landing somewhere you were not; a divider you
can cross without losing your place is one you will cross mid-sentence. Clamped
to the body's length, or a long position walks into the next page's marker.

A SELECTION crosses too — it is the same two offsets (`_spanInBody`), so
carrying both costs nothing, and text you had marked and then lost by widening
the window is a selection you have to make again for no reason you can see.

**A linked RUN lights up as a whole in the sidebar** (`Sidebar._runPages`,
`.in-run`): a run's body is stored once (row 129), so inside one there is no
single answer to "which page am I reading?", and the page you are actually on
keeps the solid outline. Only when the run has more than one page, so a page
standing alone looks exactly as it always did.

Two of the three answers cannot come from the offset, so `NotesView` also
remembers the page the sheet was opened at and the offset it put the caret at: a
linked run shares one body (row 129), so a caret in it says the RUN and not which
of its pages you were reading, and a caret that never MOVED has learnt nothing
since — which is also the only honest answer for a page with no notes, whose
caret was parked in somebody else's section.

**The sidebar follows the caret while the sheet is open**, and only the
sidebar: `onCaretPage` points `Sidebar.setPage` at the page the caret is in, so
the current thumbnail, the outline's "where you are" line (row 153) and the
scroll all follow the text you are writing in. The canvas is NOT turned — it
would re-render a page nobody can see on every keystroke, and it is `setFull` on
the way out that knows how to read a caret that never moved. Clicking a row goes
the other way (`NotesView.goToPage`), and moves the page the sheet was opened at
with it, or closing would take you back to where you started rather than where
you asked to be. The readout is DEBOUNCED and its marker table cached: the scan
is over the whole sidecar and typing must not pay for it per character.

`setModel` resets the panel to one page, and the divider's state outlives a
document change — so every path that swaps the model calls `syncFullNotes()` to
re-enter the sheet. Without it the panel is full width, showing one page.

## INSTALLED AS AN APP (row 190)

`manifest.webmanifest` + `sw.js` make the hosted copy a real PWA: its own icon,
a standalone window, and a precached shell so it opens with no network.

**Installing is not a permission grant, and this is the fact the whole design
turns on.** An installed page has the same origin and the same per-request
network checks as a tab, so the hosted copy still cannot speak to a desktop's
share server — mixed content on a LAN address, Local Network Access on a
tailnet one (both measured, see LIVE MODE). Packaging changes nothing about
that, and neither does serving JSON instead of HTML: the block is on the
request, not the payload.

- **The service worker has TWO FLAVOURS, one file**, chosen by a flag in the
  script URL (`sw.js?live=1`), and the difference is the strategy, never the
  rules:
  - **Hosted** — precache the whole app, then cache-first. Instant, works
    offline, a deploy arrives one launch later.
  - **Live** — network-first, no precache. The desktop serves `web/` straight
    off a checkout somebody may be editing (its short `max-age` exists for
    exactly that), so a cache-first worker there would hand the phone
    yesterday's app and make an edit look like it never landed. The hop is a
    LAN or tailnet away and the server sends an ETag, so asking costs a 304.
  - Neither can reach a live session's data: `../state`, `../live.pdf`,
    `../page.pdf` and `../ws` sit one level ABOVE the app directory the worker
    is scoped to. Unreachable by construction, not by a rule someone maintains.
  - **No `skipWaiting()`.** pdf.js's worker is fetched lazily, so a worker
    swapped in under a running page could answer that fetch from a different
    deploy than the page came from.
- **Registration gates on `isSecureContext`**, never on a protocol test — a
  hand-rolled hostname check leaves 127.0.0.1 and ::1 out, which is how the
  first browser run showed no worker at all. Plain-http share tiers therefore
  get no worker and cannot be installed, which is why the desktop grew a
  `tailscale serve` HTTPS tier (`docs/share.md`).
- **The precache list is EXPLICIT and checked against the directory both
  ways** (`test/pwa.mjs`). A worker cannot read a directory, and the way this
  breaks is a new module landing in `src/` that nobody adds here — which fails
  only offline, the one place nobody tests.
- **Share target**: Android's share sheet gains "Sidemark". The worker takes
  the multipart POST, parks the files in a **Cache** and 303s to `./?shared=1`,
  which the app collects and empties. The Cache rather than IndexedDB on
  purpose: `db.js` owns the one database and its one version number, and a
  worker opening it on its own schedule is exactly the mismatch that file
  exists to prevent. A share-sheet launch OUTRANKS the saved session.
- **File handlers**: "open with Sidemark" arrives carrying **handles**, so a
  document opened that way saves back in place with no second gesture.
- **`beforeinstallprompt` fires once and cannot be replayed**, so it is stashed
  and the ☰ entry is its visibility.

## MY DESKTOPS — the phone's list of computers (row 190)

`src/desktops.js`. It holds **addresses and navigates to them**; it is not and
cannot be a client (above). Tapping one leaves for that desktop's origin, where
the live session runs exactly as it always has. In a standalone window that
opens as a browser tab — the visible seam of the design, and not worth hiding.

- **No probe before navigating.** "Is it sharing right now?" is the
  cross-origin request to a local address this page may not make, so the answer
  comes from arriving: a desktop that is not sharing says so in its own words,
  which beats this page guessing on its behalf.
- **The QR scanner earns its place on the PUBLIC tier specifically.** That
  token is fresh every session and never saved (row 189), so it is the one
  address that cannot be a bookmark. `BarcodeDetector` is native on Android
  Chrome and absent elsewhere — one capability check and a paste box otherwise,
  the same shape as File System Access. A vendored decoder would be 40 kB
  carried by every visitor for a button most never press.
- **`parseDesktopLink` is strict** — http/https only, trailing slash added,
  fragment dropped. The list is tapped later from a menu with no address bar to
  read, so anything else would be navigating somewhere you cannot see.
- **Hidden in LIVE mode**: the list lives in the ORIGIN's storage, and there
  the origin is the desktop that served the page — so it is empty whatever the
  phone has saved, and an empty one reads as having lost it.

## The open question this design is waiting on

**`lna-probe.html`.** Chrome has been moving Local Network Access toward a user
*permission prompt*. The measurement on record says a tailnet HTTPS fetch from
a public origin was blocked; it does not say whether a prompt was offered and
refused, or never appeared — different findings, and only one of them durable.
The probe settles it from the phone in minutes: `no-cors` on purpose, so a
refusal is the network refusing and not CORS, with the plain-http address
alongside as the contrast that can never change.

If a prompt appears and can be granted, the hosted app can talk to a desktop
directly, "My desktops" stops being a launcher and becomes a client, and the
seam above disappears. Until then, don't design as though it will.

## LIVE MODE — this port as a phone attached to a desktop Sidemark

`?live=1`, served BY a running `sidemark.py` out of its own share server (root
`CLAUDE.md` has the desktop half). The document arrives over the wire, ink goes
both ways while the pen is down, and nothing is persisted here — it is somebody
else's file, open only as long as the connection is.

**Same origin is the only thing that works, and it was measured.** The copy on
GitHub Pages cannot reach a desktop on your LAN or your tailnet: mixed content
blocks it, Chrome's Local Network Access blocks it independently (and counts
the tailnet as local), and **TLS does not lift LNA**. A LAN address can never
satisfy mixed content at all — no CA issues a cert for `192.168.x.x`. So the
desktop serves these files itself. Don't try to make the hosted copy do it.
Numbers in `../notes/phone-web-port-sync-plan.md`.

- **`LIVE` is a URL flag like `SANDBOX`**, and suppresses the same things for
  a different reason: the session, the recents list and the leave-confirmation
  are all about a document that is not this browser's.
- **Pages arrive one at a time** (`Doc.attachLazyPages`, `../page.pdf?n=N`) —
  535.8 KB → 9.2 KB to first paint on a 60-page deck. The trap:
  `strokesFor()` CREATES a page's array on first access, so "has this page got
  its ink yet" must test for EMPTY, not for a missing key. That silently
  dropped every lazily fetched page's ink.
- **`Doc._adopt` is shared by `open` and `openLoosePage`** so a page fetched
  alone is adopted and stripped exactly like one that came with the document.
  Skip the strip and the ink is painted twice — once by pdf.js as an
  annotation appearance, once by us — and pdf.js's copy cannot be erased.
- **`Surface.onInkStream` mirrors raw samples in DOCUMENT coordinates.** That
  is what lets the phone hold its own zoom: a screen coordinate would only
  mean something against the desktop's view. The desktop runs its own pipeline
  over them, so both sides commit the same stroke from the same samples —
  which is what the conformance vectors are for.
- **A gesture that never closes wedges the desktop's ink for ever.** `active`
  is nulled in several places OUTSIDE `_onUp` (a circle becoming a lasso, a
  second finger starting a pinch), so `_closeStream` is called from a
  catch-all at the top of `_onUp` as well as from the tool branches. The
  server guards against a missing close too; do not remove either half.
- **An ink delta REPLACES a page's strokes**, filled in place rather than
  swapped, so the renderer's layer and any live selection keep looking at the
  same array.
- **`MOBILE` is `pointer: coarse` and `hover: none`** — a laptop with a
  touchscreen is not mobile, and the binding stripes are meaningless without a
  second pointer. In landscape the toolbar becomes a rail and `--header-h` is
  overridden; everything floating under the header measures from that
  variable, so change it there and nowhere else.
- **`html, body` must be `100dvh`, not `100%`.** With `overflow: hidden`,
  `100%` resolves against the LARGE viewport — the one with the URL bar
  hidden — so on a phone the bottom strip of the page sat below the visible
  area and could not be reached at all.
- **Fullscreen is Android-only.** Safari implements `requestFullscreen` for
  video and nothing else, so the entry is offered only where the API exists;
  Add to Home Screen (the `*-web-app-capable` meta) is the iOS route.
- **The next page is PREFETCHED on every turn** (`Doc.prefetchPage`). A turn
  costs a round trip to the desktop before a pixel can be rasterised, and that
  round trip — not the rendering — is the beat you can see. It restores
  `_lastPage` synchronously, because that is what `_evictLazyPages` treats as
  "here": a prefetch that moved it would evict the neighbours of a page nobody
  is looking at.
- **Lazily fetched pages are EVICTED** to a window of 8. Each is a whole
  pdf.js document whose memory lives in the WORKER, so holding them all is
  invisible in `performance.memory` and ends as a killed tab on a phone.
- **Breadcrumbs use a keepalive FETCH, never `sendBeacon`.** Brave
  neutralises sendBeacon by returning TRUE and discarding, so an
  `if (!sendBeacon(...))` fallback never fires and the record vanishes
  silently — which is exactly how a browser crash was investigated twice with
  nothing to show for it.

## Painting a page: what happens before it is ready

Three traps, and every one of them is silent — the page is simply white, or
soft, or a beat late, with nothing in the console.

- **`fit()` refuses a container with no size**, and this is the important one.
  `(0 - 48) / 595` is a perfectly good NEGATIVE number, so a fit against a box
  that has not been laid out yet gave a negative zoom, `_renderScale` asked
  pdf.js for a negative viewport, `doc.render` clamped it to a 1px canvas
  rather than throwing, and `draw` refuses to blit anything that narrow. White
  page — and the dud was CACHED under `_pageKey`, so nothing re-rendered until
  a gesture happened to change the scale. On a phone that reads as "blank until
  I pinch". It leaves `_fitPending` set instead, clamps the margin to the box
  (a narrow column is a small page, not an inverted one) and floors the zoom at
  `MIN_ZOOM`.
- **The owed re-fit is paid OUTSIDE `draw`'s canvas-resize branch.** It used to
  be inside it, so a fit owed while the canvas already had the right size was
  never paid at all. `fit` re-sets the flag when there is still no box, so this
  cannot spin.
- **Each render gets its own canvas** and is swapped in only if it still wins
  (`_renderAt`). `doc.render` sets the size and paints, so two renders sharing
  one buffer means a slower earlier one can finish last and overwrite the
  bitmap `_pageKey` claims is on screen. A degenerate result is never cached.
- **A blurry page beats a white one.** With nothing on screen for this page, a
  `PREVIEW_SIDE` bitmap goes up first and the full-scale one replaces it when
  it lands, so a page sharpens instead of appearing. Deliberately skipped in
  two cases: when this page is ALREADY showing, where the bitmap in hand is the
  better placeholder and a soft one is a visible step backwards; and when the
  full render is already small enough that the preview would be the same work
  twice.

## Browser differences (measured, not assumed)

Taken with `input-probe.html` on the Goodix GXTP7380 panel, Linux/Wayland:

| | pen | finger | mouse |
|---|---|---|---|
| Firefox | **133 Hz**, 2.21 samples/event, pressure 0–0.81 | 96 Hz | 122 Hz |
| Chromium | **134 Hz**, 2.22 samples/event, pressure 0–0.85 | 100 Hz | 130 Hz |

**The two engines are indistinguishable for ink.** Both recover the panel's
full 133 Hz through `getCoalescedEvents()` — the same rate the desktop gets
after row 147's `motion_history()` fix, and the thing that decides ink quality.
Both deliver ~60 Hz without coalescing, already twice what the GTK canvas saw
before that fix. So Chromium is the recommendation for *visitors* only because
of File System Access; Firefox is equally good to develop in.

- **File System Access is Chromium-only**, so save-in-place cannot exist on
  Firefox. The fallback is a download — one capability check, not a second code
  path.
- **Touch pressure** reads 0.00 on Firefox and 0.50 on Chromium. It costs
  nothing, because pressure is taken from the pen only: the browser's flat 0.5
  for a mouse is a lie about the device, and `finger` is a button identity, not
  a nib.
- **Tilt is reported by neither**, matching the desktop's finding that this
  class of panel has no usable tilt axes.
- Spacing ÷ feature size came out **0.017–0.036** across every device:
  oversampled, so denoising is the lever and interpolation is not carrying the
  result — the same regime the desktop measured at 133 Hz, where the Taubin
  passes are near-inert. Do not widen the smoothing range to compensate.

Safari is not supported and is not being tested.

## Why a page cannot be dragged to a file manager

Dragging thumbnails OUT works into another Sidemark window's page strip. A
Linux file manager will not take it, and the reason is worth knowing before
anyone tries to "fix" it: the browser can drag out a file that already EXISTS
(a download leaves as a `file://` URI), but a page offering bytes it has not
written yet needs `DownloadURL`, the file-promise type Chromium implements on
**Windows and macOS only**. Extracted pages live in memory, so there is no file
to point at. Export writes one — which is what the tour teaches instead.

## Re-keying pages: the invariant is that NOTHING IS LOST

Notes, linked runs, bookmarks and hidden flags are all stored against a page
index, so every insert, delete and reorder re-keys four things at once — three
of them invisible state that exists nowhere else. The failure mode is never a
crash; it is a page quietly showing somebody else's notes, or none.

**A page inserted INSIDE a linked run joins it.** A run's body is stored once,
on its first page, so breaking the link at the gap does not split the notes in
two — it deletes them from every page after the insertion point. That shipped in
both apps.

**Test it as "no page loses its text", never as "page N equals page M"**: an
insert inside a run legitimately gives the NEW page the run's text too, and an
equality has to be relaxed to allow that — relaxing it is exactly how the loss
went unnoticed. `paging.mjs` walks every insertion and deletion point and checks
that whatever a page showed, it still shows wherever it moved to.

Images need no re-keying of their own: they live in the same per-page map as
ink (`Doc.open` folds them in), so one map is re-keyed and both follow. What is
NOT carried is the undo stack — `setDoc` clears it, so a structural edit is the
end of undo history here, unlike the desktop, which re-keys its stack.

## Verbs the keyboard cannot reach

**Ctrl+N and Ctrl+Shift+N never arrive.** They are the browser's own new window
and incognito window, reserved before a page sees the event, so `preventDefault`
is never even reached — a verb living only on those keys is a verb nobody in a
browser has. New document and Add a blank page are therefore in the ☰ menu, and
anything new must be checked against the browser's reserved set before its
shortcut is called done.

## Deliberately not here

Scope limits, not oversights: no image crop, no tabs, no wiki links, no
text-first mode, no notes export, no OCR, no PowerPoint import.

**Share-to-phone is no longer on this list, and the reason it was is worth
knowing.** "A browser tab cannot listen on a socket" was true and was never
the obstacle: in LIVE mode the tab still does not listen. The desktop serves
this port and the tab connects OUT to it, which is what it was always able to
do — see LIVE MODE below.

**No prediction, and it is settled.** It was graded against 133 Hz captured ink
and recovers ~10% of the lag error at best, negative past 40 ms.
`getPredictedEvents()` is the same guess with a vendor's name on it — don't
reach for it.

## Publishing

`.github/workflows/pages.yml` deploys `web/` to GitHub Pages on every push to
`master` (<https://brokkoli71.github.io/sidemark/>). It is a static site, so
the job copies files and nothing else; `test/` and `package.json` are
development-only and are left out. Over HTTPS the File System Access API works,
which is what gives save-in-place — the one thing a `file://` copy or a plain
http server cannot offer.

HTTPS is also what makes this the **installable** copy, so the job must copy
`manifest.webmanifest` and `sw.js` too, and `sw.js` must land at the site
ROOT — a worker's scope cannot reach above the path it is served from.

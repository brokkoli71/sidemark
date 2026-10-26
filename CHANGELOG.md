## [0.7.0] - 2026-10-26 — Off the Desk

### 🚀 Features

- *(outline)* Rename and delete the document's own headings too
- *(outline)* Edit a heading's page too, and add headings from the menu
- *(web)* Browser prototype of the ink half
- *(web)* PDFs, pages, the sidebar and merge import
- *(web)* An input probe, so browser choice is measured not argued
- *(web)* The notes panel, and the sidecar format it writes
- *(web)* Live maths in the notes panel
- *(web)* Saving — annotated PDF and the .md sidecar
- *(web)* Lasso selection — select, move, resize, duplicate, delete
- *(web)* Lasso rotation, and fix the header stacking its icons
- *(web)* Port the shape recogniser (the extended dwell)
- *(web)* The extended dwell — hold still mid-stroke to snap
- *(web)* Grid dividers — a line inside a rectangle cuts even cells
- *(web)* Circle to lasso — hold on the loop you just drew
- *(web)* The divider is the way between the modes (row 130)
- *(web)* Page management — reorder, delete, insert from the toolbar
- *(web)* Find in document (Ctrl+F)
- *(web)* The text cursor — select and copy text from a PDF
- *(web)* Bookmarks, and the linked-notes checkbox
- *(web)* Sidemark's own palette, a pen swatch and zoom buttons; Pages workflow
- *(web)* Say when saving cannot write back to the original
- *(web)* The Sidemark icon as the favicon
- *(web)* Hidden pages, blank pages, notes in search, recolour, shortcuts
- *(web)* PNG favicons
- *(web)* Select several pages, export them, and a header that fits
- *(web)* Recent documents
- *(web)* Presenter mode
- *(web)* Control points and welding (row 127)
- *(web)* Copy ink — a picture for other apps, the objects for this one
- *(web)* Anchors and callouts, and hide HTML comments in the notes
- *(web)* A tour that waits until you have actually done it
- *(web)* A press outside a selection dismisses it, and marks nothing
- *(web)* The tour teaches Export, not a drag that cannot land
- *(web)* Pasted images, as objects rather than stamps
- *(web)* Publish the demo from master, and a README aimed at its visitors
- *(notes)* The caret follows you between the page and the sheet
- *(notes)* One rule for the space that ends a symbol or a script
- *(notes)* Links point at NAMES, and say when they point at nothing
- *(notes)* Hovering a link shows the page it leads to
- *(notes)* The link you are standing in shows its page under it
- *(notes)* ![[embeds]] show their page, wherever the caret is
- *(images)* Import the PDF's own pictures as objects
- *(logging)* A GTK abort and a frozen loop both leave a trace now
- *(web)* The caret crosses the divider, and a document is two files
- *(web)* The sidebar follows the caret while the notes are one sheet
- *(web)* A run lights up whole, and the caret keeps its exact place
- *(notes)* A search says the same thing on the page and in the notes, and a task box is one you can tick
- *(ink)* A recognised ellipse is still resizable while the pen is down
- *(text)* The sheet coasts after a touchpad flick
- *(text)* The sheet scrolls exactly like every other surface
- *(text)* The sheet claims every press, and the caret is a tool
- *(share)* A public link via Tailscale Funnel, and sharing outlives its window
- *(share)* The phone runs the real editor, and ink flows both ways live
- *(share)* Point the phone from the desktop, and make the QR dialog instant
- *(share)* A bookmarkable address, and the phone answers to modifiers
- *(share)* The phone's tool is the desktop's, both ways
- *(share)* The phone installs Sidemark, and remembers which computer
- *(notes)* Dirac notation, \bra and \ket

### 🐛 Bug Fixes

- *(ci)* Ask for our own scroll controller by name, and stop closing the app
- *(gtk)* Two real bugs the CI warnings were hiding
- *(web)* Reopened ink is editable again, and reloading restores the document
- *(web)* Three bugs found by auditing rather than by using
- *(web)* The pen swatch, and render the page above display resolution
- *(web)* Sharpness — 1:1 rendering on whole device pixels
- *(web)* Presenter navigation — right-click, and a double-fire I had added
- *(web)* Four dead menu entries, and a file picker that could never open
- *(web)* Every page-menu entry was dead to a real click
- *(web)* Dropping a PDF on the thumbnails, and three smaller things
- *(web)* The page drag, the timer that would not go, and a bar with too much in it
- *(web)* A page menu nobody could reach, and a hook to catch the next one
- *(web)* A modifier the window never saw released left the pen dead
- *(web)* Holds that tolerate a hand, and drags that actually carry something
- *(web)* The tour's dwell step ends when the shape appears
- *(web)* One space finishes a script, as it does on the desktop
- *(text)* The thumb button reaches the sheet's press router
- *(text)* The thumb's pan is not fought by the TextView below
- *(notes)* The hand, the hover and the picker were dead off the caret line
- *(notes)* The hover preview is a tooltip, and the sheet keeps its scroll
- *(ink)* A hold is not a freeze — both dwells reachable by a real hand
- *(pages)* A page inserted inside a run joins it, and the strip selects by region
- *(notes)* The whole task box is clickable, and it stands alone at the left margin
- *(notes)* A task box does not open under the caret, and the press decides which box a click ticks
- *(ink,text)* An ellipse stretches per axis, and the coast is much shorter
- *(notes,scroll)* A hover cannot abort the app, and a flick coasts at the sidebar's rate
- *(share)* Public link by default, a funnel stop that works, and a green suite
- *(share)* The phone's ink arrives again, and Tailscale says why in words
- *(web)* A page that is not ready yet, and one that arrives sooner
- *(share)* A teardown says who asked without looking like a crash
- *(export)* Notes keep their maths, and subsetting stops freezing the window
- *(install)* DejaVu is found when it is there, and the fitz notice stays quiet
- *(notes)* A reload keeps a text page a text page
- *(ink)* Autosave rewrites only the ink that changed

### 💼 Other

- *(web)* A single-file build, and a launcher to open it

### 📚 Documentation

- *(web)* Bring the README up to date
- *(web)* Bring the README up to date
- *(web)* Why a page cannot be dragged to a file manager
- *(text)* Record the text-mode parity plan and its diagnosis
- *(text)* The width-drag comment names the caret's tool, not the gesture it replaced
- *(share)* Record the phone-view tool, the dialog speed fix and --version
- CLAUDE.md becomes a starting point again, and stays one
- The share thread's open ends, and the ML Kit gate closes

### ⚡ Performance

- *(web)* Render the page at 3x, and oversample thumbnails too
- *(autosave)* Typing must not re-serialise the whole PDF

### 🧪 Testing

- *(web)* Drive the app in headless Chromium, and read the live model
- *(text)* The adapter rule is stated, and parity is a matrix
- Popovers stay off presented windows, so headless Weston survives

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.6.0.r0.gd4f9685
- *(aur)* Bump sidemark (fixed release) to 0.6.0
- Name the crashing test, and take llvmpipe's GL stack out of the run
- A block scalar, so the timeout flags reach pytest
- Interrupt a stall with SIGALRM, so the run still reports
- Ignore web/dist without clobbering the rest
- The suite runs again, because CI says its Weston is its own

### ◀️ Revert

- *(notes)* Pull the link previews, and write down what they taught
## [0.6.0] - 2026-08-11

### 🚀 Features

- *(ink)* Shape recognition on dwell + non-uniform lasso resize (rows 121, 122)
- *(import)* Merge dropped documents into chapters (row 123)
- *(import)* Sidebar-wide drops, text-mode parity, new defaults (rows 123, 124)
- *(lasso)* The selection keeps its loop, a chip toggles the resize box (row 125)
- *(lasso)* Circle to lasso — press and hold the stroke you just drew (row 126)
- *(ink)* The dwell recognises polygons, and a circle is actually round (row 127)
- *(ink)* Open paths of lines, and a snapped shape comes back adjustable (row 127)
- *(ink)* Movable control points on a selected shape (row 127)
- *(ink)* Welding control points, live last point, all selected shapes (row 127)
- *(ink)* Snap to edges, and live magnets while the pen still holds a shape (row 127)
- *(ink)* The live shape snaps to its own points and edges too (row 127)
- *(ink)* Snap onto freehand ink as well (row 127)
- *(notes)* Notes that continue across pages (row 129)
- *(input)* The toolbar binds buttons; per-press logging to see it (row 132)
- *(input)* Clean defaults, thumb pans, hold-to-borrow removed (row 132)
- *(input)* Links follow the table, stripes follow the modifiers, notes runs cascade
- *(notes)* Bookmark pages, jump to them, and reopen where you left off
- *(input)* A stylus's ends are mouse buttons, and a finger pans
- *(cli)* --new and --new-text start on a blank document
- *(lasso)* A delete cross on the selection, and taps that never draw
- *(ink)* A pen stroke that keeps its shape, and maths that reads like maths
- *(notes)* Per-mode button tables, nested scripts, and runs written as ranges
- *(modes)* The divider is the way between the modes, and comments stay out of the way
- *(notes)* Number sets, ruled pages that stay ruled, recents on a key
- *(ink)* The line is smoothed as you draw it, and a dot is round
- *(ink)* The pen you picked is the pen you get back
- *(ink)* A capture carries the clock, so prediction can be graded
- *(ink)* A capture names its device, and the replay leads with the rate
- *(extras)* Read a device's raw report rate without libinput (row 147)
- *(ink)* The pen draws at its own rate, not the frame's
- *(extras)* Measure end-to-end pen lag by chasing a moving dot (row 147)
- *(extras)* The latency probe follows a circle, and fits in 2D
- *(extras)* --inject-lag, so the probe can prove its own deltas
- *(ink)* A capture counts frames as well as pen samples
- *(bookmarks)* Name one as you make it, and find them in the outline
- *(reload)* Ctrl+R brings back every tab and the view you were reading in
- *(bookmarks)* The second click renames, and F2 renames in the outline
- *(outline)* Show where you are, and what page each entry starts on
- *(pages)* Hide pages, skipped when paging, presenting and exporting
- *(outline)* A position line between the entries you are between

### 🐛 Bug Fixes

- *(lasso)* End the selection on a press elsewhere; stop the post-conversion drag erasing
- *(lasso)* A tap that dismisses the selection no longer leaves a dot
- *(ink)* Ghost line from the snap label to the snap ring; row 129 not planned
- *(notes)* A click no longer jumps the caret and selects to the end (row 128)
- *(text)* Stop the sheet scrolling to the top under the click (row 128)
- *(ideas)* Repair row 118's quoting so the file parses as CSV again
- *(input)* Paint the binding stripes, and let the pen popover shrink
- *(input)* The stripe is the only signal, and it follows a left bind
- *(ui)* Point the pen popover arrow at its button
- *(input)* The table decides the right button, not the button number
- *(modes)* The page slides out and back in, instead of never leaving
- *(text)* Ink keeps its paragraph when the sheet's text is replaced
- *(modes)* The collapsed page edge is wide enough to grab
- *(text)* A drawing moves as one when the sheet's text shifts
- *(notes)* Only the symbol you touch goes back to being a command
- *(notes)* Marked text opens in full, and a command eats its last space
- *(notes)* A script eats its space too, and triple-click takes the line
- *(ink)* Only a motion event is asked for its history
- *(extras)* The latency probe closed itself after one frame
- *(ink)* The prediction damping is a time constant, not a per-event weight
- *(ink)* A stroke on a text page keeps the shape it was drawn with
- *(notes)* A PDF with no sidecar opens with no empty slot beside it
- *(input)* A second finger stops the first from drawing, until every finger lifts
- *(input)* The finger counter crashed on a NULL event, so it never counted
- *(input)* The sheet's surface origin unpacked a point as a triple
- *(input)* The sheet's pinch moves once a frame, and the fingers stay put
- *(bookmarks)* Enter makes the bookmark, Escape makes nothing
- *(bookmarks)* Double-click renames, and two popover criticals
- *(import)* An inserted chapter keeps its hidden pages
- *(bookmarks)* Clicking away from a rename no longer floods the log

### 💼 Other

- *(input)* Resolve every press through one binding table (row 132)
- *(input)* Dump the table and the stripe mapping on every refresh
- *(pages)* The model half of hidden pages (row 158)

### 🚜 Refactor

- *(text)* Route the sheet's presses through the same table (row 132)

### 📚 Documentation

- *(claude)* Trim the changelog out of CLAUDE.md, add two comment conventions
- *(claude)* Record polygon recognition and the rect-vs-polygon rule
- *(claude)* Correct the sidemark.py line count (12.6k -> 16.7k)
- Drop shortcuts that no longer exist, correct the tool model
- Point the next session at the validation pass
- *(ink)* Live smoothing is accepted, so prediction is unblocked
- *(ink)* The pen's samples arrive at half a finger's rate (row 147)
- *(ink)* The pen is 133 Hz and we discard 78% of it (row 147)
- *(ink)* The pen lag is ~110 ms and most of it is below the app
- Point the next session at the stylus block
- Hand the evening's check a list for the three fixes
- The stylus block is verified, and a survivor finger is row 151

### ⚡ Performance

- *(ink)* Committed strokes are a cached layer, not a per-frame repaint

### 🧪 Testing

- *(text)* Stop the row-128 scroll guard failing under full-run load
- *(ink)* Make the eraser test able to fail

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.5.0.r0.g8e2ed12
- *(aur)* Bump sidemark (fixed release) to 0.5.0
- *(aur)* Regenerate .SRCINFO for the 0.5.0 bump
- Run the suite through pytest, or its isolation never applies
## [0.5.0] - 2026-07-17

### 🚀 Features

- *(aur)* Add a fixed-release 'sidemark' package next to sidemark-git
- *(text)* Open the startup scratchpad as a text-first page
- *(ideas)* Rejected status closes issues as not planned
- *(text)* Dedicated caret tool, Alt+drag pen, PDF-only style menus
- *(text)* Export as PDF and sheet zoom for text pages
- *(notes,app)* Verbatim code spans and per-version instance id
- *(app)* Open launched files as a tab in the last-used window
- *(notes)* Wiki-style [[links]] that jump between slides and documents
- *(notes)* [[link]] autocomplete popup and display aliases
- *(app)* July review pass — text-page autosave, lasso resize/duplicate, pinch zoom, fix batch, --fast test tier
- *(notes)* Text-page lasso, straight-line snap and stroke smoothing (parity 1-3)
- *(notes)* Text-page pan gesture (Ctrl+drag / middle-drag) — parity item 4
- *(notes)* Text-page Shift+drag zoom-to-region — parity item 5
- *(notes)* Add zoom-to-region as a text-mode toolbar tool (parity fix)
- *(notes)* Alt+right-drag erases on text pages (quick-ink pairing)
- *(notes)* Text-mode pan tool + thumb/two-finger pan + tooltip cleanup
- *(zoom)* Unify zoom-to-region rectangle across PDF and text modes
- *(notes)* Per-document width for text-first sheets (drag the paper edge)
- *(notes)* Ctrl+Shift+drag temp-highlighter on text pages (parity item 6)
- *(input)* One chord grammar across pdf and text modes (row 115)
- *(input)* Thumb button fully mirrors middle-button navigation
- *(input)* Alt+Shift is the portable keyboard zoom chord; zoom to 16x
- *(nav)* Escape steps back out of the last zoom-to-region
- *(images)* Paste, place and copy images on text pages (row 118, text half)
- *(images)* Paste, place and copy images on PDF pages (row 118, PDF half)
- *(images)* Finish the PDF image layer; size, select and edit pastes (rows 118, 120)

### 🐛 Bug Fixes

- *(zoom)* Make text-mode right-click zoom-cancel actually fire
- *(notes)* Word-level Ctrl+Z granularity in the unified undo timeline
- *(input)* Scroll-zoom the text sheet toward the cursor, not the top-left
- *(input)* Ctrl+scroll never zoomed the text sheet (ScrolledWindow ate it)
- *(input)* Two hit-test/routing bugs on the notes panel and text sheet
- *(input)* Text-mode scroll + Ctrl+R, and share the zoom/erase policy
- *(ink)* Pen width is a document width, and let the sheet zoom in close

### 🚜 Refactor

- *(app)* Port the deck branch's doc_mode/_MODE_CHROME mode framework

### 📚 Documentation

- Add CLAUDE.md project guide
- Record text-mode gesture/workflow pass and reprioritise backlog
- *(notes)* Close #113 — audit text-mode shortcuts + document the model
- *(claude)* Park the deck branch — no longer a design concern
- Close out the July input/parity pass; queue image paste and the flake

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.4.0.r0.g4398e33
- *(aur)* Track .SRCINFO for the fixed-release package
- *(aur)* Bump pkgver to 0.4.0.r3.g9fa45aa
- *(ideas)* Sync issue numbers for #89-95
- *(ideas)* Backfill issue/hash metadata for this session's rows
## [0.4.0] - 2026-07-03

### 🚀 Features

- *(share)* Live phone view that follows along, in a non-modal window
- *(share)* Render anchors, callouts and text boxes in the live phone view
- *(notes)* Zoomable notes-panel font for reading presenter notes
- *(present)* Presentation timer + large nav bar, and window-wide side-button paging
- *(notes)* LaTeX accents, centred short pages, and presenter re-fit
- *(app)* Single instance so tabs can be dragged between any windows
- *(present)* Next-slide preview on the presenter's window
- *(present)* Next-slide stack view, presenter-window controls, live ink
- *(present)* OSD-style presentation bar that scales with the window
- *(present)* Tuck the next slide underneath when the canvas is tall
- *(notes)* Typing a bracket surrounds the selection instead of replacing it
- *(text)* Text-first mode — an endless Markdown page you can draw on
- *(text)* Hide PDF-only menu actions on a text page

### 🐛 Bug Fixes

- *(notes)* Make bracket-surround work with real keystrokes
- *(notes)* Keep hidden markdown markers when saving rendered lines
- *(ideas)* Repair broken CSV quoting in row #87
- *(ci)* Run install.sh non-interactively

### 🧪 Testing

- *(thumbnails)* Make empty-sidebar-click test deterministic

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.3.0.r0.ge90f98e
- *(ideas)* Sync issue numbers for #61, #69, #77-87
## [0.3.0] - 2026-06-22

### 🚀 Features

- *(nav)* Follow footnote/citation links and jump back (Alt+Left)
- Sidebar selection/tooltips, switchable notes file, open-any-file
- Open multiple PDFs as tabs in one window (#51)
- *(ocr)* OCR for scanned PDFs, plus CLI --help and shell completion
- *(nav)* Stop wheel/touchpad scroll from over-scrolling past first/last page
- *(callouts)* Render symbols, super/subscripts and Markdown in callout boxes
- *(callouts)* Standalone text boxes on the page (Ctrl+Alt+right-click)
- *(share)* Share the current PDF to a phone via a QR code (#62)
- *(share)* Offer an "Over Tailscale" QR when a tailnet IP is present
- *(export)* Flatten ink, trim notes pages, group them, and share the export
- *(tabs)* Reopen the last closed tab with Ctrl+Shift+T
- *(pptx)* Import slide speaker notes into the notes sidebar

### 🐛 Bug Fixes

- *(menu)* Use an inline stack so first 'Open recent' click works (#63)
- *(notes)* Keep \sum etc. as source in saved notes; render symbols for display only
- *(install)* Build ocrmypdf with system python so a mise/pyenv shim can't break it
- *(ocr)* No longer wipe a document's notes

### 📚 Documentation

- Add discovery keywords and a star/vote nudge
- Lead the README with lecture-focused selling points

### 🧪 Testing

- Add headless Weston wrapper to run the suite without spawning windows

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.2.2.r0.ge73476b
- Ignore notes/ scratch dir and sync ideas.csv issue metadata
## [0.2.2] - 2026-06-18

### 🚀 Features

- *(canvas)* Smooth diagonal two-finger touchpad panning
- *(tools)* Discoverable tool palette with modifier highlighting (#52)
- *(select)* Reading-order text selection + long-press style menu (#53)
- *(highlighter)* Mark-text style highlights whole lines as ink (#54)
- *(callouts)* Drag a callout box to reposition it (#22)
- *(shortcuts)* PDF keys work with sidebar focused + Ctrl+W close (#58)
- *(pages)* Insert a dropped PDF into the sidebar + drop confirmation (#59, #60)
- *(lasso)* Select ink strokes to move, delete, or recolour (#48)
- *(presenter)* Second-screen live view for presenting (#55)

### 🐛 Bug Fixes

- *(ui)* Don't error on cancelled Open dialog or Ctrl+C

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to 0.2.1.r0.g62eefc7
- *(changelog)* Drop ideas.csv bookkeeping from the changelog
## [0.2.1] - 2026-06-16

### 🚀 Features

- *(anchors)* Drag a placed anchor to reposition it
- *(draw)* Hold mid-stroke to snap to a straight line (#34)
- *(draw)* Smooth freehand ink on commit, tunable in pen settings (#47)
- *(ui)* Responsive header collapses secondary actions when narrow (#45)
- *(ui)* Single grouped-row header with measured progressive collapse

### 🐛 Bug Fixes

- *(icons)* Fall back to themed icon names so KDE/Breeze shows them

### 📚 Documentation

- Regenerate changelog and refresh ideas hashes

### ⚙️ Miscellaneous Tasks

- *(aur)* Bump pkgver to r173.a920636
- *(aur)* Anchor pkgver to the latest release tag
## [0.2.0] - 2026-06-15

### 🚀 Features

- *(notes)* Expand /date, /time, /now slash snippets
- *(search)* Extend Ctrl+F to search the Markdown notes too
- *(zoom)* Make pinch zoom and pan together, no stray strokes

### 🐛 Bug Fixes

- *(zoom)* Leftover finger pans after pinch instead of drawing

### 💼 Other

- Prompt to auto-install missing dependencies
- Use /usr/bin/python3 -m pip for pip installs
- Auto-install python3-pip before pip installs on deb/rpm
- Drop Ubuntu (already covered thoroughly by ci.yml)
- Add AUR/CI badges and tested distros section
- Bootstrap sudo+python3 before running install.sh
- Add --noconfirm to pacman install
- Mark text selection done, fix rows 17/18 line break
- Add backlog from review discussion, re-rate continuous scroll

### ⚙️ Miscellaneous Tasks

- Run unit tests on Arch, Ubuntu, and Fedora
- Revert unit tests to Ubuntu-only
## [0.1.0] - 2026-05-30

### 💼 Other

- Validate PyMuPDF as Poppler replacement

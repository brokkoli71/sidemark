## [0.5.0] - 2026-07-17 — Pictures, Copy, Paste

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

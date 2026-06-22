## [0.3.0] - 2026-06-22 — Reaching Out

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

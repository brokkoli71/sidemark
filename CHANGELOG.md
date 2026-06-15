## [unreleased]

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

### 📚 Documentation

- *(ideas)* Mark #44 done (commit 163b003)
- *(ideas)* Mark #43 done (commit 8787e81)

### ⚙️ Miscellaneous Tasks

- Run unit tests on Arch, Ubuntu, and Fedora
- Revert unit tests to Ubuntu-only
## [0.1.0] - 2026-05-30

### 💼 Other

- Validate PyMuPDF as Poppler replacement

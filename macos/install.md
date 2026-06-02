# Running Sidemark on macOS (Apple Silicon)

## Prerequisites

Install [Homebrew](https://brew.sh) if you haven't already, then install the
GTK stack and Python bindings:

```sh
brew bundle --file macos/Brewfile
pip3 install pymupdf
```

## Running

```sh
python3 sidemark.py
```

GTK on macOS uses the Quartz backend by default. No extra environment variables
are needed. Dark mode and accent colour are read from macOS system preferences
automatically.

## Notes

- PyMuPDF (`pymupdf`) must be installed via pip; there is no Homebrew formula.
- If GTK windows appear blank on first launch, try resizing the window once.
  This is a known Quartz/GTK4 first-draw quirk.
- For a self-contained `.app` bundle see `macos/build_app.sh`.

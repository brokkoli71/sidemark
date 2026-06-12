#!/bin/bash
# Takes a full-screen screenshot of Sidemark for the README.
# Switches to Hyprland workspace 9, opens demo.pdf, waits for the window,
# captures the focused monitor, then saves as screenshot.png.
#
# Prerequisites:  grim  (pacman -S grim  or  apt install grim)
# Run once first: python3 make_demo.py
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_PDF="$SCRIPT_DIR/sample.pdf"
OUT="$SCRIPT_DIR/screenshot.png"

if [[ ! -f "$DEMO_PDF" ]]; then
    echo "sample.pdf not found"
    exit 1
fi

# Remember current workspace, switch to 9, launch sidemark
PREV_WS=$(hyprctl activeworkspace -j | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
hyprctl dispatch workspace 9
sleep 0.3
/usr/bin/python3 "$SCRIPT_DIR/sidemark.py" "$DEMO_PDF" &
SIDEMARK_PID=$!

# Wait until the sidemark window appears (up to 10 s)
for _ in $(seq 1 50); do
    hyprctl clients -j 2>/dev/null | grep -qi sidemark && break
    sleep 0.2
done
# Let it finish rendering
sleep 2

# Capture the focused monitor
MONITOR=$(hyprctl monitors -j 2>/dev/null \
    | python3 -c "
import sys, json
ms = json.load(sys.stdin)
focused = next((m['name'] for m in ms if m.get('focused')), None)
print(focused or ms[0]['name'])
")
grim -o "$MONITOR" "$OUT"
echo "Saved $OUT"

kill "$SIDEMARK_PID" 2>/dev/null || true
hyprctl dispatch workspace "$PREV_WS"

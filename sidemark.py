#!/usr/bin/env /usr/bin/python3
import sys
import os
import signal
import math
import re
import subprocess
import threading
import tempfile
import logging
import atexit
import traceback
import hashlib
import io
import json
import shutil
import time
import datetime

RECENT_PATH = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "sidemark", "recent.json")
RECENT_MAX = 15


def _load_recent():
    """Recent files, newest first; entries whose file vanished are dropped."""
    try:
        with open(RECENT_PATH, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    return [it for it in items
            if isinstance(it, dict) and os.path.isfile(it.get("path", ""))]


def _add_recent(path):
    path = os.path.abspath(path)
    items = [it for it in _load_recent() if it.get("path") != path]
    items.insert(0, {"path": path, "ts": time.time()})
    del items[RECENT_MAX:]
    os.makedirs(os.path.dirname(RECENT_PATH), exist_ok=True)
    tmp = RECENT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f)
    os.replace(tmp, RECENT_PATH)


def _settings_path():
    # resolved at call time so tests can redirect via XDG_CONFIG_HOME
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "sidemark", "settings.json")


def _load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_setting(key, value):
    data = _load_settings()
    data[key] = value
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _fmt_clock(secs):
    """Seconds → m:ss (or h:mm:ss past an hour), for the presentation timer."""
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _notes_file_for_pdf(pdf_path):
    """The user-chosen notes file for this PDF, if one was set via the menu;
    otherwise None (callers fall back to notes_path_for)."""
    m = _load_settings().get("notes_files", {})
    if isinstance(m, dict):
        return m.get(os.path.abspath(pdf_path))
    return None


def _remember_notes_file(pdf_path, notes_path):
    m = _load_settings().get("notes_files", {})
    if not isinstance(m, dict):
        m = {}
    m[os.path.abspath(pdf_path)] = notes_path
    _save_setting("notes_files", m)


_USAGE = """\
sidemark — PDF viewer and annotator with a live Markdown notes sidebar

Usage:
  sidemark [OPTIONS] [FILE]

Arguments:
  FILE                  PDF, PowerPoint (.pptx), Markdown/text, or Sidemark
                        Deck (.smdeck) file to open. Any other file opens as
                        text in the notes panel.

Options:
  -h, --help            Show this help message and exit.
  -v, --verbose         Enable verbose (debug-level) logging.
      --page N          Open FILE at page N (0-based page index).
      --presentation    Start with a new presentation (Sidemark Deck);
                        --deck is an alias.
      --list-recent     Print recent files as "name<TAB>path" and exit
                        (for launcher integrations); no window is shown.

Examples:
  sidemark lecture.pdf
  sidemark --page 5 lecture.pdf
  sidemark notes.md
  sidemark --presentation
  sidemark talk.smdeck
"""

# Fast paths handled before any GTK import: print and exit straight away.
if __name__ == "__main__" and ("-h" in sys.argv[1:] or "--help" in sys.argv[1:]):
    print(_USAGE, end="")
    sys.exit(0)

# Launcher integrations (walker/elephant menus, rofi, …):
# print "name<TAB>path" lines and exit before any GTK import happens.
if __name__ == "__main__" and "--list-recent" in sys.argv[1:]:
    for _it in _load_recent():
        print(f"{os.path.basename(_it['path'])}\t{_it['path']}")
    sys.exit(0)

LOG_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "sidemark", "logs")
_log_path = None
_log_had_error = False
logger = logging.getLogger(__name__)


def _flag_errors(record):
    global _log_had_error
    if record.levelno >= logging.ERROR:
        _log_had_error = True
    return True


def _setup_logging(verbose=False):
    global _log_path
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%H:%M:%S")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    os.makedirs(LOG_DIR, exist_ok=True)
    _log_path = os.path.join(LOG_DIR, f"session_{os.getpid()}.log")
    file_handler = logging.FileHandler(_log_path)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addFilter(_flag_errors)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.info("session started" + (" (verbose)" if verbose else ""))

    def _excepthook(exc_type, exc, tb):
        logger.error("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    atexit.register(_cleanup_log)


def _cleanup_log():
    logger.info("session ended cleanly")
    logging.shutdown()
    if not _log_path:
        return
    if _log_had_error:
        # Keep the log — it is the only record of what went wrong.
        print(f"Errors were logged this session — log kept at {_log_path}", file=sys.stderr)
        return
    try:
        os.remove(_log_path)
    except OSError:
        pass


import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import (Gtk, Adw, Gdk, GLib, Gio, GObject, GtkSource,
                           Pango, PangoCairo, Graphene)
import cairo
import fitz          # PyMuPDF
import numpy as np


class PDFCanvas(Gtk.DrawingArea):
    SCROLL_FLIP_THRESHOLD = 3.0   # mouse-wheel notches past the page edge before flipping
    TOUCHPAD_FLIP_THRESHOLD = 180.0   # px of touchpad scroll past the edge before flipping
    WHEEL_PAN_STEP = 30.0         # px panned per mouse-wheel notch
    STRAIGHT_HOLD_MS = 500        # hold still this long mid-stroke to snap to a line

    def __init__(self, interactive=True):
        super().__init__()
        # interactive=False makes a view-only canvas (no input controllers) — used
        # by the presenter window, which mirrors the editor on a second screen.
        self._interactive = interactive
        self.document = None
        self.n_pages = 0
        self.current_page_idx = 0
        self.page = None
        self.page_width = 0
        self.page_height = 0

        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

        # {page_idx: [{"pts": [...], "color": (r,g,b,a), "width": float}]}
        self.all_strokes = {}
        self.current_stroke = []
        # GoodNotes-style straight-line snap: holding still mid-stroke collapses
        # the in-progress stroke to a line from its start to the cursor
        self._straight_mode = False
        self._straight_timer = None

        # undo: ("draw", page, stroke[, group]) | ("erase", page, idx, stroke, group);
        # erase ops of one drag gesture share a group and undo together, as do the
        # per-line strokes of one text-highlight (a draw group).
        # redo holds lists of ops exactly as undo_last popped them.
        self._undo_stack = []
        self._redo_stack = []
        self._erase_group = 0
        self._draw_group = 0

        self.pen_color = (0.05, 0.05, 0.8)   # RGB — stroke alpha lives in "opacity"
        self.pen_width = 2.0
        # freehand smoothing strength 0..1 (Laplacian passes applied on commit)
        self.smoothing = 0.5
        # highlighter mode: wide translucent strokes (PDF CA key via annot.set_opacity)
        self.highlighter = False
        self.hl_color = (1.0, 0.85, 0.0)
        self.hl_width = 12.0
        self.hl_opacity = 0.40
        # "free" = freehand highlighter strokes; "text" = drag selects text
        # (reading order) and lays one highlight rectangle per line over the
        # word boxes, still stored as ink. Long-press the highlighter tool.
        self.highlight_style = "free"
        self.surround_color = (0.910, 0.867, 0.824)  # overridden by window with theme color
        self.zoom_accent = (0.52, 0.70, 0.30)        # overridden with theme accent

        # presentation stack preview: the next page peeks out from behind the
        # current one, down-right, like the next card in a stack (editor canvas
        # only, while presenting — see set_stack_preview)
        self.stack_preview = False
        self._stack_surface = None
        self._stack_scale = 0.0
        self._stack_page_size = (0, 0)
        self._stack_below = False   # next page under the current one, not beside

        # live mirroring of an in-progress stroke: a view-only mirror canvas
        # points live_stroke_src at the editor canvas and draws its
        # current_stroke; the editor calls on_live_draw on every stroke motion
        # so the mirror redraws while the ink is still being laid down
        self.live_stroke_src = None
        self.on_live_draw = None

        self.on_page_changed = None    # callback(current_idx, n_pages)
        self.on_page_will_change = None  # callback() before leaving the page (commit notes)
        self.on_nav_button = None     # callback(delta: int) for back/forward buttons
        self.on_change = None         # callback() whenever strokes are modified
        self.on_anchor_placed = None   # callback(page_idx, pdf_x, pdf_y)
        self.on_anchor_clicked = None  # callback(anchor_index)
        self.on_anchor_moved = None    # callback(anchor_index, pdf_x, pdf_y)
        self.on_callout_placed = None  # callback(pdf_x, pdf_y) — for the last placed anchor
        self.on_callout_moved = None   # callback(anchor_index, pdf_x, pdf_y)
        self.on_textbox_placed = None  # callback(page_idx, pdf_x, pdf_y) — standalone box
        self.on_textbox_moved = None   # callback(textbox_index, pdf_x, pdf_y)
        self.on_user_action = None     # callback() once per completed draw/erase gesture
        self.on_canvas_press = None    # callback() on any press in the canvas (clears thumb selection)
        self.on_lasso_selection = None # callback(has_selection: bool) when the lasso set changes
        self.on_nav_history = None     # callback(can_go_back: bool) when the link back-stack changes

        # link navigation: following an internal (GOTO) link — e.g. a footnote or
        # citation reference — pushes the reading location here so it can be
        # restored with nav_back() (Alt+Left). Each entry: (page, ox, oy, scale).
        self._nav_history = []

        # {page_idx: [anchor dict from _parse_anchors, ...]}
        self._anchors = {}
        self._active_anchors = set()  # indices highlighted on current page
        # drag-to-reposition: index of the anchor being dragged, and whether the
        # drag moved far enough to count as a move (vs. a click that jumps notes)
        self._anchor_dragging = None
        self._anchor_drag_moved = False
        self._hovering_anchor = False

        # Ctrl+Alt+drag: anchor placed at press (GestureClick), callout box
        # placed at release when the drag travelled far enough
        self._callout_dragging = False
        self._callout_start = None    # screen (x, y)
        self._callout_cur = None
        # drag-to-reposition a placed callout box (mirrors anchor dragging):
        # index of the anchor whose callout is moving, the grab offset in PDF
        # units, and whether it moved far enough to commit
        self._callout_moving = None
        self._callout_move_offset = (0.0, 0.0)
        self._callout_move_moved = False
        # screen-space rects of callout boxes from the last draw, for hit-testing:
        # [(anchor_index, bx, by, bw, bh), ...]
        self._callout_boxes = []

        # standalone text boxes (#56): like a callout but with no anchor/arrow.
        # {page_idx: [textbox dict from _parse_textboxes, ...]}
        self._textboxes = {}
        self._textbox_boxes = []       # [(index, bx, by, bw, bh), ...] last draw
        self._textbox_moving = None
        self._textbox_move_offset = (0.0, 0.0)
        self._textbox_move_moved = False

        self.search_rects = []          # fitz.Rect hits for current page
        self.search_current_rect = None # the active match rect

        # zoom-to-region state
        self._zoom_stack = []          # [(scale, offset_x, offset_y), ...]
        self._zoom_selecting = False
        self._zoom_start = None        # screen (x, y)
        self._zoom_end = None          # screen (x, y), constrained

        # scroll-past-boundary page flip: signed accumulator of scroll notches
        # while the page edge is already visible; flips after the threshold
        self._scroll_past = 0.0

        # view-fit tracking: while the page is in "fitted" state, canvas
        # resizes (sidebar toggle, window resize) re-fit; after any manual
        # zoom/pan they keep the viewport center anchored instead
        self._is_fitted = False
        self._last_size = (0, 0)
        self.connect("resize", self._on_resize)

        # cached page surface
        self._page_surface = None      # cairo.ImageSurface rendered at _surface_scale
        self._surface_scale = 0.0
        self._rerender_id = None       # GLib timeout handle
        self._needs_fit = False        # refit on first draw after load (canvas may not be allocated yet)

        self._erasing = False

        # lasso stroke selection (the "lasso" tool): a freehand loop selects
        # ink strokes on the page, which can then be moved / deleted / recoloured.
        self._lassoing = False
        self._lasso_path = []          # screen-space points of the loop in progress
        self._selected_strokes = []    # references into self.strokes, selected
        self._lasso_moving = False
        self._lasso_move_start = None
        self._lasso_move_orig = []     # original pts of selected strokes at drag begin
        self._lasso_moved = False

        self._panning = False
        self._pan_start_offset = (0.0, 0.0)

        self._ignoring = False  # True while a button-8/9 drag sequence is active

        # after a pinch, the finger left on the screen pans the page (never
        # draws) until it too is lifted. _post_pinch_anchor latches the drag
        # offset at the moment the pinch ended so panning has no jump.
        self._post_pinch = False
        self._post_pinch_anchor = None
        self._post_pinch_base = (0.0, 0.0)

        self._thumb_panning = False
        self._thumb_origin = (0.0, 0.0)
        self._thumb_start_offset = (0.0, 0.0)

        # active tool: one of pen / highlighter / select / pan / zoom / anchor.
        # The tool decides what a *plain* (unmodified) drag does; the modifier
        # gestures (Ctrl/Alt/Shift/Ctrl+Alt) always work regardless, and the
        # selected tool is just the modifier-free shortcut for the same actions.
        # ``highlighter`` and ``select_mode`` are kept in sync as the pen-attr /
        # text-select flags the rest of the canvas already reads.
        self.tool = "pen"
        self.select_mode = False
        # set for the duration of a Ctrl+Shift+drag: a one-off highlighter stroke
        self._temp_highlighter = False
        # transient tool implied by the modifiers currently held down — surfaced
        # to the header so the matching tool button lights up (discoverability).
        self.on_modifier_tool = None   # callback(tool_name_or_None)

        # word-level text selection (Alt+drag) and link opening (Alt+click)
        self._text_selecting = False
        # True while a text-highlight drag is in progress (highlighter tool in
        # "text" style): same word-selection path, but commits highlight ink
        self._text_highlighting = False
        self._alt_start = (0.0, 0.0)
        self._selected_words = []   # fitz word tuples currently highlighted
        self._page_words = []       # cached for current page
        self._ordered_words = []    # _page_words sorted in reading order (block,line,word)
        # "reading" = press-to-release contiguous run (like a normal PDF viewer);
        # "rect" = rectangular marquee. Long-press the select tool to switch.
        self.select_style = "reading"
        self.on_text_copied = None  # callback(text_or_None)

        # link hover hint / modifier tracking
        self._alt_held = False
        self._ctrl_held = False
        self._shift_held = False
        self._hover_x = 0.0
        self._hover_y = 0.0
        self._hovered_link_rect = None


        self.set_draw_func(self._draw)
        self.set_focusable(True)
        self.set_can_focus(True)

        self._mouse_x = 0.0
        self._mouse_y = 0.0
        if not interactive:
            return   # view-only mirror: no drawing/pan/zoom/key input

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave",  self._on_motion_leave)
        self.add_controller(motion)
        self._mouse_x = 0.0
        self._mouse_y = 0.0

        # No DISCRETE flag: it quantises touchpad two-finger scroll into wheel
        # notches, which axis-locks to horizontal *or* vertical. Smooth deltas
        # let a two-finger drag pan diagonally; we tell touchpad (SURFACE) from
        # mouse-wheel (WHEEL) input per-event via get_unit().
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        drag = Gtk.GestureDrag.new()
        drag.set_button(0)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        # Two-finger pinch zoom (touchpad/touchscreen). GestureZoom reports a
        # cumulative scale-delta since the pinch began; we apply it relative to
        # the scale at begin, anchored on the pinch centroid.
        zoom = Gtk.GestureZoom.new()
        zoom.connect("begin", self._on_pinch_begin)
        zoom.connect("scale-changed", self._on_pinch_scale)
        zoom.connect("end", self._on_pinch_end)
        zoom.connect("cancel", lambda g, seq: self._on_pinch_end(g, seq))
        self.add_controller(zoom)
        self._pinch_start_scale = None
        self._pinch_anchor_pdf = None

        # MX Master thumb button (btn 10): hold to pan, scroll-while-held to
        # zoom. EventControllerLegacy is the only layer that reliably reports
        # button-10 press AND release through pointer movement — every gesture
        # API cancels the sequence once a drag claims it (extras/probe_thumb.py).
        thumb = Gtk.EventControllerLegacy()
        thumb.connect("event", self._on_thumb_event)
        self.add_controller(thumb)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_click_pressed)
        self.add_controller(click)



        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed",  self._on_modifier_key, True)
        key.connect("key-released", self._on_modifier_key, False)
        self.add_controller(key)


    # ── page management ──────────────────────────────────────────────────────

    def load(self, path):
        self.document = fitz.open(path)
        self.n_pages = len(self.document)
        self.current_stroke = []
        self.all_strokes = {}
        self._undo_stack = []
        self._redo_stack = []
        self._erase_group = 0
        self._draw_group = 0
        self._nav_history = []
        if self.on_nav_history:
            self.on_nav_history(False)
        total_annots = 0
        for i in range(self.n_pages):
            page = self.document[i]   # keep reference alive while reading annotations
            for annot in page.annots(types=[fitz.PDF_ANNOT_INK]):
                color = tuple(annot.colors.get("stroke", (0.05, 0.05, 0.8)))
                width = annot.border.get("width", 2.0)
                # fitz reports -1.0 (or 1.0) when the PDF CA key is unset
                opacity = annot.opacity if 0 < annot.opacity < 1 else 1.0
                for polyline in annot.vertices:
                    if polyline:
                        self.all_strokes.setdefault(i, []).append({
                            "pts":   [tuple(pt) for pt in polyline],
                            "color": color,
                            "width": width,
                            "opacity": opacity,
                        })
                        total_annots += 1
        logger.info(f"load: {path} — {self.n_pages} pages, {total_annots} strokes loaded")
        self._load_page(0)

    def _load_page(self, idx, keep_view=False):
        self.clear_lasso_selection()   # selection is per-page and transient
        self.current_page_idx = idx
        self.page = self.document[idx]
        self.page_width  = self.page.rect.width
        self.page_height = self.page.rect.height
        self._page_surface = None
        self._surface_scale = 0.0
        self._stack_surface = None    # the next page changed too
        self._scroll_past = 0.0
        if self._rerender_id is not None:
            GLib.source_remove(self._rerender_id)
            self._rerender_id = None
        if keep_view:
            self._needs_fit = False   # caller keeps zoom and positions the view
        else:
            self._needs_fit = True    # re-fit on first draw with real canvas dimensions
        self._page_words = self.page.get_text("words")   # cache for text selection
        # reading order: MuPDF segments columns into separate blocks, so
        # (block, line, word) gives column-first order for free
        self._ordered_words = sorted(self._page_words, key=lambda w: (w[5], w[6], w[7]))
        self._selected_words = []
        self.queue_draw()
        if self.on_page_changed:
            self.on_page_changed(idx, self.n_pages)

    def go_to_page(self, idx, keep_view=False):
        if not self.document:
            return
        idx = max(0, min(self.n_pages - 1, idx))
        if idx != self.current_page_idx:
            if self.on_page_will_change:
                self.on_page_will_change()
            self._load_page(idx, keep_view=keep_view)

    @property
    def strokes(self):
        return self.all_strokes.setdefault(self.current_page_idx, [])

    def _pen_attrs(self):
        """(color, width, opacity) of the active drawing tool. ``_temp_highlighter``
        is the transient Ctrl+Shift+drag highlighter, regardless of sticky tool."""
        if self.highlighter or self._temp_highlighter:
            return self.hl_color, self.hl_width, self.hl_opacity
        return self.pen_color, self.pen_width, 1.0

    # ── layout ───────────────────────────────────────────────────────────────

    # presentation stack look: the next page is drawn at STACK_NEXT_SCALE × the
    # current page's scale, behind its right edge (STACK_OVERLAP px under it)
    # and bottom-anchored to the canvas — mostly visible, clearly behind
    STACK_NEXT_SCALE = 0.45
    STACK_OVERLAP = 18
    STACK_MARGIN = 12

    def _fit_page(self, w=None, h=None):
        w = w or self.get_width() or 800
        h = h or self.get_height() or 600
        if not (self.page_width and self.page_height):
            return
        if self.stack_preview:
            # reserve room for the smaller next page on ONE side — beside the
            # page (the other axis fits normally, so pages keep their size and
            # position between flips) or, when the canvas is tall enough that
            # it wins the current page more space (e.g. wide notes panel),
            # underneath it. Pick whichever fit leaves the current page larger.
            k, o, m = self.STACK_NEXT_SCALE, self.STACK_OVERLAP, self.STACK_MARGIN
            s_right = min((w - 2 * m + o) / (self.page_width * (1 + k)),
                          (h - 2 * m) / self.page_height)
            s_below = min((w - 2 * m) / self.page_width,
                          (h - 2 * m + o) / (self.page_height * (1 + k)))
            self._stack_below = s_below > s_right
            if self._stack_below:
                self.scale = s_below
                foot_h = self.page_height * self.scale * (1 + k) - o
                self.offset_x = (w - self.page_width * self.scale) / 2
                self.offset_y = (h - foot_h) / 2
            else:
                self.scale = s_right
                foot_w = self.page_width * self.scale * (1 + k) - o
                self.offset_x = (w - foot_w) / 2
                self.offset_y = (h - self.page_height * self.scale) / 2
        else:
            self.scale = min(w / self.page_width, h / self.page_height) * 0.95
            self.offset_x = (w - self.page_width * self.scale) / 2
            self.offset_y = (h - self.page_height * self.scale) / 2
        self._is_fitted = True

    def _on_resize(self, _area, width, height):
        old_w, old_h = self._last_size
        self._last_size = (width, height)
        if not self.page or not old_w or not old_h or (width, height) == (old_w, old_h):
            return
        if self._is_fitted:
            self._fit_page(width, height)
            self._schedule_rerender()
        else:
            # keep the PDF point at the old viewport center centered
            cx_pdf = (old_w / 2 - self.offset_x) / self.scale
            cy_pdf = (old_h / 2 - self.offset_y) / self.scale
            self.offset_x = width / 2 - cx_pdf * self.scale
            self.offset_y = height / 2 - cy_pdf * self.scale
        self.queue_draw()

    def _page_to_surface(self, page, logical_scale):
        """Render a fitz page to a cairo surface at logical_scale × the widget's
        device scale factor."""
        sf = self.get_scale_factor()
        device_scale = logical_scale * sf
        pix = page.get_pixmap(matrix=fitz.Matrix(device_scale, device_scale), alpha=True, annots=False)
        w, h = pix.width, pix.height
        # fitz RGBA → cairo ARGB32 (BGRA in memory on little-endian): swap R and B channels
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, 4).copy()
        arr[:, :, [0, 2]] = arr[:, :, [2, 0]]
        surf = cairo.ImageSurface.create_for_data(arr, cairo.FORMAT_ARGB32, w, h)
        surf.set_device_scale(sf, sf)
        return surf

    def _rerender_now(self):
        if not self.page:
            return
        logical_scale = min(max(self.scale, 0.5), 4.0)
        self._page_surface = self._page_to_surface(self.page, logical_scale)
        self._surface_scale = logical_scale

    def set_stack_preview(self, on):
        """Toggle the next-page-behind-current stack look (presentation mode)."""
        if on == self.stack_preview:
            return
        self.stack_preview = on
        self._stack_surface = None
        if self._is_fitted and self.page:
            self._fit_page()
            self._schedule_rerender()
        self.queue_draw()

    def _draw_stack_peek(self, ctx, width, height):
        """Draw the next page — smaller and very slightly greyed — behind the
        current page's right edge (or its bottom edge, when _fit_page chose
        the underneath layout), anchored to the canvas on the free axis, so
        the presenter sees what's coming and the pages read as a stack. Its
        position depends only on the page's near edge, so pages don't shift
        between flips. Only meaningful in the fitted presentation view;
        skipped on the last page."""
        nxt = self.current_page_idx + 1
        if self.document is None or nxt >= self.n_pages:
            return
        next_scale = self.scale * self.STACK_NEXT_SCALE
        logical_scale = min(max(next_scale, 0.2), 4.0)
        if self._stack_surface is None or self._stack_scale != logical_scale:
            try:
                page = self.document[nxt]
                self._stack_surface = self._page_to_surface(page, logical_scale)
                self._stack_scale = logical_scale
                self._stack_page_size = (page.rect.width, page.rect.height)
            except Exception:
                logger.error("stack peek render failed:\n" + traceback.format_exc())
                return
        w = self._stack_page_size[0] * next_scale
        h = self._stack_page_size[1] * next_scale
        if self._stack_below:
            # just under the current page's bottom edge, right-anchored
            x = width - self.STACK_MARGIN - w
            y = self.offset_y + self.page_height * self.scale - self.STACK_OVERLAP
        else:
            # just right of the current page's edge, bottom-anchored
            x = self.offset_x + self.page_width * self.scale - self.STACK_OVERLAP
            y = height - self.STACK_MARGIN - h
        ctx.save()
        ctx.rectangle(x, y, w, h)
        ctx.set_source_rgb(1, 1, 1)
        ctx.fill()
        ctx.translate(x, y)
        blit_scale = next_scale / self._stack_scale
        ctx.scale(blit_scale, blit_scale)
        ctx.set_source_surface(self._stack_surface, 0, 0)
        ctx.get_source().set_filter(cairo.Filter.BILINEAR)
        ctx.paint()
        ctx.restore()
        # grey it out very slightly + hairline, so it isn't the live slide
        ctx.set_source_rgba(0.5, 0.5, 0.5, 0.10)
        ctx.rectangle(x, y, w, h)
        ctx.fill()
        ctx.set_source_rgba(0, 0, 0, 0.30)
        ctx.set_line_width(1)
        ctx.rectangle(x + 0.5, y + 0.5, w, h)
        ctx.stroke()

    def _schedule_rerender(self):
        if self._rerender_id is not None:
            GLib.source_remove(self._rerender_id)
        self._rerender_id = GLib.timeout_add(120, self._on_rerender_timeout)

    def _on_rerender_timeout(self):
        self._rerender_id = None
        self._rerender_now()
        self.queue_draw()
        return False

    def _screen_to_pdf(self, sx, sy):
        return (sx - self.offset_x) / self.scale, (sy - self.offset_y) / self.scale

    def _pdf_to_screen(self, px, py):
        return px * self.scale + self.offset_x, py * self.scale + self.offset_y

    # ── drawing ───────────────────────────────────────────────────────────────

    def _draw(self, area, ctx, width, height):
        ctx.set_source_rgb(*self.surround_color)
        ctx.paint()

        if self.page is None:
            r, g, b = self.surround_color
            # Placeholder text: foreground-ish tint derived from surround
            ctx.set_source_rgba(r * 0.6, g * 0.6, b * 0.6, 0.8)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            ctx.set_font_size(16)
            text = "Open a PDF to begin"
            e = ctx.text_extents(text)
            ctx.move_to((width - e.width) / 2, (height + e.height) / 2)
            ctx.show_text(text)
            return

        if self._needs_fit and width > 0 and height > 0:
            self._needs_fit = False
            self._fit_page()
        elif self.offset_x == 0 and self.offset_y == 0 and self.scale == 1.0:
            self._fit_page()

        # presenting: the next page shows behind the current one (stack look)
        if self.stack_preview and self._is_fitted:
            self._draw_stack_peek(ctx, width, height)

        ctx.set_source_rgb(1, 1, 1)
        ctx.rectangle(self.offset_x, self.offset_y,
                      self.page_width * self.scale, self.page_height * self.scale)
        ctx.fill()

        if self._page_surface is None:
            self._rerender_now()

        ctx.save()
        ctx.translate(self.offset_x, self.offset_y)
        blit_scale = self.scale / self._surface_scale
        ctx.scale(blit_scale, blit_scale)
        ctx.set_source_surface(self._page_surface, 0, 0)
        ctx.get_source().set_filter(cairo.Filter.BILINEAR)
        ctx.paint()
        ctx.restore()

        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)

        to_draw = self.strokes[:]
        if self.current_stroke:
            color, width, opacity = self._pen_attrs()
            to_draw.append({"pts": self.current_stroke,
                             "color": color,
                             "width": width,
                             "opacity": opacity})
        # mirror canvas: also draw the editor's stroke still being laid down
        src = self.live_stroke_src
        if (src is not None and src.current_stroke
                and src.current_page_idx == self.current_page_idx):
            color, width, opacity = src._pen_attrs()
            to_draw.append({"pts": src.current_stroke,
                            "color": color,
                            "width": width,
                            "opacity": opacity})

        ctx.save()
        ctx.translate(self.offset_x, self.offset_y)
        ctx.scale(self.scale, self.scale)
        for stroke in to_draw:
            pts = stroke["pts"]
            r, g, b = stroke["color"]
            ctx.set_source_rgba(r, g, b, stroke.get("opacity", 1.0))
            ctx.set_line_width(stroke["width"])   # PDF units — scales with zoom
            if len(pts) < 2:
                if pts:
                    ctx.arc(pts[0][0], pts[0][1], stroke["width"] / 2, 0, 2 * math.pi)
                    ctx.fill()
                continue
            ctx.move_to(*pts[0])
            for pt in pts[1:]:
                ctx.line_to(*pt)
            ctx.stroke()
        ctx.restore()

        self._draw_lasso(ctx)

        # standalone text boxes (#56) — drawn before anchors so anchor circles
        # stay on top if they overlap
        self._textbox_boxes = []
        for i, t in enumerate(self._textboxes.get(self.current_page_idx, [])):
            self._draw_text_box(ctx, t, i)

        # anchor markers
        anchors = self._anchors.get(self.current_page_idx, [])
        self._callout_boxes = []
        if anchors:
            # callout boxes go under the circles so an anchor inside a box stays visible
            for i, a in enumerate(anchors):
                if a.get("callout") and a.get("text"):
                    self._draw_callout(ctx, a, i)
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            r, g, b = self.zoom_accent
            radius = 8.0 / self.scale
            for i, a in enumerate(anchors):
                ax, ay = a["x"], a["y"]
                if i in self._active_anchors:
                    ctx.set_source_rgba(r, g, b, 0.3)
                    ctx.arc(ax, ay, radius * 1.9, 0, 2 * math.pi)
                    ctx.fill()
                ctx.set_source_rgba(r, g, b, 0.88)
                ctx.arc(ax, ay, radius, 0, 2 * math.pi)
                ctx.fill()
                ctx.set_source_rgb(1, 1, 1)
                ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
                ctx.set_font_size(radius * 1.3)
                label = str(i + 1)
                ext = ctx.text_extents(label)
                ctx.move_to(ax - ext.width / 2 - ext.x_bearing, ay - ext.height / 2 - ext.y_bearing)
                ctx.show_text(label)
            ctx.restore()

        # callout placement preview (Ctrl+Alt+drag in progress)
        if self._callout_dragging and self._callout_start and self._callout_cur:
            ar, ag, ab = self.zoom_accent
            ctx.set_source_rgba(ar, ag, ab, 0.8)
            ctx.set_line_width(1.5)
            ctx.set_dash([5.0, 3.0])
            ctx.move_to(*self._callout_start)
            ctx.line_to(*self._callout_cur)
            ctx.stroke()
            ctx.set_dash([])

        # search highlights
        if self.search_rects:
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            for rect in self.search_rects:
                if rect == self.search_current_rect:
                    ctx.set_source_rgba(1.0, 0.55, 0.0, 0.55)
                else:
                    ctx.set_source_rgba(1.0, 0.88, 0.0, 0.40)
                ctx.rectangle(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0)
                ctx.fill()
            ctx.restore()

        # word-selection highlights
        if self._selected_words:
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            ctx.set_source_rgba(0.2, 0.5, 0.9, 0.35)
            for w in self._selected_words:
                ctx.rectangle(w[0], w[1], w[2] - w[0], w[3] - w[1])
                ctx.fill()
            ctx.restore()

        # hovered link highlight (Alt held)
        if self._hovered_link_rect is not None:
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            r = self._hovered_link_rect
            ar, ag, ab = self.zoom_accent
            ctx.set_source_rgba(ar, ag, ab, 0.15)
            ctx.rectangle(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)
            ctx.fill()
            ctx.set_source_rgba(ar, ag, ab, 0.7)
            ctx.set_line_width(1.0 / self.scale)
            ctx.rectangle(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)
            ctx.stroke()
            ctx.restore()

        # zoom-selection rubber-band
        if self._zoom_selecting and self._zoom_start and self._zoom_end:
            x1 = min(self._zoom_start[0], self._zoom_end[0])
            y1 = min(self._zoom_start[1], self._zoom_end[1])
            rw = abs(self._zoom_end[0] - self._zoom_start[0])
            rh = abs(self._zoom_end[1] - self._zoom_start[1])
            ar, ag, ab = self.zoom_accent
            ctx.set_source_rgba(ar, ag, ab, 0.15)
            ctx.rectangle(x1, y1, rw, rh)
            ctx.fill()
            ctx.set_source_rgba(ar, ag, ab, 0.85)
            ctx.set_line_width(1.5)
            ctx.set_dash([5.0, 3.0])
            ctx.rectangle(x1, y1, rw, rh)
            ctx.stroke()
            ctx.set_dash([])

    def _note_box_layout(self, ctx, text):
        """A Pango layout for a callout / text box, rendering symbols + markup."""
        layout = PangoCairo.create_layout(ctx)
        desc = Pango.FontDescription("Sans")
        desc.set_absolute_size(max(6.0, 8.5 * self.scale) * Pango.SCALE)
        layout.set_font_description(desc)
        layout.set_width(int(170 * self.scale * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        # render symbols (always), super/subscripts and inline Markdown; fall
        # back to plain symbolized text if the generated markup is ever invalid
        try:
            layout.set_markup(_notes_to_pango_markup(text))
        except GLib.Error:
            layout.set_text(_symbolize(text))
        return layout

    def _paint_note_box(self, ctx, layout, bx, by, bw, bh, pad):
        """White rounded-less box with an accent border and the laid-out text."""
        ar, ag, ab = self.zoom_accent
        ctx.set_source_rgba(1, 1, 1, 0.95)
        ctx.rectangle(bx, by, bw, bh)
        ctx.fill()
        ctx.set_source_rgba(ar, ag, ab, 0.9)
        ctx.set_line_width(max(1.0, 1.2 * self.scale))
        ctx.rectangle(bx, by, bw, bh)
        ctx.stroke()
        ctx.set_source_rgb(0.1, 0.1, 0.1)
        ctx.move_to(bx + pad, by + pad)
        PangoCairo.show_layout(ctx, layout)

    def _draw_callout(self, ctx, a, idx=None):
        """Wrapped note text in a box at the callout position, with an arrow
        from the anchor circle to the box. Drawn in screen space for crisp
        text; all dimensions scale with zoom."""
        ax, ay = self._pdf_to_screen(a["x"], a["y"])
        cx, cy = self._pdf_to_screen(*a["callout"])
        pad = max(3.0, 5.0 * self.scale)
        layout = self._note_box_layout(ctx, a["text"])
        tw, th = layout.get_pixel_size()
        bx, by = cx, cy
        bw, bh = tw + 2 * pad, th + 2 * pad
        if idx is not None:
            self._callout_boxes.append((idx, bx, by, bw, bh))

        # arrow from anchor to the nearest point on the box edge
        attach_x = min(max(ax, bx), bx + bw)
        attach_y = min(max(ay, by), by + bh)
        ar, ag, ab = self.zoom_accent
        dxv, dyv = attach_x - ax, attach_y - ay
        dist = math.hypot(dxv, dyv)
        if dist > 1.0:
            ctx.set_source_rgba(ar, ag, ab, 0.85)
            ctx.set_line_width(max(1.0, 1.5 * self.scale))
            ctx.move_to(ax, ay)
            ctx.line_to(attach_x, attach_y)
            ctx.stroke()
            ux, uy = dxv / dist, dyv / dist
            head = max(4.0, 6.0 * self.scale)
            base_x, base_y = attach_x - ux * head, attach_y - uy * head
            ctx.move_to(attach_x, attach_y)
            ctx.line_to(base_x - uy * head * 0.5, base_y + ux * head * 0.5)
            ctx.line_to(base_x + uy * head * 0.5, base_y - ux * head * 0.5)
            ctx.close_path()
            ctx.fill()

        self._paint_note_box(ctx, layout, bx, by, bw, bh, pad)

    def _draw_text_box(self, ctx, t, idx=None):
        """A standalone text box (#56) — like a callout but with no anchor or
        arrow; just the box with its rendered note text at its PDF position."""
        cx, cy = self._pdf_to_screen(t["x"], t["y"])
        pad = max(3.0, 5.0 * self.scale)
        layout = self._note_box_layout(ctx, t["text"] or " ")
        tw, th = layout.get_pixel_size()
        bw, bh = tw + 2 * pad, th + 2 * pad
        if idx is not None:
            self._textbox_boxes.append((idx, cx, cy, bw, bh))
        self._paint_note_box(ctx, layout, cx, cy, bw, bh, pad)

    # ── input handlers ────────────────────────────────────────────────────────

    def _on_thumb_event(self, ctrl, event):
        if event is None:   # PyGObject sometimes fails to marshal the arg
            event = ctrl.get_current_event()
        if event is None:
            return False
        t = event.get_event_type()
        if t == Gdk.EventType.BUTTON_PRESS and event.get_button() == 10:
            logger.debug(f"thumb pan start ({self._mouse_x:.0f},{self._mouse_y:.0f})")
            self._thumb_panning = True
            self._is_fitted = False
            self._thumb_origin = (self._mouse_x, self._mouse_y)
            self._thumb_start_offset = (self.offset_x, self.offset_y)
        elif t == Gdk.EventType.BUTTON_RELEASE and event.get_button() == 10:
            logger.debug("thumb pan end")
            self._thumb_panning = False
        return False

    def _on_motion(self, _ctrl, x, y):
        if self._thumb_panning:
            self.offset_x = self._thumb_start_offset[0] + (x - self._thumb_origin[0])
            self.offset_y = self._thumb_start_offset[1] + (y - self._thumb_origin[1])
            self.queue_draw()
        self._mouse_x = x
        self._mouse_y = y
        self._hover_x, self._hover_y = x, y
        self._update_link_hover()
        self._update_anchor_hover(x, y)

    def _update_anchor_hover(self, x, y):
        """Show a grab cursor over anchor circles and callout boxes so it's clear
        they can be dragged. Yields to link-hover (Alt) and stays out of its way."""
        over = (self._hovered_link_rect is None and not self._alt_held
                and self.page is not None
                and (self._anchor_hit_test(x, y) is not None
                     or self._callout_hit_test(x, y) is not None
                     or self._textbox_hit_test(x, y) is not None))
        if over == self._hovering_anchor:
            return
        self._hovering_anchor = over
        if over:
            self.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        elif self._hovered_link_rect is None:
            self.set_cursor(self._default_cursor())

    def _on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        # Touchpad two-finger scroll arrives as smooth SURFACE-unit deltas (~1px
        # each, both axes at once); a mouse wheel as ±1 WHEEL notches. Pan speed
        # and the page-flip resistance differ per source so both feel natural.
        smooth = ctrl.get_unit() == Gdk.ScrollUnit.SURFACE
        # zoom on Ctrl+scroll, or plain scroll while thumb pan mode is latched
        if not (state & Gdk.ModifierType.CONTROL_MASK) and not self._thumb_panning:
            flip_threshold = (self.TOUCHPAD_FLIP_THRESHOLD if smooth
                              else self.SCROLL_FLIP_THRESHOLD)
            if self._handle_boundary_flip(dx, dy, flip_threshold):
                return True
            self._scroll_past = 0.0
            step = 1.0 if smooth else self.WHEEL_PAN_STEP
            self.offset_x -= dx * step
            self.offset_y -= dy * step
            self._clamp_scroll_offset()
            self._is_fitted = False
            self.queue_draw()
            return True
        if smooth:
            factor = max(0.5, min(2.0, 1.0 - dy * 0.02))
        else:
            factor = 0.9 if dy > 0 else 1.1
        self._zoom_at(factor, self._mouse_x, self._mouse_y)
        return True

    def _clamp_scroll_offset(self):
        """Stop wheel/touchpad scrolling from pushing the document's outer
        boundaries into empty space: the first page can't scroll below its top,
        the last page can't rise above its bottom. (Within the document, edges
        flip to the next page; drag-panning is intentionally left unclamped, so
        you can still pan a page freely when you really want to.)"""
        if self.page is None:
            return
        ch = self.get_height() or 600
        page_h = self.page_height * self.scale
        if page_h <= ch:
            # Page shorter than the viewport (zoomed out, or a wide/landscape
            # page): there's no vertical scroll room, so pin the document's ends
            # to the centred position rather than letting a fast scroll to the
            # start/end slam the page against the top or bottom of the window.
            center = (ch - page_h) / 2
            if self.current_page_idx <= 0:
                self.offset_y = min(self.offset_y, center)
            if self.current_page_idx >= self.n_pages - 1:
                self.offset_y = max(self.offset_y, center)
            return
        lo = ch - page_h   # top-most scroll (page bottom at viewport bottom)
        hi = 0.0           # bottom-most scroll (page top at viewport top)
        if self.current_page_idx <= 0:
            self.offset_y = min(self.offset_y, hi)
        if self.current_page_idx >= self.n_pages - 1:
            self.offset_y = max(self.offset_y, lo)

    def _zoom_at(self, factor, cx, cy):
        """Multiply the zoom by ``factor`` keeping the document point under
        (cx, cy) fixed on screen. Shared by Ctrl+scroll, thumb-scroll zoom and
        the pinch gesture."""
        old_scale = self.scale
        new_scale = max(0.1, min(20.0, old_scale * factor))
        if new_scale == old_scale:
            return
        pdf_x = (cx - self.offset_x) / old_scale
        pdf_y = (cy - self.offset_y) / old_scale
        self.scale = new_scale
        self._is_fitted = False
        self.offset_x = cx - pdf_x * self.scale
        self.offset_y = cy - pdf_y * self.scale
        if self._thumb_panning:
            # rebase the pan origin so the next motion event doesn't jump
            self._thumb_origin = (cx, cy)
            self._thumb_start_offset = (self.offset_x, self.offset_y)
        self._schedule_rerender()
        self.queue_draw()

    def _on_pinch_begin(self, gesture, _seq):
        if self.page is None:
            return
        ok, cx, cy = gesture.get_bounding_box_center()
        if not ok:
            cx, cy = self._mouse_x, self._mouse_y
        self._pinch_start_scale = self.scale
        # document point under the pinch centroid — kept under the (moving)
        # centroid for the rest of the gesture, so fingers stay anchored to the
        # page and pinch zooms *and* pans at once
        self._pinch_anchor_pdf = ((cx - self.offset_x) / self.scale,
                                  (cy - self.offset_y) / self.scale)
        # a single-finger drag may have already begun a stroke (a dot) before
        # the second finger landed — discard it and stop the drag from drawing
        self.current_stroke = []
        self._ignoring = True

    def _on_pinch_scale(self, gesture, delta):
        if self._pinch_start_scale is None or self.page is None:
            return
        ok, cx, cy = gesture.get_bounding_box_center()
        if not ok:
            return
        new_scale = max(0.1, min(20.0, self._pinch_start_scale * delta))
        pdf_x, pdf_y = self._pinch_anchor_pdf
        self.scale = new_scale
        self._is_fitted = False
        self.offset_x = cx - pdf_x * new_scale
        self.offset_y = cy - pdf_y * new_scale
        self._schedule_rerender()
        self.queue_draw()

    def _on_pinch_end(self, _gesture, _seq):
        self._pinch_start_scale = None
        self._pinch_anchor_pdf = None
        self._ignoring = False
        # a finger may still be on the screen (the user lifted one before the
        # other) — its still-live drag should pan, not draw, until it lifts too
        self._post_pinch = True
        self._post_pinch_anchor = None

    def _handle_boundary_flip(self, dx, dy, threshold):
        """Scrolling further while the page edge is already visible flips the
        page (after a resistance ``threshold``). Returns True when the
        scroll was consumed (accumulating or flipping) instead of panning."""
        if self.page is None or not dy or abs(dy) < abs(dx):
            return False
        ch = self.get_height() or 600
        page_top = self.offset_y
        page_bottom = self.offset_y + self.page_height * self.scale
        at_bottom = dy > 0 and page_bottom <= ch + 1
        at_top = dy < 0 and page_top >= -1
        if not (at_bottom or at_top):
            return False
        if at_bottom and self.current_page_idx >= self.n_pages - 1:
            return False
        if at_top and self.current_page_idx <= 0:
            return False
        if self._scroll_past and (self._scroll_past > 0) != (dy > 0):
            self._scroll_past = 0.0   # direction reversed — restart resistance
        self._scroll_past += dy
        if self._scroll_past >= threshold:
            self._flip_page(1)
        elif self._scroll_past <= -threshold:
            self._flip_page(-1)
        return True

    def _flip_page(self, delta):
        if self._is_fitted:
            self.go_to_page(self.current_page_idx + delta)   # refit on the new page
            return
        # zoomed: keep zoom and horizontal position, align the new page so
        # reading continues at its top (or bottom when flipping backwards)
        ch = self.get_height() or 600
        self.go_to_page(self.current_page_idx + delta, keep_view=True)
        page_h = self.page_height * self.scale
        if page_h <= ch:
            # short page: centre it — there's nothing to read past an edge, and
            # this keeps wide/landscape decks from jumping to the top/bottom
            self.offset_y = (ch - page_h) / 2
        elif delta > 0:
            self.offset_y = 8.0
        else:
            self.offset_y = ch - page_h - 8.0
        self._schedule_rerender()
        self.queue_draw()

    def _anchor_hit_test(self, sx, sy):
        """Return index of anchor circle under screen point, or None."""
        anchors = self._anchors.get(self.current_page_idx, [])
        for i, a in enumerate(anchors):
            scx, scy = self._pdf_to_screen(a["x"], a["y"])
            if math.hypot(sx - scx, sy - scy) <= 10.0:
                return i
        return None

    def _callout_hit_test(self, sx, sy):
        """Return the anchor index whose callout box is under the screen point,
        or None. Uses the rects recorded during the last draw; topmost wins."""
        for idx, bx, by, bw, bh in reversed(self._callout_boxes):
            if bx <= sx <= bx + bw and by <= sy <= by + bh:
                return idx
        return None

    def _textbox_hit_test(self, sx, sy):
        """Return the index of the standalone text box under the screen point,
        or None (topmost wins)."""
        for idx, bx, by, bw, bh in reversed(self._textbox_boxes):
            if bx <= sx <= bx + bw and by <= sy <= by + bh:
                return idx
        return None

    def _on_motion_leave(self, _ctrl):
        if self._hovered_link_rect is not None:
            self._hovered_link_rect = None
            self.set_cursor(None)
            self.queue_draw()

    def _on_modifier_key(self, _ctrl, keyval, _keycode, _state, pressed):
        if keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R):
            self._alt_held = pressed
        elif keyval in (Gdk.KEY_Control_L, Gdk.KEY_Control_R):
            self._ctrl_held = pressed
        elif keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R):
            self._shift_held = pressed
        else:
            return
        self._update_link_hover()
        if self.on_modifier_tool:
            self.on_modifier_tool(self._modifier_tool())

    def _modifier_tool(self):
        """Which tool the held modifiers stand in for, mirroring the gesture
        routing in _on_drag_begin — or None when nothing relevant is held."""
        if self._ctrl_held and self._shift_held and self._alt_held:
            return "lasso"
        if self._ctrl_held and self._alt_held:
            return "anchor"
        if self._ctrl_held and self._shift_held:
            return "highlighter"
        if self._ctrl_held:
            return "pan"
        if self._alt_held:
            return "select"
        if self._shift_held:
            return "zoom"
        return None

    def _default_cursor(self):
        """Cursor that matches the active tool while idle (no drag in flight)."""
        name = {"select": "text", "pan": "grab", "lasso": "crosshair",
                "zoom": "crosshair", "anchor": "crosshair"}.get(self.tool)
        return Gdk.Cursor.new_from_name(name, None) if name else None

    def _update_link_hover(self):
        new_rect = None
        if self._alt_held and self.page:
            px, py = self._screen_to_pdf(self._hover_x, self._hover_y)
            for link in self.page.get_links():
                r = link["from"]
                if r.x0 <= px <= r.x1 and r.y0 <= py <= r.y1:
                    new_rect = r
                    break
        if new_rect == self._hovered_link_rect:
            return
        self._hovered_link_rect = new_rect
        self.set_cursor(Gdk.Cursor.new_from_name("pointer", None) if new_rect else None)
        self.queue_draw()

    def _on_click_pressed(self, gesture, n_press, x, y):
        if self.on_canvas_press:
            self.on_canvas_press()
        state = gesture.get_current_event_state()
        # Ctrl+Alt (but not when Shift is also held — that's the lasso gesture)
        ctrl_alt = ((state & Gdk.ModifierType.CONTROL_MASK)
                    and (state & Gdk.ModifierType.ALT_MASK)
                    and not (state & Gdk.ModifierType.SHIFT_MASK))
        any_mod = state & (Gdk.ModifierType.CONTROL_MASK
                           | Gdk.ModifierType.ALT_MASK
                           | Gdk.ModifierType.SHIFT_MASK)
        # Ctrl+Alt, or the anchor tool with no modifier, drops an anchor here.
        if ctrl_alt or (self.tool == "anchor" and not any_mod):
            if self.page is None:
                return
            px, py = self._screen_to_pdf(x, y)
            if self.on_anchor_placed:
                self.on_anchor_placed(self.current_page_idx, round(px), round(py))

    def _on_drag_begin(self, gesture, start_x, start_y):
        self._post_pinch = False   # a fresh press starts a normal interaction
        self._text_highlighting = False
        if gesture.get_current_button() == 3:
            state = gesture.get_current_event_state()
            if ((state & Gdk.ModifierType.CONTROL_MASK)
                    and (state & Gdk.ModifierType.ALT_MASK)
                    and not (state & Gdk.ModifierType.SHIFT_MASK)):
                # Ctrl+Alt+right-click drops a standalone text box here (#56)
                self._ignoring = True
                if self.page is not None and self.on_textbox_placed:
                    px, py = self._screen_to_pdf(start_x, start_y)
                    self.on_textbox_placed(self.current_page_idx, round(px), round(py))
                return
            self._erasing = True
            self._erase_group += 1
            self._panning = False
            self._text_selecting = False
            self._zoom_selecting = False
            self._selected_words = []
            self._erase_at(start_x, start_y)
            return
        btn = gesture.get_current_button()
        logger.debug(f"drag begin btn={btn}")
        if btn in (8, 9):
            # Page navigation by the mouse side buttons is handled window-wide
            # (a capture-phase legacy controller), so it also works when the
            # notes editor has focus; here we just make sure the drag gesture
            # doesn't turn the press into a stroke.
            self._ignoring = True
            return
        if btn == 10:
            # thumb-button pan is driven by _on_motion while held; ignore the
            # drag gesture so it doesn't draw or pan on top of it
            self._ignoring = True
            return
        self._ignoring = False
        self._erasing = False
        self._temp_highlighter = False
        if btn == 2:
            # middle-mouse drag pans, same as Ctrl+drag
            self._panning = True
            self._is_fitted = False
            self._pan_start_offset = (self.offset_x, self.offset_y)
            self._text_selecting = False
            self._zoom_selecting = False
            self._selected_words = []
            return
        state = gesture.get_current_event_state()
        if ((state & Gdk.ModifierType.CONTROL_MASK)
                and (state & Gdk.ModifierType.ALT_MASK)
                and (state & Gdk.ModifierType.SHIFT_MASK)):
            # Ctrl+Shift+Alt+drag: lasso-select ink, regardless of the sticky tool
            # (mirrors the lasso tool as a discoverable modifier gesture)
            self._panning = False
            self._text_selecting = False
            self._zoom_selecting = False
            self._selected_words = []
            self._lassoing = True
            self._set_selected_strokes([])
            self._lasso_path = [(start_x, start_y)]
            return
        if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.ALT_MASK):
            # anchor already placed at press by GestureClick; dragging on
            # places a callout box at the release point
            self._callout_dragging = True
            self._callout_start = (start_x, start_y)
            self._callout_cur = None
            return
        if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.SHIFT_MASK):
            # Ctrl+Shift+drag: a one-off highlighter stroke regardless of the
            # sticky tool (mirrors the Ctrl+H highlighter toggle as a gesture)
            self._temp_highlighter = True
            self._panning = False
            self._text_selecting = False
            self._zoom_selecting = False
            self._selected_words = []
            self._cancel_straight_timer()
            self._straight_mode = False
            self.current_stroke = [self._screen_to_pdf(start_x, start_y)]
            return
        if state & Gdk.ModifierType.CONTROL_MASK:
            self._panning = True
            self._is_fitted = False
            self._pan_start_offset = (self.offset_x, self.offset_y)
            self._text_selecting = False
            self._zoom_selecting = False
            self._selected_words = []
        elif state & Gdk.ModifierType.ALT_MASK:
            self._text_selecting = True
            self._alt_start = (start_x, start_y)
            self._selected_words = []
            self._panning = False
            self._zoom_selecting = False
            self.grab_focus()
        elif state & Gdk.ModifierType.SHIFT_MASK:
            self._zoom_selecting = True
            self._zoom_start = (start_x, start_y)
            self._zoom_end = (start_x, start_y)
            self._text_selecting = False
            self._panning = False
            self._selected_words = []
        else:
            self._zoom_selecting = False
            self._text_selecting = False
            self._panning = False
            self._selected_words = []
            # the active tool is the modifier-free shortcut for a gesture: pan
            # mirrors Ctrl, zoom mirrors Shift, anchor mirrors Ctrl+Alt (the
            # anchor itself is dropped at press by _on_click_pressed).
            if self.tool == "pan":
                self._panning = True
                self._is_fitted = False
                self._pan_start_offset = (self.offset_x, self.offset_y)
                return
            if self.tool == "zoom":
                self._zoom_selecting = True
                self._zoom_start = (start_x, start_y)
                self._zoom_end = (start_x, start_y)
                return
            if self.tool == "anchor":
                self._callout_dragging = True
                self._callout_start = (start_x, start_y)
                self._callout_cur = None
                return
            if self.tool == "eraser":
                # left-drag erases, same as the always-on right-drag gesture
                self._erasing = True
                self._erase_group += 1
                self._erase_at(start_x, start_y)
                return
            if self.tool == "lasso":
                px, py = self._screen_to_pdf(start_x, start_y)
                if self._selected_strokes and self._point_in_selection(px, py):
                    # press inside the current selection grabs it for a move
                    self._lasso_moving = True
                    self._lasso_move_start = (start_x, start_y)
                    self._lasso_move_orig = [list(s["pts"])
                                            for s in self._selected_strokes]
                    self._lasso_moved = False
                    self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))
                else:
                    # otherwise start a fresh loop, dropping any prior selection
                    self._lassoing = True
                    self._set_selected_strokes([])
                    self._lasso_path = [(start_x, start_y)]
                return
            hit = self._anchor_hit_test(start_x, start_y)
            if hit is not None:
                # begin dragging the anchor; a release with no real movement is
                # treated as a click that jumps the notes cursor (see drag-end)
                self._anchor_dragging = hit
                self._anchor_drag_moved = False
                self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))
                return
            chit = self._callout_hit_test(start_x, start_y)
            if chit is not None:
                # begin dragging a callout box; keep the grab point fixed within
                # the box so it doesn't jump to the cursor
                self._callout_moving = chit
                self._callout_move_moved = False
                cpx, cpy = self._anchors[self.current_page_idx][chit]["callout"]
                px, py = self._screen_to_pdf(start_x, start_y)
                self._callout_move_offset = (cpx - px, cpy - py)
                self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))
                return
            thit = self._textbox_hit_test(start_x, start_y)
            if thit is not None:
                # begin dragging a standalone text box (same feel as callouts)
                self._textbox_moving = thit
                self._textbox_move_moved = False
                t = self._textboxes[self.current_page_idx][thit]
                px, py = self._screen_to_pdf(start_x, start_y)
                self._textbox_move_offset = (t["x"] - px, t["y"] - py)
                self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))
                return
            if self.select_mode:
                # plain drag selects text instead of drawing
                self._text_selecting = True
                self._alt_start = (start_x, start_y)
                self.grab_focus()
                return
            if self.highlighter and self.highlight_style == "text":
                # highlighter "text" style: drag selects words (reading order)
                # and commits highlight ink over them on release
                self._text_selecting = True
                self._text_highlighting = True
                self._alt_start = (start_x, start_y)
                self._selected_words = []
                self.grab_focus()
                return
            self._cancel_straight_timer()
            self._straight_mode = False
            self.current_stroke = [self._screen_to_pdf(start_x, start_y)]

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if self._post_pinch:
            # the finger left over from a pinch pans the page (never draws);
            # latch the offset at hand-off so the page doesn't jump
            if self._post_pinch_anchor is None:
                self._post_pinch_anchor = (offset_x, offset_y)
                self._post_pinch_base = (self.offset_x, self.offset_y)
                self._is_fitted = False
            ax, ay = self._post_pinch_anchor
            self.offset_x = self._post_pinch_base[0] + (offset_x - ax)
            self.offset_y = self._post_pinch_base[1] + (offset_y - ay)
            self.queue_draw()
            return
        if self._ignoring:
            return
        logger.debug(f"drag update offset=({offset_x:.0f},{offset_y:.0f})")
        sx, sy = gesture.get_start_point()[1], gesture.get_start_point()[2]
        if self._anchor_dragging is not None:
            if math.hypot(offset_x, offset_y) >= 4:
                self._anchor_drag_moved = True
            anchors = self._anchors.get(self.current_page_idx, [])
            if 0 <= self._anchor_dragging < len(anchors):
                px, py = self._screen_to_pdf(sx + offset_x, sy + offset_y)
                anchors[self._anchor_dragging]["x"] = round(px)
                anchors[self._anchor_dragging]["y"] = round(py)
                self.queue_draw()
            return
        if self._callout_moving is not None:
            if math.hypot(offset_x, offset_y) >= 4:
                self._callout_move_moved = True
            anchors = self._anchors.get(self.current_page_idx, [])
            if 0 <= self._callout_moving < len(anchors):
                px, py = self._screen_to_pdf(sx + offset_x, sy + offset_y)
                ox, oy = self._callout_move_offset
                anchors[self._callout_moving]["callout"] = (round(px + ox), round(py + oy))
                self.queue_draw()
            return
        if self._textbox_moving is not None:
            if math.hypot(offset_x, offset_y) >= 4:
                self._textbox_move_moved = True
            boxes = self._textboxes.get(self.current_page_idx, [])
            if 0 <= self._textbox_moving < len(boxes):
                px, py = self._screen_to_pdf(sx + offset_x, sy + offset_y)
                ox, oy = self._textbox_move_offset
                boxes[self._textbox_moving]["x"] = round(px + ox)
                boxes[self._textbox_moving]["y"] = round(py + oy)
                self.queue_draw()
            return
        if self._callout_dragging:
            self._callout_cur = (sx + offset_x, sy + offset_y)
            self.queue_draw()
            return
        if self._erasing:
            self._erase_at(sx + offset_x, sy + offset_y)
            return
        if self._lasso_moving:
            if math.hypot(offset_x, offset_y) >= 3:
                self._lasso_moved = True
            dx, dy = offset_x / self.scale, offset_y / self.scale
            for s, orig in zip(self._selected_strokes, self._lasso_move_orig):
                s["pts"] = [(x + dx, y + dy) for x, y in orig]
            self.queue_draw()
            return
        if self._lassoing:
            self._lasso_path.append((sx + offset_x, sy + offset_y))
            self.queue_draw()
            return
        if self._panning:
            self.offset_x = self._pan_start_offset[0] + offset_x
            self.offset_y = self._pan_start_offset[1] + offset_y
            self.queue_draw()
            return
        if self._text_selecting:
            px0, py0 = self._screen_to_pdf(sx, sy)
            px1, py1 = self._screen_to_pdf(sx + offset_x, sy + offset_y)
            if self.select_style == "rect" and not self._text_highlighting:
                self._selected_words = self._words_in_rect(px0, py0, px1, py1)
            else:
                # text-highlight always follows reading order, regardless of the
                # select tool's rectangular/reading-order preference
                self._selected_words = self._words_in_reading_range(px0, py0, px1, py1)
            self.queue_draw()
            return
        if self._zoom_selecting:
            self._zoom_end = self._constrain_zoom_end(sx, sy, sx + offset_x, sy + offset_y)
        else:
            pt = self._screen_to_pdf(sx + offset_x, sy + offset_y)
            if self._straight_mode:
                # locked to a line: only the endpoint follows the cursor
                self.current_stroke = [self.current_stroke[0], pt]
            else:
                self.current_stroke.append(pt)
                # re-arm on every motion → the snap fires once the cursor rests
                self._arm_straight_timer()
            if self.on_live_draw:
                self.on_live_draw()   # mirror the in-progress ink live
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        logger.debug(f"drag end offset=({offset_x:.0f},{offset_y:.0f})")
        self._cancel_straight_timer()
        was_straight = self._straight_mode
        self._straight_mode = False
        if self._post_pinch:
            self._post_pinch = False
            self._post_pinch_anchor = None
            self._schedule_rerender()
            self.queue_draw()
            return
        if self._anchor_dragging is not None:
            idx = self._anchor_dragging
            self._anchor_dragging = None
            self.set_cursor(None)
            self._hovering_anchor = False
            anchors = self._anchors.get(self.current_page_idx, [])
            if self._anchor_drag_moved and 0 <= idx < len(anchors):
                a = anchors[idx]
                if self.on_anchor_moved:
                    self.on_anchor_moved(idx, a["x"], a["y"])
            elif not self._anchor_drag_moved and self.on_anchor_clicked:
                self.on_anchor_clicked(idx)
            self.queue_draw()
            return
        if self._callout_moving is not None:
            idx = self._callout_moving
            self._callout_moving = None
            self.set_cursor(None)
            self._hovering_anchor = False
            anchors = self._anchors.get(self.current_page_idx, [])
            if self._callout_move_moved and 0 <= idx < len(anchors):
                cx, cy = anchors[idx]["callout"]
                if self.on_callout_moved:
                    self.on_callout_moved(idx, cx, cy)
            self.queue_draw()
            return
        if self._textbox_moving is not None:
            idx = self._textbox_moving
            self._textbox_moving = None
            self.set_cursor(None)
            self._hovering_anchor = False
            boxes = self._textboxes.get(self.current_page_idx, [])
            if self._textbox_move_moved and 0 <= idx < len(boxes):
                if self.on_textbox_moved:
                    self.on_textbox_moved(idx, boxes[idx]["x"], boxes[idx]["y"])
            self.queue_draw()
            return
        if self._ignoring:
            self._ignoring = False
            self.queue_draw()
            return
        if self._callout_dragging:
            self._callout_dragging = False
            sx, sy = self._callout_start
            self._callout_start = None
            self._callout_cur = None
            if math.hypot(offset_x, offset_y) >= 12 and self.on_callout_placed:
                px, py = self._screen_to_pdf(sx + offset_x, sy + offset_y)
                self.on_callout_placed(round(px), round(py))
            self.queue_draw()
            return
        if self._erasing:
            self._erasing = False
            # one timeline entry per erase gesture that actually removed something
            if (self._undo_stack and self._undo_stack[-1][0] == "erase"
                    and self._undo_stack[-1][4] == self._erase_group
                    and self.on_user_action):
                self.on_user_action()
            return
        if self._lasso_moving:
            self._lasso_moving = False
            self.set_cursor(self._default_cursor())
            if self._lasso_moved and self._selected_strokes:
                dx, dy = offset_x / self.scale, offset_y / self.scale
                self._undo_stack.append(("lasso_move", self.current_page_idx,
                                         list(self._selected_strokes), dx, dy))
                self._redo_stack.clear()
                if self.on_change:
                    self.on_change()
                if self.on_user_action:
                    self.on_user_action()
            self.queue_draw()
            return
        if self._lassoing:
            self._lassoing = False
            self._finish_lasso()
            self.queue_draw()
            return
        if self._panning:
            self._panning = False
            return
        if self._text_selecting:
            self._text_selecting = False
            if self._text_highlighting:
                self._text_highlighting = False
                self._commit_text_highlight()
            elif abs(offset_x) < 8 and abs(offset_y) < 8:
                sx, sy = self._alt_start
                self._open_link_at(sx, sy)
            else:
                self._finish_text_selection()
            return
        if self._zoom_selecting:
            if self._zoom_start and self._zoom_end:
                dx = abs(self._zoom_end[0] - self._zoom_start[0])
                dy = abs(self._zoom_end[1] - self._zoom_start[1])
                if dx >= 8 and dy >= 8:
                    self._execute_zoom_to_rect(self._zoom_start, self._zoom_end)
                else:
                    self.zoom_to_fit()   # Shift+click with no rect → fit page
            self._zoom_selecting = False
            self._zoom_start = None
            self._zoom_end = None
        else:
            if self.current_stroke:
                pts = self.current_stroke
                # smooth freehand ink on commit; a snapped straight line and
                # tiny strokes (dots) are left exactly as drawn
                if not was_straight and len(pts) > 2:
                    pts = self._smooth_points(pts, self.smoothing)
                color, width, opacity = self._pen_attrs()
                stroke = {
                    "pts": pts,
                    "color": color,
                    "width": width,
                    "opacity": opacity,
                }
                self.strokes.append(stroke)
                self._undo_stack.append(("draw", self.current_page_idx, stroke))
                self._redo_stack.clear()
                if self.on_change:
                    self.on_change()
                if self.on_user_action:
                    self.on_user_action()
            self.current_stroke = []
            if self.on_live_draw:
                self.on_live_draw()   # drop the live stroke from the mirror
        self._temp_highlighter = False
        self.queue_draw()

    def _arm_straight_timer(self):
        self._cancel_straight_timer()
        self._straight_timer = GLib.timeout_add(
            self.STRAIGHT_HOLD_MS, self._snap_to_straight)

    def _cancel_straight_timer(self):
        if self._straight_timer is not None:
            GLib.source_remove(self._straight_timer)
            self._straight_timer = None

    @staticmethod
    def _smooth_points(pts, strength, passes=4):
        """Clean up a freehand polyline with Laplacian (moving-average)
        smoothing. ``strength`` 0..1 scales how far each interior point is
        pulled toward the midpoint of its neighbours; endpoints stay fixed so
        the stroke keeps its start and end. Returns a new list of (x, y)."""
        if strength <= 0 or len(pts) < 3:
            return list(pts)
        factor = 0.5 * min(strength, 1.0)
        cur = [(float(x), float(y)) for x, y in pts]
        for _ in range(passes):
            nxt = [cur[0]]
            for i in range(1, len(cur) - 1):
                x = cur[i][0] + factor * (cur[i - 1][0] + cur[i + 1][0] - 2 * cur[i][0])
                y = cur[i][1] + factor * (cur[i - 1][1] + cur[i + 1][1] - 2 * cur[i][1])
                nxt.append((x, y))
            nxt.append(cur[-1])
            cur = nxt
        return cur

    def _snap_to_straight(self):
        """Fired when the cursor has rested mid-stroke: collapse the in-progress
        free stroke into a straight line from its start to the current point."""
        self._straight_timer = None
        if len(self.current_stroke) >= 2:
            self._straight_mode = True
            self.current_stroke = [self.current_stroke[0], self.current_stroke[-1]]
            self.queue_draw()
            if self.on_live_draw:
                self.on_live_draw()
        return False   # one-shot

    def _words_in_rect(self, px0, py0, px1, py1):
        """Return fitz word tuples whose bounding boxes overlap the given PDF rect."""
        rx0, rx1 = min(px0, px1), max(px0, px1)
        ry0, ry1 = min(py0, py1), max(py0, py1)
        return [w for w in self._page_words
                if w[0] < rx1 and w[2] > rx0 and w[1] < ry1 and w[3] > ry0]

    @staticmethod
    def _word_point_dist2(w, px, py):
        """Squared distance from point to a word's bounding box (0 if inside)."""
        dx = max(w[0] - px, 0.0, px - w[2])
        dy = max(w[1] - py, 0.0, py - w[3])
        return dx * dx + dy * dy

    def _nearest_word_index(self, px, py):
        """Index into self._ordered_words of the word nearest the given point."""
        best_i, best_d = 0, float("inf")
        for i, w in enumerate(self._ordered_words):
            d = self._word_point_dist2(w, px, py)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    def _words_in_reading_range(self, px0, py0, px1, py1):
        """Contiguous reading-order run between the words nearest press & release."""
        if not self._ordered_words:
            return []
        i = self._nearest_word_index(px0, py0)
        j = self._nearest_word_index(px1, py1)
        lo, hi = min(i, j), max(i, j)
        return self._ordered_words[lo:hi + 1]

    def _commit_text_highlight(self):
        """Turn the selected words into highlighter ink: one wide stroke per text
        line covering its word boxes. Stored as ink, so save / eraser / undo all
        work unchanged; the per-line strokes share a draw group so a single undo
        removes the whole highlight."""
        words = self._selected_words
        self._selected_words = []
        if not words:
            self.queue_draw()
            return
        color = self.hl_color
        opacity = self.hl_opacity
        ordered = sorted(words, key=lambda w: (w[5], w[6], w[7]))
        self._draw_group += 1
        group = self._draw_group
        committed = False
        line_key = None
        line = []
        runs = []
        for w in ordered:
            key = (w[5], w[6])
            if key != line_key and line:
                runs.append(line)
                line = []
            line_key = key
            line.append(w)
        if line:
            runs.append(line)
        for run in runs:
            x0 = min(w[0] for w in run)
            x1 = max(w[2] for w in run)
            y0 = min(w[1] for w in run)
            y1 = max(w[3] for w in run)
            ymid = 0.5 * (y0 + y1)
            stroke = {
                "pts": [(x0, ymid), (x1, ymid)],
                "color": color,
                "width": max(y1 - y0, 1.0),   # span the line height
                "opacity": opacity,
            }
            self.strokes.append(stroke)
            self._undo_stack.append(("draw", self.current_page_idx, stroke, group))
            committed = True
        if committed:
            self._redo_stack.clear()
            if self.on_change:
                self.on_change()
            if self.on_user_action:
                self.on_user_action()
        self.queue_draw()

    def _finish_text_selection(self):
        self.queue_draw()   # highlight stays; copy on Ctrl+C

    def copy_selection(self):
        text = self._words_to_text(self._selected_words)
        self._selected_words = []
        self.queue_draw()
        if text:
            content = Gdk.ContentProvider.new_for_bytes(
                "text/plain;charset=utf-8",
                GLib.Bytes.new(text.encode("utf-8")))
            Gdk.Display.get_default().get_clipboard().set_content(content)
        if self.on_text_copied:
            self.on_text_copied(text)

    def _open_link_at(self, sx, sy):
        if not self.page:
            logger.debug("link click: no page loaded")
            return
        px, py = self._screen_to_pdf(sx, sy)
        links = self.page.get_links()
        logger.debug(
            f"link click: screen=({sx:.0f},{sy:.0f}) pdf=({px:.1f},{py:.1f}) "
            f"page={self.current_page_idx} scale={self.scale:.3f} "
            f"offset=({self.offset_x:.0f},{self.offset_y:.0f}) "
            f"{len(links)} link(s) on page")
        for link in links:
            r = link["from"]
            hit = r.x0 <= px <= r.x1 and r.y0 <= py <= r.y1
            logger.debug(
                f"  link kind={link.get('kind')} from=({r.x0:.0f},{r.y0:.0f},"
                f"{r.x1:.0f},{r.y1:.0f}) page={link.get('page')} "
                f"to={link.get('to')} uri={link.get('uri')!r} hit={hit}")
            if hit:
                kind = link.get("kind", 0)
                if kind == fitz.LINK_URI:
                    uri = link.get("uri", "")
                    if uri:
                        logger.debug(f"link: launching uri {uri!r}")
                        try:
                            Gio.AppInfo.launch_default_for_uri(uri, None)
                        except Exception:
                            logger.debug("link: launch_default_for_uri failed",
                                         exc_info=True)
                elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                    # LINK_NAMED is what LaTeX/hyperref emits for \cite, \ref and
                    # cross-references; PyMuPDF resolves the named destination into
                    # the same page/to fields as a plain GOTO.
                    page_no = link.get("page", -1)
                    to = link.get("to")
                    to_y = to.y if to is not None else None
                    kname = "NAMED" if kind == fitz.LINK_NAMED else "GOTO"
                    logger.debug(f"link: {kname} page={page_no} to_y={to_y}")
                    if page_no < 0 and kind == fitz.LINK_NAMED:
                        page_no, to_y = self._resolve_named_dest(
                            link.get("name") or link.get("nameddest"))
                        logger.debug(
                            f"link: NAMED resolved to page={page_no} to_y={to_y}")
                    if page_no >= 0:
                        self.follow_goto(page_no, to_y)
                    else:
                        logger.debug(f"link: {kname} ignored (page < 0)")
                else:
                    logger.debug(f"link: unhandled kind={kind}")
                break
        else:
            logger.debug("link click: no link under cursor")

    def _resolve_named_dest(self, name):
        """Best-effort resolution of an unresolved named destination to
        (page_index, y) — for the rare LaTeX/hyperref link PyMuPDF leaves with
        page=-1. Returns (-1, None) if it can't be resolved."""
        if not name or not self.document:
            return -1, None
        try:
            dests = self.document.resolve_names()
            d = dests.get(name)
            if d:
                page_no = d.get("page", -1)
                to = d.get("to")
                to_y = to[1] if to is not None else None
                return page_no, to_y
        except Exception:
            logger.debug("resolve_names failed", exc_info=True)
        return -1, None

    def follow_goto(self, page_no, to_y=None):
        """Jump to an internal link destination (footnote, citation, TOC entry),
        scrolling to the target point — not just the page top — and remembering
        the current reading location so nav_back() can return to it."""
        page_no = max(0, min(self.n_pages - 1, page_no))
        logger.debug(
            f"follow_goto: page {self.current_page_idx} -> {page_no} to_y={to_y}")
        self._push_nav_history()
        if page_no != self.current_page_idx:
            if self.on_page_will_change:
                self.on_page_will_change()
            self._load_page(page_no, keep_view=True)
        if to_y is not None:
            self._scroll_to_pdf_y(to_y)
        logger.debug(
            f"follow_goto: now page={self.current_page_idx} "
            f"offset=({self.offset_x:.0f},{self.offset_y:.0f}) scale={self.scale:.3f}")
        self._is_fitted = False
        self.queue_draw()

    def _scroll_to_pdf_y(self, py, top_frac=0.18):
        """Position PDF y-coordinate ``py`` ``top_frac`` of the way down the
        viewport, keeping the current zoom. Won't reveal empty space above the
        page top (footnote destinations sit low, so this usually scrolls down)."""
        h = self.get_height() or 600
        self.offset_y = min(8.0, h * top_frac - py * self.scale)

    def _push_nav_history(self):
        self._nav_history.append(
            (self.current_page_idx, self.offset_x, self.offset_y, self.scale))
        if self.on_nav_history:
            self.on_nav_history(True)

    def can_nav_back(self):
        return bool(self._nav_history)

    def nav_back(self):
        """Return to the reading location saved before the last followed link."""
        if not self._nav_history:
            return False
        page, ox, oy, scale = self._nav_history.pop()
        if page != self.current_page_idx:
            if self.on_page_will_change:
                self.on_page_will_change()
            self._load_page(page, keep_view=True)
        self.offset_x, self.offset_y, self.scale = ox, oy, scale
        self._is_fitted = False
        self.queue_draw()
        if self.on_nav_history:
            self.on_nav_history(bool(self._nav_history))
        return True

    @staticmethod
    def _words_to_text(words):
        """Join fitz word tuples in reading order, preserving line/paragraph breaks."""
        if not words:
            return ""
        # fitz words: (x0,y0,x1,y1, word, block_no, line_no, word_no)
        ordered = sorted(words, key=lambda w: (w[5], w[6], w[7]))
        parts = []
        prev_block = prev_line = None
        for w in ordered:
            block, line = w[5], w[6]
            if prev_block is not None:
                if block != prev_block:
                    parts.append("\n\n")
                elif line != prev_line:
                    parts.append("\n")
                else:
                    parts.append(" ")
            parts.append(w[4])
            prev_block, prev_line = block, line
        return "".join(parts)


    def _erase_at(self, sx, sy):
        px, py = self._screen_to_pdf(sx, sy)
        page = self.current_page_idx
        logger.debug(f"erase at pdf=({px:.1f},{py:.1f}) strokes={len(self.strokes)}")
        kept = []
        removed = 0
        for i, s in enumerate(self.strokes):
            if self._stroke_hits(s["pts"], px, py, s["width"] / 2 + 3.0):
                # record the index as if strokes were removed one at a time,
                # so undo can reinsert by popping ops in reverse order
                self._undo_stack.append(("erase", page, i - removed, s, self._erase_group))
                removed += 1
            else:
                kept.append(s)
        if removed:
            self.all_strokes[page] = kept
            self._redo_stack.clear()
            if self.on_change:
                self.on_change()
            self.queue_draw()

    @staticmethod
    def _stroke_hits(pts, px, py, radius):
        if not pts:
            return False
        if len(pts) == 1:
            return math.hypot(px - pts[0][0], py - pts[0][1]) <= radius
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            dx, dy = x2 - x1, y2 - y1
            if dx == 0 and dy == 0:
                d = math.hypot(px - x1, py - y1)
            else:
                t = max(0.0, min(1.0, ((px - x1)*dx + (py - y1)*dy) / (dx*dx + dy*dy)))
                d = math.hypot(px - x1 - t*dx, py - y1 - t*dy)
            if d <= radius:
                return True
        return False

    # ── lasso stroke selection ──────────────────────────────────────────────
    @staticmethod
    def _point_in_polygon(px, py, poly):
        """Even-odd ray-cast test: is (px, py) inside the polygon `poly`?"""
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py) != (yj > py)) and \
               (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _finish_lasso(self):
        """Close the in-progress loop and select every stroke on the page with at
        least one point inside it (forgiving 'any point inside' rule)."""
        path = self._lasso_path
        self._lasso_path = []
        if len(path) < 3:
            self._set_selected_strokes([])
            return
        poly = [self._screen_to_pdf(x, y) for x, y in path]
        sel = [s for s in self.strokes
               if any(self._point_in_polygon(px, py, poly) for px, py in s["pts"])]
        self._set_selected_strokes(sel)

    def _selection_bbox(self):
        """PDF-space (x0, y0, x1, y1) bounding box of the selected strokes, or None."""
        pts = [p for s in self._selected_strokes for p in s["pts"]]
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def _point_in_selection(self, px, py):
        """Is the PDF point inside the selection's (padded) bounding box?"""
        bbox = self._selection_bbox()
        if bbox is None:
            return False
        x0, y0, x1, y1 = bbox
        pad = 8.0 / self.scale
        return x0 - pad <= px <= x1 + pad and y0 - pad <= py <= y1 + pad

    def _set_selected_strokes(self, strokes):
        self._selected_strokes = strokes
        if self.on_lasso_selection:
            self.on_lasso_selection(bool(strokes))

    def has_lasso_selection(self):
        return bool(self._selected_strokes)

    def clear_lasso_selection(self):
        if self._selected_strokes or self._lasso_path:
            self._lasso_path = []
            self._set_selected_strokes([])
            self.queue_draw()

    def delete_selected_strokes(self):
        """Remove the lasso-selected strokes (one undo entry, reusing erase ops)."""
        if not self._selected_strokes:
            return
        page = self.current_page_idx
        sel = set(id(s) for s in self._selected_strokes)
        self._erase_group += 1
        kept = []
        removed = 0
        for i, s in enumerate(self.strokes):
            if id(s) in sel:
                self._undo_stack.append(("erase", page, i - removed, s, self._erase_group))
                removed += 1
            else:
                kept.append(s)
        if removed:
            self.all_strokes[page] = kept
            self._redo_stack.clear()
            self._set_selected_strokes([])
            if self.on_change:
                self.on_change()
            if self.on_user_action:
                self.on_user_action()
            self.queue_draw()

    def recolor_selected(self, color, width, opacity):
        """Apply the given pen attrs to the selected strokes (one undo entry)."""
        if not self._selected_strokes:
            return
        before = [(s, s["color"], s["width"], s.get("opacity", 1.0))
                  for s in self._selected_strokes]
        for s in self._selected_strokes:
            s["color"] = color
            s["width"] = width
            s["opacity"] = opacity
        self._undo_stack.append(("recolor", self.current_page_idx, before,
                                 color, width, opacity))
        self._redo_stack.clear()
        if self.on_change:
            self.on_change()
        if self.on_user_action:
            self.on_user_action()
        self.queue_draw()

    def _draw_lasso(self, ctx):
        """Overlay for the lasso tool: highlight selected strokes, the live loop
        being drawn, and a bounding box around the current selection."""
        ar, ag, ab = self.zoom_accent
        # retint selected strokes so they read as picked up
        if self._selected_strokes:
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            for s in self._selected_strokes:
                pts = s["pts"]
                # a translucent glow wider than the stroke, so its real colour
                # still shows through the centre
                ctx.set_source_rgba(ar, ag, ab, 0.35)
                ctx.set_line_width(s["width"] + 7.0 / self.scale)
                if len(pts) < 2:
                    if pts:
                        ctx.arc(pts[0][0], pts[0][1],
                                s["width"] / 2 + 3.5 / self.scale, 0, 2 * math.pi)
                        ctx.fill()
                    continue
                ctx.move_to(*pts[0])
                for pt in pts[1:]:
                    ctx.line_to(*pt)
                ctx.stroke()
            ctx.restore()
            # dashed bounding box (screen space for crisp 1px lines)
            bbox = self._selection_bbox()
            if bbox:
                x0, y0 = self._pdf_to_screen(bbox[0], bbox[1])
                x1, y1 = self._pdf_to_screen(bbox[2], bbox[3])
                pad = 5.0
                ctx.set_source_rgba(ar, ag, ab, 0.85)
                ctx.set_line_width(1.0)
                ctx.set_dash([4.0, 3.0])
                ctx.rectangle(x0 - pad, y0 - pad,
                              (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad)
                ctx.stroke()
                ctx.set_dash([])
        # the loop being drawn
        if self._lassoing and len(self._lasso_path) >= 2:
            ctx.set_source_rgba(ar, ag, ab, 0.9)
            ctx.set_line_width(1.5)
            ctx.set_dash([5.0, 3.0])
            ctx.move_to(*self._lasso_path[0])
            for pt in self._lasso_path[1:]:
                ctx.line_to(*pt)
            ctx.stroke()
            ctx.set_dash([])

    def _constrain_zoom_end(self, sx, sy, ex, ey):
        """Constrain (ex, ey) so the rect has the same aspect ratio as the canvas."""
        cw = self.get_width() or 800
        ch = self.get_height() or 600
        dx = ex - sx
        dy_constrained = abs(dx) * ch / cw
        return sx + dx, sy + (dy_constrained if ey >= sy else -dy_constrained)

    def _execute_zoom_to_rect(self, start, end):
        x1, y1 = min(start[0], end[0]), min(start[1], end[1])
        x2, y2 = max(start[0], end[0]), max(start[1], end[1])
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        # Convert to PDF coords
        px1, py1 = self._screen_to_pdf(x1, y1)
        px2, py2 = self._screen_to_pdf(x2, y2)
        pdf_w, pdf_h = px2 - px1, py2 - py1
        if pdf_w <= 0 or pdf_h <= 0:
            return
        cw = self.get_width() or 800
        ch = self.get_height() or 600
        self._zoom_stack.append((self.scale, self.offset_x, self.offset_y))
        new_scale = min(cw / pdf_w, ch / pdf_h) * 0.97
        self.scale = new_scale
        self._is_fitted = False
        self.offset_x = (cw - pdf_w * new_scale) / 2 - px1 * new_scale
        self.offset_y = (ch - pdf_h * new_scale) / 2 - py1 * new_scale
        self._schedule_rerender()

    def zoom_back(self):
        if self._zoom_stack:
            self.scale, self.offset_x, self.offset_y = self._zoom_stack.pop()
            self._is_fitted = False
            self._schedule_rerender()
            self.queue_draw()

    def zoom_to_fit(self):
        """Reset to fit-page view, clearing the entire zoom history."""
        self._zoom_stack.clear()
        self._fit_page()
        self._schedule_rerender()
        self.queue_draw()

    @staticmethod
    def _remove_stroke(strokes, stroke):
        for i, s in enumerate(strokes):
            if s is stroke:
                del strokes[i]
                return

    def undo_last(self):
        """Undo the last draw or erase operation (an erase drag counts as one)."""
        if not self._undo_stack:
            return
        op = self._undo_stack.pop()
        popped = [op]
        page = op[1]
        strokes = self.all_strokes.setdefault(page, [])
        if op[0] == "draw":
            self._remove_stroke(strokes, op[2])
            # a text-highlight is many per-line strokes sharing a draw group;
            # collapse them into one undo, like an erase gesture
            if len(op) > 3:
                group = op[3]
                while (self._undo_stack and self._undo_stack[-1][0] == "draw"
                       and len(self._undo_stack[-1]) > 3
                       and self._undo_stack[-1][3] == group):
                    op = self._undo_stack.pop()
                    popped.append(op)
                    self._remove_stroke(strokes, op[2])
        elif op[0] == "lasso_move":
            _, _, refs, dx, dy = op
            for s in refs:
                s["pts"] = [(x - dx, y - dy) for x, y in s["pts"]]
        elif op[0] == "recolor":
            for s, oc, ow, oo in op[2]:
                s["color"], s["width"], s["opacity"] = oc, ow, oo
        else:
            strokes.insert(min(op[2], len(strokes)), op[3])
            group = op[4]
            while (self._undo_stack and self._undo_stack[-1][0] == "erase"
                   and self._undo_stack[-1][4] == group):
                op = self._undo_stack.pop()
                popped.append(op)
                strokes.insert(min(op[2], len(strokes)), op[3])
        self.clear_lasso_selection()
        self._redo_stack.append(popped)
        if page != self.current_page_idx:
            self.go_to_page(page)   # show the user what was undone
        if self.on_change:
            self.on_change()
        self.queue_draw()

    def redo_last(self):
        """Re-apply the most recently undone draw or erase gesture."""
        if not self._redo_stack:
            return
        ops = self._redo_stack.pop()
        page = ops[0][1]
        strokes = self.all_strokes.setdefault(page, [])
        if ops[0][0] == "draw":
            # re-add in chronological order (reverse of the pop order)
            for op in reversed(ops):
                strokes.append(op[2])
        elif ops[0][0] == "lasso_move":
            _, _, refs, dx, dy = ops[0]
            for s in refs:
                s["pts"] = [(x + dx, y + dy) for x, y in s["pts"]]
        elif ops[0][0] == "recolor":
            _, _, before, nc, nw, no = ops[0]
            for s, _oc, _ow, _oo in before:
                s["color"], s["width"], s["opacity"] = nc, nw, no
        else:
            # re-remove in the gesture's chronological order (reverse of pop order)
            for op in reversed(ops):
                for i, s in enumerate(strokes):
                    if s is op[3]:
                        del strokes[i]
                        break
        self.clear_lasso_selection()
        self._undo_stack.extend(reversed(ops))
        if page != self.current_page_idx:
            self.go_to_page(page)
        if self.on_change:
            self.on_change()
        self.queue_draw()

    # ── save ──────────────────────────────────────────────────────────────────

    def _write_ink_annotations(self):
        """Sync in-memory strokes into the document as ink annotations."""
        total_written = 0
        for i in range(self.n_pages):
            page = self.document[i]
            for annot in list(page.annots(types=[fitz.PDF_ANNOT_INK])):
                page.delete_annot(annot)
            for stroke in self.all_strokes.get(i, []):
                pts = stroke["pts"]
                if not pts:
                    continue
                polyline = pts if len(pts) > 1 else [pts[0], pts[0]]
                r, g, b = stroke["color"]
                annot = page.add_ink_annot([polyline])
                annot.set_colors(stroke=(r, g, b))
                annot.set_border(width=stroke["width"])
                if stroke.get("opacity", 1.0) < 1.0:
                    annot.set_opacity(stroke["opacity"])
                annot.update()
                total_written += 1
        return total_written

    def save(self, path):
        """Save via self.document so structural changes (inserted pages) are preserved."""
        tmp = path + ".tmp"
        total_written = self._write_ink_annotations()
        logger.info(f"save: {path} — wrote {total_written} ink annotation(s)")
        self.document.save(tmp, garbage=4, deflate=True)
        os.replace(tmp, path)
        # Reopen so self.document reflects the saved state cleanly
        self.document = fitz.open(path)

    def save_copy(self, path):
        """Write the current state (including unsaved strokes and structural
        changes) to path without touching the original or rebinding the
        document — used for autosave snapshots."""
        self._write_ink_annotations()
        tmp = path + ".tmp"
        self.document.save(tmp)
        os.replace(tmp, path)

    def export_pages(self, indices, path):
        """Write the given page indices (with current ink strokes baked in) to a
        standalone PDF at path. Used by thumbnail drag-to-export."""
        self._write_ink_annotations()
        out = fitz.open()
        try:
            for i in sorted(set(indices)):
                if 0 <= i < self.n_pages:
                    out.insert_pdf(self.document, from_page=i, to_page=i)
            tmp = path + ".tmp"
            out.save(tmp, garbage=4, deflate=True)
            os.replace(tmp, path)
        finally:
            out.close()

    def add_blank_page(self):
        """Insert a blank page with the same dimensions as the current page, after it."""
        idx = self.current_page_idx + 1
        pw, ph = self.page_width, self.page_height
        self.document.insert_page(idx, width=pw, height=ph)
        # Shift all stroke and anchor entries at or beyond the insertion point up by one
        self.all_strokes = {
            (k + 1 if k >= idx else k): v
            for k, v in self.all_strokes.items()
        }
        self._anchors = {
            (k + 1 if k >= idx else k): v
            for k, v in self._anchors.items()
        }
        self._undo_stack = [
            (op[0], op[1] + 1 if op[1] >= idx else op[1]) + op[2:]
            for op in self._undo_stack
        ]
        self._redo_stack = [
            [(op[0], op[1] + 1 if op[1] >= idx else op[1]) + op[2:] for op in ops]
            for ops in self._redo_stack
        ]
        self.n_pages = len(self.document)
        self._load_page(idx)   # navigate to the new blank page

    def insert_pdf_pages(self, at_idx, src_path):
        """Insert every page of the PDF at src_path so the first lands at index
        at_idx, shifting strokes/anchors/undo for pages at or after it (mirrors
        add_blank_page). Navigates to the first inserted page. Returns the number
        of pages inserted (0 if the document is empty or unreadable)."""
        if not self.document:
            return 0
        src = fitz.open(src_path)
        try:
            count = len(src)
            if count == 0:
                return 0
            at_idx = max(0, min(at_idx, self.n_pages))
            self.document.insert_pdf(src, start_at=at_idx)
        finally:
            src.close()
        self.all_strokes = {
            (k + count if k >= at_idx else k): v
            for k, v in self.all_strokes.items()
        }
        self._anchors = {
            (k + count if k >= at_idx else k): v
            for k, v in self._anchors.items()
        }
        self._undo_stack = [
            (op[0], op[1] + count if op[1] >= at_idx else op[1]) + op[2:]
            for op in self._undo_stack
        ]
        self._redo_stack = [
            [(op[0], op[1] + count if op[1] >= at_idx else op[1]) + op[2:]
             for op in ops]
            for ops in self._redo_stack
        ]
        self.n_pages = len(self.document)
        self._load_page(at_idx)
        return count

    def delete_current_page(self):
        """Delete the current page. Refused if it's the last one."""
        if self.n_pages <= 1:
            return False
        idx = self.current_page_idx
        self.document.delete_page(idx)
        # Remove strokes/anchors for deleted page; shift later pages down by one
        self.all_strokes = {
            (k - 1 if k > idx else k): v
            for k, v in self.all_strokes.items()
            if k != idx
        }
        self._anchors = {
            (k - 1 if k > idx else k): v
            for k, v in self._anchors.items()
            if k != idx
        }
        self._undo_stack = [
            (op[0], op[1] - 1 if op[1] > idx else op[1]) + op[2:]
            for op in self._undo_stack
            if op[1] != idx
        ]
        self._redo_stack = [
            shifted for shifted in (
                [(op[0], op[1] - 1 if op[1] > idx else op[1]) + op[2:]
                 for op in ops if op[1] != idx]
                for ops in self._redo_stack
            ) if shifted
        ]
        self.n_pages = len(self.document)
        new_idx = min(idx, self.n_pages - 1)
        self._load_page(new_idx)
        return True

    @staticmethod
    def _move_order(n, src, dst):
        """Permutation (list of old indices in new order) for moving the page
        at src so it lands at index dst."""
        order = list(range(n))
        order.insert(dst, order.pop(src))
        return order

    def move_page(self, src, dst):
        """Move the page at index src to index dst, re-keying strokes/anchors
        and the undo/redo stacks. Returns the old→new index map (or None)."""
        if not self.document or src == dst:
            return None
        n = self.n_pages
        if not (0 <= src < n and 0 <= dst < n):
            return None
        order = self._move_order(n, src, dst)
        self.document.select(order)        # reorder underlying pages
        old_to_new = {old: new for new, old in enumerate(order)}
        self.all_strokes = {old_to_new[k]: v for k, v in self.all_strokes.items()}
        self._anchors = {old_to_new[k]: v for k, v in self._anchors.items()}
        self._undo_stack = [
            (op[0], old_to_new[op[1]]) + op[2:] for op in self._undo_stack]
        self._redo_stack = [
            [(op[0], old_to_new[op[1]]) + op[2:] for op in ops]
            for ops in self._redo_stack]
        self.n_pages = len(self.document)
        self._load_page(old_to_new[self.current_page_idx])
        return old_to_new


def _load_theme():
    """Read background/foreground/accent — tries Omarchy, then GNOME, then KDE."""
    defaults = {
        "background": "#fdf6ee", "foreground": "#22211d", "accent": "#85b34c",
        "color1": "#df2b0d", "color3": "#8a6c3e", "color6": "#3d6b52", "color8": "#a09080",
    }

    # ── Omarchy ───────────────────────────────────────────────────────────────
    omarchy = os.path.expanduser("~/.config/omarchy/current/theme/colors.toml")
    try:
        with open(omarchy) as f:
            for line in f:
                line = line.strip()
                if " = " in line and not line.startswith("#"):
                    k, v = line.split(" = ", 1)
                    k = k.strip()
                    if k in defaults:
                        defaults[k] = v.strip().strip('"')
        return defaults   # Omarchy wins outright
    except OSError:
        pass

    # ── GNOME (gsettings) ─────────────────────────────────────────────────────
    try:
        import subprocess
        def _gs(key):
            r = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", key],
                capture_output=True, text=True, timeout=1,
            )
            return r.stdout.strip().strip("'") if r.returncode == 0 else None

        _GNOME_ACCENTS = {
            "blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a",
            "yellow": "#c88800", "orange": "#ed5b00", "red": "#e62d42",
            "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396",
        }
        accent = _gs("accent-color")
        if accent in _GNOME_ACCENTS:
            defaults["accent"] = _GNOME_ACCENTS[accent]
        if _gs("color-scheme") == "prefer-dark":
            defaults["background"] = "#242424"
            defaults["foreground"] = "#e5e5e5"
    except Exception:
        pass

    # ── KDE Plasma (kdeglobals) ───────────────────────────────────────────────
    try:
        import configparser
        cfg = configparser.ConfigParser(strict=False)
        cfg.read(os.path.expanduser("~/.config/kdeglobals"))

        def _rgb(s):
            r, g, b = [int(x.strip()) for x in s.split(",")]
            return "#{:02x}{:02x}{:02x}".format(r, g, b)

        # Accent: Plasma 5.25+ puts it in [General], older in [Colors:Button]
        for sec, key in [("General", "AccentColor"), ("Colors:Button", "FocusDecoration")]:
            if cfg.has_option(sec, key):
                defaults["accent"] = _rgb(cfg[sec][key])
                break
        # Dark mode: colour scheme name contains "Dark"
        if cfg.has_option("General", "ColorScheme"):
            if "dark" in cfg["General"]["ColorScheme"].lower():
                defaults["background"] = "#1e1e2e"
                defaults["foreground"] = "#cdd6f4"
    except Exception:
        pass

    return defaults


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


def _themed_icon(*candidates):
    """First candidate icon the active theme actually has, else the first.

    Symbolic icon names are not portable: e.g. ``view-sidebar-symbolic`` and
    ``view-list-symbolic`` exist in Adwaita/GNOME but not in KDE's Breeze, so a
    hard-coded name shows a blank button under Plasma. Falling back keeps the
    button labelled on every desktop.
    """
    display = Gdk.Display.get_default()
    if display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        for name in candidates:
            if theme.has_icon(name):
                return name
    return candidates[0]


def notes_path_for(pdf_path):
    return os.path.splitext(pdf_path)[0] + "-notes.md"


# ── autosave snapshots ────────────────────────────────────────────────────────
# Unsaved changes are snapshotted here periodically; the original file is
# never touched until an explicit save. XDG_STATE_HOME, not cache — cache
# cleaners must not eat unsaved lecture notes.

AUTOSAVE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "sidemark", "autosave")


def _safe_filename(name):
    """Sanitise a string into a filesystem-safe filename (for drag-exported
    pages): drop path separators and control characters, collapse the rest."""
    name = name.replace(os.sep, "-")
    if os.altsep:
        name = name.replace(os.altsep, "-")
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return name or "page.pdf"


def _autosave_dir_for(path):
    key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:16]
    return os.path.join(AUTOSAVE_DIR, key)


def _find_autosave(path):
    """Return (snapshot_pdf, snapshot_notes_or_None, saved_at) when a
    recoverable snapshot newer than the file itself exists, else None."""
    d = _autosave_dir_for(path)
    snap_pdf = os.path.join(d, "doc.pdf")
    meta_path = os.path.join(d, "meta.json")
    if not (os.path.exists(snap_pdf) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if meta.get("path") != os.path.abspath(path):
        return None   # hash collision or moved file — don't recover blindly
    saved_at = meta.get("saved_at", 0)
    try:
        if os.path.getmtime(path) >= saved_at:
            return None   # the file was saved/modified after the snapshot
    except OSError:
        pass
    snap_notes = os.path.join(d, "notes.md")
    return snap_pdf, (snap_notes if os.path.exists(snap_notes) else None), saved_at


def _discard_autosave(path):
    shutil.rmtree(_autosave_dir_for(path), ignore_errors=True)


def _prune_autosaves(max_age_days=30):
    """Drop snapshots nobody recovered for a month (e.g. of deleted temp files)."""
    cutoff = time.time() - max_age_days * 86400
    try:
        entries = os.listdir(AUTOSAVE_DIR)
    except OSError:
        return
    for name in entries:
        d = os.path.join(AUTOSAVE_DIR, name)
        try:
            with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
                saved_at = json.load(f).get("saved_at", 0)
        except (OSError, ValueError):
            saved_at = 0
        if saved_at < cutoff:
            shutil.rmtree(d, ignore_errors=True)


_ANCHOR_RE = re.compile(r'<!--\s*anchor:(\d+):(\d+)\s*-->')
_CALLOUT_RE = re.compile(r'<!--\s*callout:(\d+):(\d+)\s*-->')
_TEXTBOX_RE = re.compile(r'<!--\s*textbox:(\d+):(\d+)\s*-->')
_MD_STRIP = [
    (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    (re.compile(r'\*([^*\n]+?)\*'), r'\1'),
    (re.compile(r'`([^`\n]+?)`'), r'\1'),
]


def _strip_markers(text):
    text = _ANCHOR_RE.sub('', text)
    text = _CALLOUT_RE.sub('', text)
    text = _TEXTBOX_RE.sub('', text)
    for pattern, repl in _MD_STRIP:
        text = pattern.sub(repl, text)
    return text.strip()


def _parse_anchors(text):
    """Parse anchor markers (and their optional callout companions) from notes
    text. Returns one dict per anchor:
      {x, y, callout: (cx, cy) | None, text: cleaned paragraph text,
       line: anchor line number, para_end: last line of its paragraph}
    A callout marker belongs to the nearest anchor before it, within the same
    paragraph (paragraphs end at the first blank line)."""
    lines = text.split('\n')
    n_lines = len(lines)
    line_starts = []
    off = 0
    for l in lines:
        line_starts.append(off)
        off += len(l) + 1
    result = []
    matches = list(_ANCHOR_RE.finditer(text))
    for i, m in enumerate(matches):
        ln = text[:m.start()].count('\n')
        para_end = n_lines - 1
        for j in range(ln + 1, n_lines):
            if not lines[j].strip():
                para_end = j - 1
                break
        para_end_off = line_starts[para_end] + len(lines[para_end])
        region_end = para_end_off
        if i + 1 < len(matches):
            region_end = min(region_end, matches[i + 1].start())
        cm = _CALLOUT_RE.search(text, m.end(), region_end) if region_end > m.end() else None
        result.append({
            "x": int(m.group(1)), "y": int(m.group(2)),
            "callout": (int(cm.group(1)), int(cm.group(2))) if cm else None,
            "text": _strip_markers('\n'.join(lines[ln:para_end + 1])),
            "line": ln, "para_end": para_end,
        })
    return result


def _parse_textboxes(text):
    """Parse standalone text-box markers (`<!-- textbox:X:Y -->`, no anchor).
    Returns one dict per box: {x, y, text, line, para_end} — the text is the
    box's paragraph (up to the next blank line), markers stripped."""
    lines = text.split('\n')
    n_lines = len(lines)
    result = []
    for m in _TEXTBOX_RE.finditer(text):
        ln = text[:m.start()].count('\n')
        para_end = n_lines - 1
        for j in range(ln + 1, n_lines):
            if not lines[j].strip():
                para_end = j - 1
                break
        result.append({
            "x": int(m.group(1)), "y": int(m.group(2)),
            "text": _strip_markers('\n'.join(lines[ln:para_end + 1])),
            "line": ln, "para_end": para_end,
        })
    return result


def _extract_pptx_notes(pptx_path):
    """Return {0-based slide index: speaker-notes text} for a .pptx, in the
    presentation's slide order, for slides that actually carry notes.

    A .pptx is a zip of OOXML parts: presentation.xml lists the slides in order
    (by relationship id), each slide's .rels points at its notesSlide part, and
    the notes text lives in the notes slide's body placeholder. Best-effort —
    any problem yields {} so importing notes never blocks opening the deck."""
    import zipfile
    import posixpath
    from xml.etree import ElementTree as ET

    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    PR = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    SKIP_PH = {"sldNum", "dt", "ftr", "hdr"}   # non-notes placeholders

    def _resolve(target, base_dir):
        if target.startswith("/"):
            return target.lstrip("/")
        return posixpath.normpath(posixpath.join(base_dir, target))

    def _rels_for(part):
        d, b = posixpath.split(part)
        return posixpath.join(d, "_rels", b + ".rels")

    def _rel_map(z, rels_part):
        out = {}
        try:
            root = ET.fromstring(z.read(rels_part))
        except (KeyError, ET.ParseError):
            return out
        for rel in root.findall(f"{REL}Relationship"):
            out[rel.get("Id")] = (rel.get("Type", ""), rel.get("Target", ""))
        return out

    def _notes_text(xml_bytes):
        root = ET.fromstring(xml_bytes)
        paras = []
        for sp in root.iter(f"{PR}sp"):
            ph = sp.find(f".//{PR}nvSpPr/{PR}nvPr/{PR}ph")
            if ph is not None and ph.get("type") in SKIP_PH:
                continue
            txbody = sp.find(f"{PR}txBody")
            if txbody is None:
                continue
            for p in txbody.findall(f"{A}p"):
                paras.append("".join(t.text or "" for t in p.iter(f"{A}t")))
        return "\n".join(paras)

    notes = {}
    try:
        with zipfile.ZipFile(pptx_path) as z:
            names = set(z.namelist())
            pres = ET.fromstring(z.read("ppt/presentation.xml"))
            lst = pres.find(f"{PR}sldIdLst")
            if lst is None:
                return {}
            rids = [s.get(f"{R}id") for s in lst.findall(f"{PR}sldId")]
            pres_rels = _rel_map(z, "ppt/_rels/presentation.xml.rels")
            for idx, rid in enumerate(rids):
                _, target = pres_rels.get(rid, ("", ""))
                if not target:
                    continue
                slide_part = _resolve(target, "ppt")
                slide_rels = _rel_map(z, _rels_for(slide_part))
                notes_part = None
                for _id, (rtype, rtarget) in slide_rels.items():
                    if rtype.endswith("/notesSlide"):
                        notes_part = _resolve(rtarget, posixpath.dirname(slide_part))
                        break
                if not notes_part or notes_part not in names:
                    continue
                text = _notes_text(z.read(notes_part)).strip()
                if text:
                    notes[idx] = text
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError):
        return {}
    return notes


def _pdf_needs_ocr(path, sample=10):
    """Heuristic: does this PDF look like a scan with no searchable text?

    Samples the first few pages; a document that carries images but has almost
    no extractable text is very likely a scan that would benefit from OCR.
    """
    try:
        doc = fitz.open(path)
    except Exception:
        return False
    try:
        n = doc.page_count
        if n == 0:
            return False
        pages = min(sample, n)
        text_chars = 0
        has_image = False
        for i in range(pages):
            page = doc.load_page(i)
            text_chars += len(page.get_text().strip())
            if not has_image and page.get_images(full=False):
                has_image = True
        # images present, but essentially no text → scanned
        return has_image and text_chars < 8 * pages
    finally:
        doc.close()


# Symbol substitution table — \sum → Σ etc. Applied for *display* only (the
# notes editor, callout boxes, PDF export). The .md sidecar always stores the
# source \commands so files round-trip cleanly through other Markdown editors.
_MD_SYMBOLS = {
    r'\sum': 'Σ', r'\prod': 'Π', r'\int': '∫',
    r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
    r'\epsilon': 'ε', r'\zeta': 'ζ', r'\eta': 'η', r'\theta': 'θ',
    r'\iota': 'ι', r'\kappa': 'κ', r'\lambda': 'λ', r'\mu': 'μ',
    r'\nu': 'ν', r'\xi': 'ξ', r'\pi': 'π', r'\rho': 'ρ',
    r'\sigma': 'σ', r'\tau': 'τ', r'\upsilon': 'υ', r'\phi': 'φ',
    r'\chi': 'χ', r'\psi': 'ψ', r'\omega': 'ω',
    r'\Gamma': 'Γ', r'\Delta': 'Δ', r'\Theta': 'Θ', r'\Lambda': 'Λ',
    r'\Xi': 'Ξ', r'\Pi': 'Π', r'\Sigma': 'Σ', r'\Phi': 'Φ',
    r'\Psi': 'Ψ', r'\Omega': 'Ω',
    r'\infty': '∞', r'\approx': '≈', r'\neq': '≠',
    r'\leq': '≤', r'\geq': '≥', r'\pm': '±', r'\times': '×',
    r'\div': '÷', r'\cdot': '·', r'\to': '→', r'\gets': '←', r'\mapsto': '↦',
    r'\in': '∈', r'\notin': '∉', r'\subset': '⊂', r'\supset': '⊃',
    r'\cup': '∪', r'\cap': '∩', r'\emptyset': '∅',
    r'\forall': '∀', r'\exists': '∃',
    r'\partial': '∂', r'\nabla': '∇',
}
_MD_SYMBOL_RE = re.compile(r'\\([A-Za-z]+)')

# LaTeX accents over a base symbol, rendered with Unicode combining marks:
# \hat{x} → x̂, \bar{x} → x̄, \tilde{x} → x̃, \vec{x} → x⃗ (and \dot / \ddot).
# The mark follows the base grapheme, so it sits on the first character of the
# (usually single-character) argument.
_MD_ACCENTS = {
    'hat': '̂', 'bar': '̄', 'tilde': '̃', 'vec': '⃗',
    'dot': '̇', 'ddot': '̈',
}
_MD_ACCENT_RE = re.compile(
    r'\\(' + '|'.join(_MD_ACCENTS) + r')\s*(?:\{([^}]*)\}|(\S))')


def _apply_accents(text):
    def sub(m):
        mark = _MD_ACCENTS[m.group(1)]
        base = m.group(2) if m.group(2) is not None else (m.group(3) or '')
        if not base:
            return mark
        return base[0] + mark + base[1:]
    return _MD_ACCENT_RE.sub(sub, text)


def _symbolize(text):
    """Replace LaTeX-style \\commands with their Unicode symbols (display only)."""
    text = _MD_SYMBOL_RE.sub(
        lambda m: _MD_SYMBOLS.get('\\' + m.group(1), m.group(0)), text)
    # Accents run after symbol substitution so \hat{\alpha} → α̂ (the inner
    # \alpha is already α by the time the accent is placed on it).
    return _apply_accents(text)


# Shared inline-Markdown / script regexes (used by the notes editor's TextTag
# rendering and by the callout Pango-markup rendering).
# Bold must come before italic so ** is consumed first; italic uses [^*\n] to
# avoid matching across ** markers or newlines.
_MD_INLINE_RE = re.compile(r'\*\*(.+?)\*\*|\*([^*\n]+?)\*|`([^`\n]+?)`')
# Super/subscript: ^{content} or ^x  /  _{content} or _x
_MD_SCRIPT_RE = re.compile(r'(\^|_)(?:\{([^}]*)\}|(\S+))')


def _notes_to_pango_markup(text):
    """One paragraph of notes source → Pango markup for callout rendering:
    \\commands become symbols (always), ^x/_x become super/subscripts, and
    **bold** / *italic* / `code` become the matching tags. Markers themselves
    are dropped (like the editor hides them off the cursor line)."""
    s = _symbolize(text)
    s = GLib.markup_escape_text(s)
    # scripts first (operate on clean escaped text), then inline can wrap them
    def _script(m):
        content = m.group(2) if m.group(2) is not None else m.group(3)
        tag = "sup" if m.group(1) == '^' else "sub"
        return f"<{tag}>{content}</{tag}>"
    s = _MD_SCRIPT_RE.sub(_script, s)

    def _inline(m):
        if m.group(1) is not None:
            return f"<b>{m.group(1)}</b>"
        if m.group(2) is not None:
            return f"<i>{m.group(2)}</i>"
        return f"<tt>{m.group(3)}</tt>"
    return _MD_INLINE_RE.sub(_inline, s)


# ── share a PDF to a phone over the LAN (#62) ─────────────────────────────────
def _lan_ip():
    """Best-effort LAN IP for a URL the phone can reach (no packet is sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # just selects the outgoing route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _tailscale_ip():
    """This machine's Tailscale (tailnet) IPv4 *while actually connected*, or
    None. A URL on this address reaches a phone that's on the same tailnet from
    anywhere — handy when LAN sharing is blocked by AP isolation or a repeater
    on a different subnet.

    NB: `tailscale ip -4` keeps printing the assigned address even after
    `tailscale down`, so we gate on the backend being "Running" (otherwise we'd
    hand out a QR that doesn't route)."""
    if shutil.which("tailscale"):
        try:
            out = subprocess.run(["tailscale", "status", "--json"],
                                 capture_output=True, text=True, timeout=3)
            data = json.loads(out.stdout)
            if data.get("BackendState") != "Running":
                return None
            for ip in data.get("Self", {}).get("TailscaleIPs", []):
                if ip.count(".") == 3:        # IPv4 only (skip the v6 address)
                    return ip
            return None
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    # fall back (no tailscale CLI / unparseable status): look for a live
    # 100.64.0.0/10 (CGNAT, what Tailscale uses) address on an interface. When
    # Tailscale is down the tun interface drops its address, so this won't
    # false-positive the way `tailscale ip` does.
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"],
                             capture_output=True, text=True, timeout=3)
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[3]:
                addr = parts[3].split("/")[0]
                octs = addr.split(".")
                if (len(octs) == 4 and octs[0] == "100"
                        and octs[1].isdigit() and 64 <= int(octs[1]) <= 127):
                    return addr
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _make_qr_png(url, out_path):
    """Render a QR PNG for url via the optional 'qrencode' tool. Returns True on
    success, False if qrencode isn't installed or fails (caller shows the URL)."""
    if not shutil.which("qrencode"):
        return False
    try:
        subprocess.run(["qrencode", "-s", "8", "-m", "2", "-o", out_path, url],
                       check=True, capture_output=True)
        return os.path.exists(out_path)
    except (subprocess.CalledProcessError, OSError):
        return False


def _html_escape(s):
    import html
    return html.escape(s or "", quote=True)


def _run_on_main(func, timeout=30):
    """Call func() on the GTK main thread from a worker thread and block until
    it returns (or raises). Used by the live share server, whose HTTP requests
    arrive on worker threads but must touch the document, which the UI owns.
    Must NOT be called from the main thread itself (it would deadlock)."""
    box, done = {}, threading.Event()

    def _cb():
        try:
            box["r"] = func()
        except Exception as e:                      # noqa: BLE001
            box["e"] = e
        finally:
            done.set()
        return False

    GLib.idle_add(_cb)
    if not done.wait(timeout):
        raise TimeoutError("main-thread call timed out")
    if "e" in box:
        raise box["e"]
    return box.get("r")


# The phone-facing page: a current-page image that auto-refreshes (so the viewer
# follows along live as you draw / flip pages) plus a Download button for the
# full annotated PDF. An <img> renders on every mobile browser; an embedded PDF
# does not (Android Chrome won't render PDFs inline in an iframe).
_SHARE_VIEWER_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif}
 header{position:sticky;top:0;z-index:1;display:flex;gap:.6rem;align-items:center;
   padding:.5rem .75rem;background:#1c1c1c;border-bottom:1px solid #333}
 header .t{flex:1;font-size:.9rem;overflow:hidden;text-overflow:ellipsis;
   white-space:nowrap}
 #page{font-size:.8rem;color:#bbb}
 a.btn{background:#3584e4;color:#fff;border-radius:7px;padding:.45rem .8rem;
   font-size:.85rem;text-decoration:none;white-space:nowrap}
 #live{display:flex;align-items:center;gap:.35rem;font-size:.72rem;color:#9ad29a;
   padding:.25rem .75rem}
 #dot{width:.5rem;height:.5rem;border-radius:50%;background:#9ad29a;
   animation:pulse 1.6s infinite}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
 #img{display:block;max-width:100%;height:auto;margin:0 auto;background:#fff}
 #err{display:none;color:#e0a0a0;padding:.5rem .75rem;font-size:.8rem}
</style></head>
<body>
<header>
 <span class=t>__TITLE__</span>
 <span id=page></span>
 <a class=btn href="doc.pdf" download="__TITLE__">Download</a>
</header>
<div id=live><span id=dot></span><span>Live — follows the presenter</span></div>
<div id=err>Lost connection to the computer.</div>
<img id=img alt="current page" src="page.png">
<script>
 let cur=null, fails=0;
 async function tick(){
   try{
     const r=await fetch('state',{cache:'no-store'});
     if(!r.ok) throw 0;
     const s=await r.json();
     fails=0; document.getElementById('err').style.display='none';
     document.getElementById('page').textContent='Page '+(s.page+1)+' / '+s.pages;
     const key=s.rev+'-'+s.page;
     if(key!==cur){cur=key;
       document.getElementById('img').src='page.png?v='+encodeURIComponent(key);}
   }catch(e){ if(++fails>3) document.getElementById('err').style.display='block'; }
   setTimeout(tick, 1500);
 }
 tick();
</script>
</body></html>"""


class _ShareServer:
    """A one-shot LAN HTTP server under a random, unguessable path. Two modes:

    * static  — serves a single file (legacy; `_ShareServer(path)`).
    * live    — serves a phone viewer that follows the document as it changes:
                `_ShareServer(providers=...)`, where providers is a dict with
                'title' (str), 'state' ()->(rev,page,pages), 'render'(path) and
                'pdf'(path). render/pdf are expected to already marshal to the
                main thread; state is read straight (cheap int reads)."""

    def __init__(self, file_path=None, *, providers=None):
        import secrets
        self.token = secrets.token_urlsafe(8)
        self.ip = _lan_ip()
        self.port = 0
        self.served = False
        self._httpd = None
        self.providers = providers
        self.file_path = file_path
        self.filename = (os.path.basename(file_path) if file_path
                         else (providers or {}).get("title", "document.pdf"))
        self._lock = threading.Lock()
        self._img_cache = {"key": None, "data": None}
        self._pdf_cache = {"rev": None, "data": None}
        self._tmp = (tempfile.mkdtemp(prefix="sidemark-live-")
                     if providers is not None else None)

    # ── live-mode body builders (cached; rendered/baked on demand) ──────────
    def _live_image(self):
        rev, page, _ = self.providers["state"]()
        key = (rev, page)
        with self._lock:
            if self._img_cache["key"] != key:
                p = os.path.join(self._tmp, "page.png")
                self.providers["render"](p)
                with open(p, "rb") as f:
                    self._img_cache = {"key": key, "data": f.read()}
            return self._img_cache["data"]

    def _live_pdf(self):
        rev, _, _ = self.providers["state"]()
        with self._lock:
            if self._pdf_cache["rev"] != rev or self._pdf_cache["data"] is None:
                p = os.path.join(self._tmp, "doc.pdf")
                self.providers["pdf"](p)
                with open(p, "rb") as f:
                    self._pdf_cache = {"rev": rev, "data": f.read()}
            return self._pdf_cache["data"]

    def start(self):
        import http.server
        import json as _json
        from urllib.parse import unquote
        server = self
        token, fname = self.token, self.filename

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, data, ctype, disposition=None):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if disposition:
                    self.send_header("Content-Disposition", disposition)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = unquote(self.path.split("?", 1)[0])
                base = f"/{token}/"
                if not path.startswith(base):
                    self.send_error(404)
                    return
                sub = path[len(base):]

                if server.providers is None:           # static, single file
                    if sub != fname:
                        self.send_error(404)
                        return
                    try:
                        with open(server.file_path, "rb") as f:
                            data = f.read()
                    except OSError:
                        self.send_error(404)
                        return
                    self._send(data, "application/pdf",
                               f'inline; filename="{fname}"')
                    server.served = True
                    return

                try:
                    if sub in ("", "index.html"):
                        html = _SHARE_VIEWER_HTML.replace(
                            "__TITLE__", _html_escape(fname))
                        self._send(html.encode("utf-8"),
                                   "text/html; charset=utf-8")
                    elif sub == "state":
                        rev, page, pages = server.providers["state"]()
                        body = _json.dumps(
                            {"rev": rev, "page": page, "pages": pages})
                        self._send(body.encode("utf-8"), "application/json")
                    elif sub == "page.png":
                        self._send(server._live_image(), "image/png")
                    elif sub == "doc.pdf":
                        self._send(server._live_pdf(), "application/pdf",
                                   f'attachment; filename="{fname}"')
                        server.served = True
                    else:
                        self.send_error(404)
                except Exception:                      # noqa: BLE001
                    self.send_error(503)

            def log_message(self, *_a):
                pass   # don't spam stderr

        self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def url_for(self, host):
        from urllib.parse import quote
        if self.providers is not None:
            return f"http://{host}:{self.port}/{self.token}/"
        return f"http://{host}:{self.port}/{self.token}/{quote(self.filename)}"

    @property
    def url(self):
        return self.url_for(self.ip)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)


def _draw_page_marks(out_page, notes_text, accent):
    """Draw a page's on-page marks into the given (copied) page: flatten ink to
    content first, then text boxes, callout boxes, and numbered anchor circles on
    top — same stacking as the canvas. Shared by the PDF export and the live
    phone-share page render so both look identical."""
    _flatten_ink(out_page)
    anchors = _parse_anchors(notes_text)
    for t in _parse_textboxes(notes_text):
        if t["text"]:
            _draw_export_textbox(out_page, t, accent)
    for a in anchors:
        if a["callout"] and a["text"]:
            _draw_export_callout(out_page, a, accent)
    for i, a in enumerate(anchors):
        _draw_export_anchor(out_page, a["x"], a["y"], i + 1, accent)


def _export_pdf_with_notes(src_path, out_path, notes_model, include_empty,
                           accent, group=False):
    """Bake the notes into a copy of the PDF.

    Each source page gets its on-page marks (text boxes, callout boxes, numbered
    anchor circles). A *notes page* then carries only the information that is NOT
    already on the page: callout and text-box text are skipped (they're drawn on
    the page) and empty anchors are skipped (only their circle is drawn). Anchor
    notes are prefixed with their [N] number.

    With group=True, small notes from consecutive pages are packed onto shared
    notes pages, each section headed by the page it came from. With group=False
    each annotated page is followed by its own notes page (and include_empty adds
    a notes page even for pages with nothing extra)."""
    src_doc = fitz.open(src_path)
    out_doc = fitz.open()
    anchor_color = accent

    def _draw_marks(out_page, notes_text, _anchors):
        _draw_page_marks(out_page, notes_text, anchor_color)

    if group:
        pending = []          # [(page_idx, [blocks])] waiting for a notes page
        last_dims = [595.0, 842.0]

        def _flush():
            if not pending:
                return
            w, h = last_dims
            wr = _NotesWriter(out_doc, w, h, anchor_color, title="Notes")
            for pi, blocks in pending:
                wr.section_heading(f"Page {pi + 1}")
                for b in blocks:
                    wr.paragraph(b)
                    wr.gap()
            pending.clear()

        for page_idx in range(len(src_doc)):
            notes_text = _symbolize(notes_model.get(page_idx))
            anchors = _parse_anchors(notes_text)
            out_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
            out_page = out_doc[-1]
            last_dims[0], last_dims[1] = out_page.rect.width, out_page.rect.height
            _draw_marks(out_page, notes_text, anchors)

            blocks = _export_notes_blocks(notes_text)
            if blocks:
                w, h = out_page.rect.width, out_page.rect.height
                usable = h - 45 - 40
                if (pending and _estimate_notes_height(pending, w)
                        + _estimate_notes_height([(page_idx, blocks)], w) > usable):
                    _flush()
                pending.append((page_idx, blocks))
        _flush()
    else:
        for page_idx in range(len(src_doc)):
            notes_text = _symbolize(notes_model.get(page_idx))
            anchors = _parse_anchors(notes_text)
            out_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
            out_page = out_doc[-1]
            _draw_marks(out_page, notes_text, anchors)

            blocks = _export_notes_blocks(notes_text)
            if blocks or include_empty:
                w, h = out_page.rect.width, out_page.rect.height
                wr = _NotesWriter(out_doc, w, h, anchor_color,
                                  title=f"Notes — Page {page_idx + 1}")
                wr.ensure_page()
                for b in blocks:
                    wr.paragraph(b)
                    wr.gap()

    out_doc.save(out_path, garbage=4, deflate=True)
    out_doc.close()
    src_doc.close()


def _flatten_ink(page):
    """Redraw the page's ink annotations as ordinary vector content and remove
    the annotations. Ink (pen/highlighter strokes) otherwise lives in annotation
    objects that many viewers — notably mobile browsers — don't render."""
    for annot in list(page.annots(types=[fitz.PDF_ANNOT_INK])):
        stroke = annot.colors.get("stroke") or [0, 0, 0]
        color = tuple(stroke[:3])
        width = (annot.border or {}).get("width", 1) or 1
        opacity = annot.opacity
        opacity = 1.0 if opacity is None or opacity < 0 else opacity
        for sub in annot.vertices or []:
            pts = [tuple(p) for p in sub]
            if len(pts) == 1:
                pts = [pts[0], pts[0]]      # a dot — draw a degenerate segment
            if len(pts) >= 2:
                page.draw_polyline(pts, color=color, width=width,
                                   stroke_opacity=opacity, lineCap=1, lineJoin=1)
        page.delete_annot(annot)


def _draw_export_anchor(page, px, py, number, color):
    radius = 6
    page.draw_circle((px, py), radius, color=color, fill=color)
    label = str(number)
    fontsize = radius * 1.5
    tw = fitz.Font("helv").text_length(label, fontsize)
    # Center the text baseline visually inside the circle
    page.insert_text((px - tw / 2, py + fontsize * 0.35),
                     label, fontsize=fontsize, color=(1, 1, 1), fontname="helv")


def _draw_export_callout(page, a, color):
    """Callout box with the anchor's paragraph text plus an arrow from the
    anchor — same layout as the canvas rendering."""
    fontsize = 8.5
    pad = 5.0
    box_w = 170.0
    page_rect = page.rect
    # Measure the exact height fitz's own wrapping needs on a scratch page —
    # estimating it ourselves risks a too-small rect, and insert_textbox
    # silently renders nothing when the text does not fit.
    text = a["text"]
    measure_doc = fitz.open()
    measure_page = measure_doc.new_page(width=page_rect.width, height=page_rect.height)
    measure_rect = fitz.Rect(0, 0, box_w - 2 * pad, page_rect.height)
    spare = measure_page.insert_textbox(measure_rect, text, fontsize=fontsize,
                                        fontname="helv", align=0)
    while spare < 0 and len(text) > 8:   # taller than a page: truncate
        text = text[:int(len(text) * 0.8)].rstrip() + "…"
        spare = measure_page.insert_textbox(measure_rect, text, fontsize=fontsize,
                                            fontname="helv", align=0)
    measure_doc.close()
    box_h = (measure_rect.height - max(spare, 0)) + 2 * pad + 2

    cx, cy = a["callout"]
    cx = min(max(cx, 0), page_rect.width - box_w)
    cy = min(max(cy, 0), page_rect.height - box_h)
    box = fitz.Rect(cx, cy, cx + box_w, cy + box_h)

    # arrow from anchor to nearest box-edge point
    ax, ay = a["x"], a["y"]
    attach = (min(max(ax, box.x0), box.x1), min(max(ay, box.y0), box.y1))
    dxv, dyv = attach[0] - ax, attach[1] - ay
    dist = math.hypot(dxv, dyv)
    if dist > 1.0:
        page.draw_line((ax, ay), attach, color=color, width=1.2)
        ux, uy = dxv / dist, dyv / dist
        head = 6.0
        base = (attach[0] - ux * head, attach[1] - uy * head)
        left = (base[0] - uy * head * 0.5, base[1] + ux * head * 0.5)
        right = (base[0] + uy * head * 0.5, base[1] - ux * head * 0.5)
        page.draw_line(attach, left, color=color, width=1.2)
        page.draw_line(attach, right, color=color, width=1.2)

    page.draw_rect(box, color=color, fill=(1, 1, 1), width=0.8, fill_opacity=0.95)
    text_rect = fitz.Rect(box.x0 + pad, box.y0 + pad, box.x1 - pad, box.y1 - pad)
    page.insert_textbox(text_rect, text, fontsize=fontsize,
                        color=(0.1, 0.1, 0.1), fontname="helv", align=0)


def _draw_export_textbox(page, t, color):
    """A standalone text box (#56) in the export — a callout box without the
    anchor/arrow, positioned at its own (x, y)."""
    fontsize = 8.5
    pad = 5.0
    box_w = 170.0
    page_rect = page.rect
    text = t["text"]
    measure_doc = fitz.open()
    measure_page = measure_doc.new_page(width=page_rect.width, height=page_rect.height)
    measure_rect = fitz.Rect(0, 0, box_w - 2 * pad, page_rect.height)
    spare = measure_page.insert_textbox(measure_rect, text, fontsize=fontsize,
                                        fontname="helv", align=0)
    while spare < 0 and len(text) > 8:
        text = text[:int(len(text) * 0.8)].rstrip() + "…"
        spare = measure_page.insert_textbox(measure_rect, text, fontsize=fontsize,
                                            fontname="helv", align=0)
    measure_doc.close()
    box_h = (measure_rect.height - max(spare, 0)) + 2 * pad + 2

    bx = min(max(t["x"], 0), page_rect.width - box_w)
    by = min(max(t["y"], 0), page_rect.height - box_h)
    box = fitz.Rect(bx, by, bx + box_w, by + box_h)
    page.draw_rect(box, color=color, fill=(1, 1, 1), width=0.8, fill_opacity=0.95)
    text_rect = fitz.Rect(box.x0 + pad, box.y0 + pad, box.x1 - pad, box.y1 - pad)
    page.insert_textbox(text_rect, text, fontsize=fontsize,
                        color=(0.1, 0.1, 0.1), fontname="helv", align=0)


def _export_notes_blocks(notes_text):
    """Content for one source page's notes page: one string per paragraph that
    carries information *not* already drawn on the page. Callout and text-box
    paragraphs are omitted (their text is rendered on the page), and empty
    anchors are omitted (only their numbered circle is drawn). Anchor notes are
    prefixed with their [N] number so they line up with the circles."""
    text = notes_text
    anchor_matches = list(_ANCHOR_RE.finditer(text))
    anchor_number = {m.start(): i + 1 for i, m in enumerate(anchor_matches)}

    lines = text.split('\n')
    n = len(lines)
    line_off, off = [], 0
    for l in lines:
        line_off.append(off)
        off += len(l) + 1

    blocks = []
    i = 0
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j < n and lines[j].strip():
            j += 1
        start = line_off[i]
        end = line_off[j - 1] + len(lines[j - 1])
        para = '\n'.join(lines[i:j])
        i = j
        # drawn on the page already → don't repeat it on the notes page
        if (_TEXTBOX_RE.search(text, start, end)
                or _CALLOUT_RE.search(text, start, end)):
            continue
        cleaned = _strip_markers(para)
        cleaned = re.sub(r'\n[ \t]*\n+', '\n', cleaned).strip()
        if not cleaned:
            continue                       # empty anchor / blank paragraph
        nums = [anchor_number[m.start()] for m in anchor_matches
                if start <= m.start() < end]
        if nums:
            cleaned = ''.join(f"[{k}] " for k in nums) + cleaned
        blocks.append(cleaned)
    return blocks


def _estimate_notes_height(sections, width):
    """Rough rendered height (pt) of grouped note sections — used only to decide
    when to start a new notes page; the actual layout wraps precisely."""
    lh = 10 * 1.45
    chars = max(10, int((width - 80) / (10 * 0.5)))
    total = 0.0
    for _pi, blocks in sections:
        total += lh + 6                    # section heading + gap
        for b in blocks:
            for logical in b.split('\n'):
                total += lh * max(1, math.ceil(len(logical) / chars))
            total += lh * 0.4              # inter-paragraph gap
    return total


class _NotesWriter:
    """Lays notes text across one or more notes pages, wrapping and page-breaking
    by hand so arbitrarily long notes flow cleanly. Pages are appended to the
    output document as they fill."""

    def __init__(self, out_doc, width, height, accent, title="Notes"):
        self.doc = out_doc
        self.w = width
        self.h = height
        self.accent = accent
        self.title = title
        self.margin = 40
        self.size = 10
        self.line_h = self.size * 1.45
        self.font = fitz.Font("helv")
        self.page = None
        self.y = 0.0

    def _new_page(self):
        self.page = self.doc.new_page(width=self.w, height=self.h)
        self.page.draw_line((self.margin, 30), (self.w - self.margin, 30),
                            color=(0.7, 0.7, 0.7))
        self.page.insert_text((self.margin, 24), self.title,
                              fontsize=11, color=(0.3, 0.3, 0.3), fontname="hebo")
        self.y = 45.0

    def ensure_page(self):
        """Make sure at least one (possibly empty) notes page exists."""
        if self.page is None:
            self._new_page()

    def _wrap(self, text, max_w):
        out = []
        for word in text.split(' '):
            if not out:
                out.append(word)
                continue
            trial = out[-1] + ' ' + word
            if self.font.text_length(trial, self.size) <= max_w:
                out[-1] = trial
            else:
                out.append(word)
        return out or ['']

    def _line(self, text, fontname, color, indent):
        if self.page is None or self.y + self.line_h > self.h - self.margin:
            self._new_page()
        self.page.insert_text((self.margin + indent, self.y + self.size),
                              text, fontsize=self.size, color=color,
                              fontname=fontname)
        self.y += self.line_h

    def section_heading(self, label):
        self.y += 6
        self._line(label, "hebo", self.accent, 0)

    def paragraph(self, text, indent=0):
        max_w = self.w - 2 * self.margin - indent
        for logical in text.split('\n'):
            for dl in self._wrap(logical, max_w):
                self._line(dl, "helv", (0, 0, 0), indent)

    def gap(self):
        self.y += self.line_h * 0.4


class NotesModel:
    """Per-page markdown notes, backed by a sidecar .md file."""

    def __init__(self):
        self._notes = {}
        self.pdf_name = None  # written as ![[name.pdf]] at top of the file

    def get(self, idx):
        return self._notes.get(idx, "")

    def has_content(self):
        """True if any page has non-whitespace notes (drives lazy file creation)."""
        return any(v.strip() for v in self._notes.values())

    def set(self, idx, text):
        self._notes[idx] = text

    def load(self, path):
        self._notes = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            return
        # Strip leading embed line (![[name.pdf]]) before parsing
        raw = re.sub(r'^\s*!\[\[.*?\]\]\n+', '', raw)
        # Format: <!-- page:N --> delimiters (invisible in markdown viewers)
        parts = re.split(r'<!--\s*page:(\d+)\s*-->', raw)
        if len(parts) == 1:
            # No page markers — an externally authored .md or an arbitrary text
            # file opened as notes: keep the whole thing as page-0 content.
            text = raw.strip()
            if text:
                self._notes[0] = text
            return
        for i in range(1, len(parts), 2):
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                self._notes[int(parts[i])] = content

    def shift_for_insert(self, idx, count=1):
        """Re-key notes after count pages were inserted at idx."""
        self._notes = {
            (k + count if k >= idx else k): v
            for k, v in self._notes.items()
        }

    def shift_for_delete(self, idx):
        """Drop the note of deleted page idx; re-key later pages."""
        self._notes = {
            (k - 1 if k > idx else k): v
            for k, v in self._notes.items()
            if k != idx
        }

    def reorder(self, old_to_new):
        """Re-key notes after pages were reordered. old_to_new maps each old
        page index to its new index."""
        self._notes = {
            old_to_new.get(k, k): v
            for k, v in self._notes.items()
        }

    def save(self, path):
        sections = [
            f"<!-- page:{idx} -->\n\n{self._notes[idx].strip()}"
            for idx in sorted(self._notes)
            if self._notes[idx].strip()
        ]
        body = "\n\n".join(sections) + "\n" if sections else ""
        embed = f"![[{self.pdf_name}]]\n\n" if self.pdf_name else ""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(embed + body)
        os.replace(tmp, path)


class MarkdownNotesView(GtkSource.View):
    """
    GtkSource.View with Typora-style in-place markdown rendering.
    Non-cursor lines: syntax markers hidden, bold/italic/code/heading applied.
    Cursor line: raw markdown visible for editing.
    """

    # Inline-Markdown / script regexes (module-level; shared with callout markup)
    _INLINE = _MD_INLINE_RE
    _SCRIPT_RE = _MD_SCRIPT_RE

    # Symbol substitution table (module-level; shared with export rendering)
    _SYMBOLS = _MD_SYMBOLS
    _SYMBOL_RE = _MD_SYMBOL_RE

    def __init__(self, scheme_id="Adwaita"):
        buf = GtkSource.Buffer()
        super().__init__(buffer=buf)
        self.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.set_left_margin(10)
        self.set_right_margin(10)
        self.set_top_margin(6)
        self.set_bottom_margin(10)
        self.add_css_class("notes-view")

        buf.set_language(GtkSource.LanguageManager.get_default().get_language("markdown"))
        buf.set_style_scheme(GtkSource.StyleSchemeManager.get_default().get_scheme(scheme_id))

        # Build TextTags
        tt = buf.get_tag_table()
        is_dark = scheme_id.endswith("dark")

        def tag(name, **props):
            tg = Gtk.TextTag.new(name)
            for k, v in props.items():
                tg.set_property(k.replace("_", "-"), v)
            tt.add(tg)
            return tg

        self._t = {
            "h1":          tag("h1",          weight=700, scale=1.5),
            "h2":          tag("h2",          weight=700, scale=1.25),
            "h3":          tag("h3",          weight=600, scale=1.1),
            "bold":        tag("bold",        weight=700),
            "italic":      tag("italic",      style=2),   # Pango.Style.ITALIC
            "code":        tag("code",        family="monospace",
                               background="#2d2d2d" if is_dark else "#f0f0f0",
                               foreground="#e06c75" if is_dark else "#c0392b"),
            "hide":        tag("hide",        invisible=True),
            "superscript": tag("superscript", rise=4000,  scale=0.65),
            "subscript":   tag("subscript",   rise=-2000, scale=0.65),
        }

        self._cursor_line = 0
        self._rehighlight_id = None
        self._in_highlight = False
        self._line_originals: dict[int, str] = {}
        buf.connect("notify::cursor-position", self._on_cursor_moved)
        buf.connect("changed", self._on_changed)
        # keep _line_originals keyed correctly when whole lines are added/removed,
        # so an already-rendered symbol line never loses its source \command
        buf.connect("insert-text", self._on_insert_text)
        buf.connect("delete-range", self._on_delete_range)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        # capture phase: the text view's built-in input-method controller also
        # runs at capture and CONSUMES printable keys (brackets, quotes) there —
        # a bubble-phase handler only ever sees modifier combos. Ours is
        # prepended, so at capture it runs before the IM; anything we don't
        # handle (return False) still reaches it and types normally.
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(key)

        # Ctrl+scroll rescales the notes font (mirrors the canvas zoom gesture)
        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # set by the window: called with +1 / -1 / 0 to grow / shrink / reset
        self.font_zoom_cb = None

    # ── formatting shortcuts ──────────────────────────────────────────────────

    def _on_scroll(self, ctrl, _dx, dy):
        ev = ctrl.get_current_event()
        if ev and (ev.get_modifier_state() & Gdk.ModifierType.CONTROL_MASK):
            if self.font_zoom_cb and dy:
                self.font_zoom_cb(-1 if dy > 0 else 1)
            return True
        return False

    def _on_key(self, ctrl, keyval, keycode, state):
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt_held = bool(state & Gdk.ModifierType.ALT_MASK)
        if ctrl_held and not alt_held:
            if keyval in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
                if self.font_zoom_cb:
                    self.font_zoom_cb(1)
                return True
            if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                if self.font_zoom_cb:
                    self.font_zoom_cb(-1)
                return True
            if keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
                if self.font_zoom_cb:
                    self.font_zoom_cb(0)
                return True
            if keyval == Gdk.KEY_b:
                self._wrap_selection("**")
                return True
            if keyval == Gdk.KEY_i:
                self._wrap_selection("*")
                return True
            if keyval == Gdk.KEY_e:
                self._wrap_selection("`")
                return True
            if keyval == Gdk.KEY_d:
                self._duplicate_lines()
                return True
            return False
        if alt_held and not ctrl_held:
            if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
                self._move_lines(-1)
                return True
            if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
                self._move_lines(1)
                return True
            return False
        if not ctrl_held and not alt_held:
            # typing a bracket / quote with text selected surrounds the
            # selection instead of replacing it (works with either half of a
            # bracket pair; the selection is kept so pairs can be chained)
            pair = self._SURROUND_CHARS.get(chr(Gdk.keyval_to_unicode(keyval) or 0))
            if pair and self.get_buffer().get_has_selection():
                self._surround_selection(*pair)
                return True
            # Slash snippets (/date, /time, /now) expand on the trigger key;
            # return False so the space/newline still gets inserted afterwards.
            if keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._expand_snippet()
        return False

    # typing any of these with a selection wraps it in the (open, close) pair
    _SURROUND_CHARS = {}
    for _o, _c in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
        _SURROUND_CHARS[_o] = (_o, _c)
        _SURROUND_CHARS[_c] = (_o, _c)
    for _q in ('"', "'", "`"):
        _SURROUND_CHARS[_q] = (_q, _q)
    del _o, _c, _q

    def _surround_selection(self, opener, closer):
        """Wrap the selection in opener…closer, keeping the inner text
        selected so further brackets can be stacked around it."""
        buf = self.get_buffer()
        s = buf.get_iter_at_mark(buf.get_selection_bound())
        e = buf.get_iter_at_mark(buf.get_insert())
        if s.compare(e) > 0:
            s, e = e, s
        # include hidden chars: the selection may span markers an already-
        # rendered line keeps under an invisible tag — delete+reinsert below
        # would otherwise strip them from the text
        text = buf.get_text(s, e, True)
        buf.begin_user_action()
        try:
            buf.delete(s, e)
            ins = buf.get_iter_at_mark(buf.get_insert())
            buf.insert(ins, opener + text + closer)
        finally:
            buf.end_user_action()
        end = buf.get_iter_at_mark(buf.get_insert())
        end.backward_chars(len(closer))
        start = end.copy()
        start.backward_chars(len(text))
        buf.select_range(start, end)

    # ── slash snippets (/date → today's date) ─────────────────────────────────

    @staticmethod
    def _snippet_value(token):
        """The replacement text for a slash snippet token, or None."""
        if token == "/date":
            return datetime.date.today().isoformat()           # 2026-06-15
        if token == "/time":
            return datetime.datetime.now().strftime("%H:%M")   # 14:09
        if token == "/now":
            return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        return None

    def _expand_snippet(self):
        """Replace the slash token just before the cursor with its value."""
        buf = self.get_buffer()
        if buf.get_has_selection():
            return False
        ins = buf.get_iter_at_mark(buf.get_insert())
        start = ins.copy()
        while not start.starts_line():                 # back up to the token start
            prev = start.copy()
            prev.backward_char()
            if prev.get_char().isspace():
                break
            start = prev
        value = self._snippet_value(buf.get_text(start, ins, False))
        if value is None:
            return False
        buf.begin_user_action()
        try:
            buf.delete(start, ins)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), value)
        finally:
            buf.end_user_action()
        return True

    # ── line operations (Ctrl+D duplicate, Alt+↑/↓ move) ──────────────────────

    def _line_range(self):
        """The line span the cursor or selection covers, as
        (buf, ins, bound, first, last). A selection that ends at column 0
        doesn't include that trailing line (matches common editors)."""
        buf = self.get_buffer()
        ins = buf.get_iter_at_mark(buf.get_insert())
        bound = buf.get_iter_at_mark(buf.get_selection_bound())
        first = min(ins.get_line(), bound.get_line())
        last = max(ins.get_line(), bound.get_line())
        if buf.get_has_selection() and last > first:
            lower = ins if ins.get_line() == last else bound
            if lower.get_line_offset() == 0:
                last -= 1
        return buf, ins, bound, first, last

    @staticmethod
    def _iter_at(buf, line, col):
        """Iter at line/col, clamping col to the line length and line to the buffer."""
        ok, it = buf.get_iter_at_line(line)
        if not ok:
            return buf.get_end_iter()
        end = it.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        it.forward_chars(min(col, end.get_line_offset()))
        return it

    def _duplicate_lines(self):
        """Ctrl+D — duplicate the current line, or every line the selection spans."""
        buf, ins, bound, first, last = self._line_range()
        start = buf.get_iter_at_line(first)[1]
        ok, end = buf.get_iter_at_line(last + 1)
        if not ok:
            end = buf.get_end_iter()
        block = buf.get_text(start, end, True)
        ins_line, ins_col = ins.get_line(), ins.get_line_offset()
        bnd_line, bnd_col = bound.get_line(), bound.get_line_offset()
        n = last - first + 1
        buf.begin_user_action()
        try:
            # a final line without a trailing newline needs one before the copy
            buf.insert(end, block if block.endswith("\n") else "\n" + block)
            buf.select_range(self._iter_at(buf, ins_line + n, ins_col),
                             self._iter_at(buf, bnd_line + n, bnd_col))
        finally:
            buf.end_user_action()

    def _move_lines(self, direction):
        """Alt+↑/↓ — move the current line (or selected lines) up or down."""
        buf, ins, bound, first, last = self._line_range()
        if direction < 0 and first == 0:
            return
        if direction > 0 and last >= buf.get_line_count() - 1:
            return
        lo, hi = (first - 1, last) if direction < 0 else (first, last + 1)
        start = buf.get_iter_at_line(lo)[1]
        ok, end = buf.get_iter_at_line(hi + 1)
        trailing = ok
        if not ok:
            end = buf.get_end_iter()
        region = buf.get_text(start, end, True)
        parts = region.split("\n")
        if trailing:
            parts = parts[:-1]              # drop the empty tail after the last newline
        if direction < 0:
            parts = parts[1:] + parts[:1]   # first line rotates to the bottom
        else:
            parts = parts[-1:] + parts[:-1] # last line rotates to the top
        new_region = "\n".join(parts) + ("\n" if trailing else "")
        ins_line, ins_col = ins.get_line(), ins.get_line_offset()
        bnd_line, bnd_col = bound.get_line(), bound.get_line_offset()
        buf.begin_user_action()
        try:
            mark = buf.create_mark(None, start, True)   # left gravity: insertion point
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(mark), new_region)
            buf.delete_mark(mark)
            buf.select_range(self._iter_at(buf, ins_line + direction, ins_col),
                             self._iter_at(buf, bnd_line + direction, bnd_col))
        finally:
            buf.end_user_action()

    def _wrap_selection(self, marker):
        """Wrap selection in marker, or unwrap if already wrapped. Selection is preserved.

        Auto-expand: if the selection is exactly inside an existing marker pair
        (e.g. cursor is on 'world' inside '**world**') the selection is silently
        expanded to include the markers before the toggle check, so Ctrl+B twice
        always round-trips to plain text.
        """
        buf = self.get_buffer()
        if not buf.get_has_selection():
            return
        s = buf.get_iter_at_mark(buf.get_selection_bound())
        e = buf.get_iter_at_mark(buf.get_insert())
        if s.compare(e) > 0:
            s, e = e, s

        # Auto-expand if the selection sits inside marker…marker
        s, e = self._expand_to_markers(buf, s, e, marker)

        # hidden chars included — same delete+reinsert data-loss guard as above
        text = buf.get_text(s, e, True)
        m = len(marker)
        # Already wrapped check (single * must not be part of **)
        already = (
            text.startswith(marker) and text.endswith(marker) and len(text) > 2 * m
            and not (m == 1 and (text.startswith(marker * 2) or text.endswith(marker * 2)))
        )
        buf.begin_user_action()
        try:
            buf.delete(s, e)
            ins = buf.get_iter_at_mark(buf.get_insert())
            if already:
                inner = text[m:-m]
                buf.insert(ins, inner)
                inner_len = len(inner)
            else:
                buf.insert(ins, marker + text + marker)
                inner_len = len(text)
            # Re-select just the inner content
            end_it = buf.get_iter_at_mark(buf.get_insert())
            if not already:
                end_it.backward_chars(m)
            start_it = end_it.copy()
            start_it.backward_chars(inner_len)
            buf.select_range(start_it, end_it)
        finally:
            buf.end_user_action()

    @staticmethod
    def _expand_to_markers(buf, s, e, marker):
        """If the m chars before s and after e both equal marker, return the expanded range."""
        m = len(marker)
        pre_s = s.copy()
        if not pre_s.backward_chars(m):
            return s, e
        if buf.get_text(pre_s, s, False) != marker:
            return s, e
        # Single * must not be part of **: check char before the marker
        if m == 1:
            guard = pre_s.copy()
            if guard.backward_chars(1) and buf.get_text(guard, pre_s, False) == marker:
                return s, e
        post_e = e.copy()
        post_e.forward_chars(m)
        if buf.get_text(e, post_e, False) != marker:
            return s, e
        # Single * must not be part of **: check char after the marker
        if m == 1:
            guard = post_e.copy()
            nxt = post_e.copy()
            if nxt.forward_chars(1) and buf.get_text(guard, nxt, False) == marker:
                return s, e
        return pre_s, post_e

    # ── signal handlers ───────────────────────────────────────────────────────

    def _on_cursor_moved(self, buf, _):
        line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        if line != self._cursor_line:
            self._cursor_line = line
            self._schedule()

    def _on_changed(self, _buf):
        if not self._in_highlight:
            self._schedule()

    def _schedule(self):
        if self._rehighlight_id is not None:
            GLib.source_remove(self._rehighlight_id)
        self._rehighlight_id = GLib.timeout_add(30, self._rehighlight)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _apply_symbol_subs(self, text):
        return _symbolize(text)

    def reset_render_state(self):
        """Forget per-line render bookkeeping. Call after replacing the whole
        buffer (page switch, undo) so a previous document's substituted-line
        originals can't leak onto the new content."""
        self._line_originals.clear()
        buf = self.get_buffer()
        self._cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()

    def get_source_text(self):
        """The buffer's text with display substitutions reversed — i.e. the
        canonical Markdown source (\\sum, not Σ). This, not the raw buffer, is
        what gets saved, autosaved, diffed for undo and stored in the model."""
        buf = self.get_buffer()
        out = []
        for ln in range(buf.get_line_count()):
            ok, ls = buf.get_iter_at_line(ln)
            if not ok:
                continue
            le = ls.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            # include_hidden_chars=True: rendered lines carry their markdown
            # markers (#, **, `) under an invisible tag — excluding them here
            # silently stripped the markers from every saved note (data loss)
            cur = buf.get_text(ls, le, True)
            orig = self._line_originals.get(ln)
            # only trust the stored source if it still renders to this line
            if orig is not None and _symbolize(orig) == cur:
                out.append(orig)
            else:
                out.append(cur)
        return "\n".join(out)

    def _on_insert_text(self, _buf, location, text, _len):
        # a real edit that adds whole lines shifts every substituted line below
        # the insertion point down by that many lines
        if self._in_highlight:
            return
        n = text.count("\n")
        if not n or not self._line_originals:
            return
        ins_line = location.get_line()
        self._line_originals = {
            (ln + n if ln > ins_line else ln): orig
            for ln, orig in self._line_originals.items()
        }

    def _on_delete_range(self, _buf, start, end):
        if self._in_highlight:
            return
        sl, el = start.get_line(), end.get_line()
        if sl == el or not self._line_originals:
            return
        n = el - sl
        shifted = {}
        for ln, orig in self._line_originals.items():
            if ln <= sl:
                shifted[ln] = orig
            elif ln <= el:
                continue          # this line is being deleted away
            else:
                shifted[ln - n] = orig
        self._line_originals = shifted

    def _buf_replace_line(self, buf, ln, new_text):
        ok, ls = buf.get_iter_at_line(ln)
        if not ok:
            return
        le = ls.copy()
        if not le.ends_line():
            le.forward_to_line_end()
        self._in_highlight = True
        try:
            buf.delete(ls, le)
            ins = buf.get_iter_at_line(ln)[1]
            buf.insert(ins, new_text)
        finally:
            self._in_highlight = False

    def _restore_line(self, buf, ln):
        original = self._line_originals.pop(ln, None)
        if original is None:
            return
        ok, ls = buf.get_iter_at_line(ln)
        if not ok:
            return
        le = ls.copy()
        if not le.ends_line():
            le.forward_to_line_end()
        if buf.get_text(ls, le, True) != original:
            self._buf_replace_line(buf, ln, original)

    def _rehighlight(self):
        self._rehighlight_id = None
        buf = self.get_buffer()
        self._cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()

        # Restore cursor line before clearing tags so its text is editable
        self._restore_line(buf, self._cursor_line)

        s, e = buf.get_start_iter(), buf.get_end_iter()
        for tg in self._t.values():
            buf.remove_tag(tg, s, e)

        for ln in range(buf.get_line_count()):
            ok, ls = buf.get_iter_at_line(ln)
            if not ok:
                continue
            le = ls.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            text = buf.get_text(ls, le, False)

            if ln != self._cursor_line and ln not in self._line_originals:
                subbed = self._apply_symbol_subs(text)
                if subbed != text:
                    self._line_originals[ln] = text
                    self._buf_replace_line(buf, ln, subbed)
                    ls = buf.get_iter_at_line(ln)[1]
                    text = subbed

            self._highlight_line(buf, ls, ln, text)
        return False

    def _highlight_line(self, buf, ls, ln, text):
        on_cursor = (ln == self._cursor_line)

        def at(n):
            it = ls.copy(); it.forward_chars(n); return it

        def apply(name, a, b):
            buf.apply_tag(self._t[name], at(a), at(b))

        def hide(a, b):
            if not on_cursor:
                apply("hide", a, b)

        # Heading
        m = re.match(r'^(#{1,3}) ', text)
        if m:
            lvl = len(m.group(1))
            apply(["h1", "h2", "h3"][lvl - 1], 0, len(text))
            hide(0, m.end())
            return

        # Inline: bold / italic / code (combined regex handles priority)
        for m in self._INLINE.finditer(text):
            a, b = m.start(), m.end()
            if m.group(1) is not None:       # **bold**
                apply("bold", a + 2, b - 2)
                hide(a, a + 2)
                hide(b - 2, b)
            elif m.group(2) is not None:     # *italic*
                apply("italic", a + 1, b - 1)
                hide(a, a + 1)
                hide(b - 1, b)
            else:                            # `code`
                apply("code", a, b)
                hide(a, a + 1)
                hide(b - 1, b)

        # Super/subscripts: ^{ab} ^x  _{ab} _x  — only rendered off cursor line
        if not on_cursor:
            for m in self._SCRIPT_RE.finditer(text):
                a, b = m.start(), m.end()
                tag_name = "superscript" if m.group(1) == '^' else "subscript"
                if m.group(2) is not None:   # braced: ^{content}
                    apply("hide", a, a + 2)  # hide ^{ or _{
                    apply(tag_name, a + 2, b - 1)
                    apply("hide", b - 1, b)  # hide }
                else:                        # single char: ^x or _x
                    apply("hide", a, a + 1)  # hide ^ or _
                    apply(tag_name, a + 1, b)


def _ink_path_for(md_path):
    """Sidecar holding the freehand ink of a text-first page. The .md itself
    stays pure Markdown so it round-trips through any other editor."""
    stem, _ext = os.path.splitext(md_path)
    return stem + "-ink.json"


class TextPageView(Gtk.Overlay):
    """Text-first mode: an endless A4-width sheet of Markdown you can draw on.

    The sheet is a MarkdownNotesView styled as white paper, centred in the
    themed surround; ink lives in a transparent DrawingArea overlaid on the
    scroll area. Each stroke is anchored to a GtkTextMark plus per-point
    offsets in buffer pixels, so it rides along with its paragraph when text
    above it is edited. The Markdown file stays pure text; ink round-trips
    through ink_to_json()/load_ink() into a `<name>-ink.json` sidecar and is
    re-matched by line hash, so external edits to the .md don't strand the
    drawings."""

    PAGE_WIDTH = 794          # ≈ A4 width at 96 dpi
    PAGE_GAP = 30             # surround gap around the sheet
    ERASE_RADIUS = 9          # px hit distance for the eraser
    MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 56, 48, 96   # inner paper margins
    ZOOM_MIN, ZOOM_MAX = 0.5, 3.0

    def __init__(self, font_px=13):
        super().__init__()
        self.tool = "text"        # text | pen | highlighter | eraser
        self.pan_tool = False     # pan rides on top: tool stays "text"
        self.zoom = 1.0           # sheet zoom: paper, text and ink together
        self._base_font_px = font_px
        self.font_px = font_px    # effective (base × zoom); strokes scale with it
        # {"mark", "pts": [(dx, dy), ...], "color", "width", "opacity", "font_px"}
        self.strokes = []
        self.current_stroke = []  # overlay coords while a stroke is in flight
        self._undo_ops = []       # ("add", [stroke, ...]) | ("erase", [stroke, ...])
        self._redo_ops = []
        self._erased_now = []     # strokes removed during the ongoing erase drag
        self.on_ink_action = None   # a draw/erase gesture finished (undo timeline)
        self.on_ink_changed = None  # any ink mutation (dirty tracking)
        # (color, width, opacity) for the given highlighter flag — the window
        # points this at the shared pen settings so both modes feel identical
        self.pen_style = lambda highlighter: ((0.05, 0.05, 0.8), 2.0, 1.0)

        # the sheet always reads like paper: white page, dark text, light scheme
        self.view = MarkdownNotesView("Adwaita")
        self.view.add_css_class("text-page")
        self.view.set_size_request(self.PAGE_WIDTH, -1)
        self.view.set_hexpand(False)
        self.view.set_vexpand(True)
        self.view.set_halign(Gtk.Align.CENTER)
        self.view.set_left_margin(self.MARGIN_X)
        self.view.set_right_margin(self.MARGIN_X)
        self.view.set_top_margin(self.MARGIN_TOP)
        self.view.set_bottom_margin(self.MARGIN_BOTTOM)
        # sheet zoom scales the paper and its font together via a provider
        # scoped to this instance (same pattern as the present-bar CSS)
        self._zoom_class = f"text-page-{id(self)}"
        self.view.add_css_class(self._zoom_class)
        self._zoom_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._zoom_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.view.set_margin_top(self.PAGE_GAP)
        self.view.set_margin_bottom(self.PAGE_GAP)
        self.view.set_margin_start(self.PAGE_GAP)
        self.view.set_margin_end(self.PAGE_GAP)

        # a Box wrapper keeps the view out of the ScrolledWindow's scrollable
        # protocol, so it grows to its full content height (endless paper) and
        # the viewport scrolls the whole sheet, margins included
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.append(self.view)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_hexpand(True)
        self.scroll.set_vexpand(True)
        self.scroll.add_css_class("text-surround")
        self.scroll.set_child(wrapper)
        self.set_child(self.scroll)

        self.ink = Gtk.DrawingArea()
        self.ink.set_draw_func(self._draw_ink)
        self.ink.set_can_target(False)   # clicks reach the text until a pen tool
        self.add_overlay(self.ink)

        drag = Gtk.GestureDrag()
        drag.set_button(0)               # pen draws, right button erases
        drag.connect("drag-begin", self._on_ink_begin)
        drag.connect("drag-update", self._on_ink_update)
        drag.connect("drag-end", self._on_ink_end)
        self.ink.add_controller(drag)
        self._drag = drag

        # One capture-phase drag on the overlay routes the modifier gestures,
        # mirroring the PDF canvas: Alt+drag draws with the pen, and Ctrl+drag
        # / middle-drag / the pan tool scroll the sheet. It sees the press
        # before the TextView and denies itself for anything else, so clicking
        # and selecting text work untouched.
        self._alt_saved_tool = None
        self._pan_start = None
        cap = Gtk.GestureDrag()
        cap.set_button(0)
        cap.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        cap.connect("drag-begin", self._on_capture_begin)
        cap.connect("drag-update", self._on_capture_update)
        cap.connect("drag-end", self._on_capture_end)
        self.add_controller(cap)
        self._capture_drag = cap

        # MX Master thumb button (btn 10): hold to pan — same legacy-event
        # approach as the canvas (gesture APIs drop button-10 sequences)
        self._thumb_panning = False
        self._thumb_origin = (0.0, 0.0)
        self._thumb_start = (0.0, 0.0)
        self._mouse_pos = (0.0, 0.0)
        thumb = Gtk.EventControllerLegacy()
        thumb.connect("event", self._on_thumb_event)
        self.add_controller(thumb)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        # the ink must repaint whenever the sheet scrolls or reflows under it
        self.scroll.get_vadjustment().connect(
            "value-changed", lambda *_: self.ink.queue_draw())
        self.scroll.get_hadjustment().connect(
            "value-changed", lambda *_: self.ink.queue_draw())
        self.view.get_buffer().connect(
            "changed", lambda *_: self.ink.queue_draw())

    # ── tool routing ─────────────────────────────────────────────────────────

    def set_tool(self, tool):
        """Drawing tools ink the sheet; pan drags it; anything else falls
        back to text editing."""
        self.pan_tool = (tool == "pan")
        self.tool = tool if tool in ("pen", "highlighter", "eraser") else "text"
        inking = self.tool != "text"
        self.ink.set_can_target(inking)
        self.ink.set_cursor(
            Gdk.Cursor.new_from_name("crosshair") if inking else None)
        # with the pan tool the caret makes no sense — show a grab hand
        self.view.set_cursor(Gdk.Cursor.new_from_name(
            "grab" if self.pan_tool else "text"))
        if not inking and self.current_stroke:
            self.current_stroke = []
            self.ink.queue_draw()

    def set_font_px(self, px):
        self._base_font_px = px
        self._apply_zoom()

    def set_zoom(self, z):
        self.zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, z))
        self._apply_zoom()

    def zoom_step(self, direction):
        """+1 zoom in, -1 out, 0 reset — wired to the sheet's Ctrl+scroll and
        Ctrl+= / Ctrl+- / Ctrl+0 (the notes-font gesture on the panel)."""
        if direction == 0:
            self.set_zoom(1.0)
        else:
            self.set_zoom(self.zoom * (1.1 ** direction))

    def _apply_zoom(self):
        """Width, margins and font all scale by the same factor, so the text
        wraps at the same words and the mark-anchored ink stays glued."""
        z = self.zoom
        self.font_px = self._base_font_px * z
        self.view.set_size_request(round(self.PAGE_WIDTH * z), -1)
        self.view.set_left_margin(round(self.MARGIN_X * z))
        self.view.set_right_margin(round(self.MARGIN_X * z))
        self.view.set_top_margin(round(self.MARGIN_TOP * z))
        self.view.set_bottom_margin(round(self.MARGIN_BOTTOM * z))
        self._zoom_css.load_from_data(
            f".{self._zoom_class} {{ font-size: {self.font_px:.1f}px; }}"
            .encode())
        self.ink.queue_draw()

    # ── coordinates ──────────────────────────────────────────────────────────
    # three spaces: overlay (the DrawingArea, == viewport), view widget
    # (scrolls with the sheet) and buffer (stable text-layout pixels). Strokes
    # are stored in buffer space relative to their anchor mark.

    def _overlay_to_buffer(self, x, y):
        res = self.ink.translate_coordinates(self.view, x, y)
        vx, vy = res if res else (x, y)
        return self.view.window_to_buffer_coords(
            Gtk.TextWindowType.WIDGET, int(vx), int(vy))

    def _buffer_to_overlay(self, bx, by):
        vx, vy = self.view.buffer_to_window_coords(
            Gtk.TextWindowType.WIDGET, int(bx), int(by))
        res = self.view.translate_coordinates(self.ink, vx, vy)
        return res if res else (vx, vy)

    def _stroke_overlay_pts(self, st):
        """Current on-screen positions of a stroke, following its mark and
        scaling with the notes font relative to when it was drawn."""
        buf = self.view.get_buffer()
        it = buf.get_iter_at_mark(st["mark"])
        r = self.view.get_iter_location(it)
        f = self.font_px / max(st["font_px"], 1)
        return [self._buffer_to_overlay(r.x + dx * f, r.y + dy * f)
                for dx, dy in st["pts"]]

    def stroke_view_pts(self, st):
        """Stroke points in sheet-widget coordinates — scroll-independent, the
        space a snapshot of the sheet renders in (used by the PDF export)."""
        buf = self.view.get_buffer()
        it = buf.get_iter_at_mark(st["mark"])
        r = self.view.get_iter_location(it)
        f = self.font_px / max(st["font_px"], 1)
        return [self.view.buffer_to_window_coords(
                    Gtk.TextWindowType.WIDGET,
                    int(r.x + dx * f), int(r.y + dy * f))
                for dx, dy in st["pts"]]

    def page_break_offsets(self, page_px):
        """Widget-y boundaries slicing the sheet into export pages of at most
        page_px, breaking at display-line tops so no text line is cut in half
        (a single over-tall line falls back to a hard cut)."""
        h = self.view.get_height()
        offs = [0.0]
        while offs[-1] + page_px < h:
            target = offs[-1] + page_px
            _bx, by = self.view.window_to_buffer_coords(
                Gtk.TextWindowType.WIDGET, 0, int(target))
            _ok, it = self.view.get_iter_at_location(0, by)
            r = self.view.get_iter_location(it)   # r.y = display-line top
            _wx, top = self.view.buffer_to_window_coords(
                Gtk.TextWindowType.WIDGET, 0, r.y)
            offs.append(float(top) if top > offs[-1] + 1 else target)
        offs.append(float(h))
        return offs

    # ── drawing gestures ─────────────────────────────────────────────────────

    def _pan_by(self, dx, dy):
        """Scroll the sheet so the content follows the pointer."""
        h, v = self.scroll.get_hadjustment(), self.scroll.get_vadjustment()
        sh, sv = self._pan_start
        h.set_value(sh - dx)
        v.set_value(sv - dy)

    def _on_capture_begin(self, gesture, x, y):
        btn = gesture.get_current_button()
        state = gesture.get_current_event_state()
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        # middle-drag / Ctrl+drag / the pan tool: scroll the sheet
        if btn == 2 or (btn == 1 and (ctrl or self.pan_tool)):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._pan_start = (self.scroll.get_hadjustment().get_value(),
                               self.scroll.get_vadjustment().get_value())
            return
        # Alt+drag with the text tool: draw with the pen while held
        if (btn == 1 and self.tool == "text"
                and state & Gdk.ModifierType.ALT_MASK):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._alt_saved_tool = self.tool
            self.tool = "pen"       # style + erase checks read self.tool
            self._on_ink_begin(gesture, x, y)
            return
        gesture.set_state(Gtk.EventSequenceState.DENIED)

    def _on_capture_update(self, gesture, dx, dy):
        if self._pan_start is not None:
            self._pan_by(dx, dy)
        elif self._alt_saved_tool is not None:
            self._on_ink_update(gesture, dx, dy)

    def _on_capture_end(self, gesture, dx, dy):
        if self._pan_start is not None:
            self._pan_start = None
            return
        if self._alt_saved_tool is None:
            return
        self._on_ink_end(gesture, dx, dy)
        self.tool = self._alt_saved_tool
        self._alt_saved_tool = None

    def _on_thumb_event(self, ctrl, event):
        if event is None:   # PyGObject sometimes fails to marshal the arg
            event = ctrl.get_current_event()
        if event is None:
            return False
        t = event.get_event_type()
        if t == Gdk.EventType.BUTTON_PRESS and event.get_button() == 10:
            self._thumb_panning = True
            self._thumb_origin = self._mouse_pos
            self._thumb_start = (self.scroll.get_hadjustment().get_value(),
                                 self.scroll.get_vadjustment().get_value())
        elif t == Gdk.EventType.BUTTON_RELEASE and event.get_button() == 10:
            self._thumb_panning = False
        return False

    def _on_motion(self, _ctrl, x, y):
        if self._thumb_panning:
            ox, oy = self._thumb_origin
            sh, sv = self._thumb_start
            self.scroll.get_hadjustment().set_value(sh - (x - ox))
            self.scroll.get_vadjustment().set_value(sv - (y - oy))
        self._mouse_pos = (x, y)

    def _on_ink_begin(self, gesture, x, y):
        button = gesture.get_current_button()
        self._erased_now = []
        if self.tool == "eraser" or button == 3:
            self._erase_at(x, y)
        else:
            self.current_stroke = [(x, y)]
        self.ink.queue_draw()

    def _on_ink_update(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        x, y = sx + dx, sy + dy
        if self.current_stroke:
            self.current_stroke.append((x, y))
        else:
            self._erase_at(x, y)
        self.ink.queue_draw()

    def _on_ink_end(self, gesture, dx, dy):
        if self.current_stroke:
            self._commit_stroke(self.current_stroke)
            self.current_stroke = []
        elif self._erased_now:
            self._undo_ops.append(("erase", self._erased_now))
            self._redo_ops.clear()
            self._erased_now = []
            if self.on_ink_action:
                self.on_ink_action()
        self.ink.queue_draw()

    def _commit_stroke(self, pts_overlay):
        if len(pts_overlay) < 2:
            return
        buf_pts = [self._overlay_to_buffer(x, y) for x, y in pts_overlay]
        # anchor at the first point: a left-gravity mark stays put when text is
        # typed right at it and rides along with every edit above it
        buf = self.view.get_buffer()
        _over_text, it = self.view.get_iter_at_location(*buf_pts[0])
        mark = buf.create_mark(None, it, True)
        r = self.view.get_iter_location(it)
        color, width, opacity = self.pen_style(self.tool == "highlighter")
        stroke = {
            "mark": mark,
            "pts": [(bx - r.x, by - r.y) for bx, by in buf_pts],
            "color": tuple(color),
            "width": width,
            "opacity": opacity,
            "font_px": self.font_px,
        }
        self.strokes.append(stroke)
        self._undo_ops.append(("add", [stroke]))
        self._redo_ops.clear()
        if self.on_ink_action:
            self.on_ink_action()
        if self.on_ink_changed:
            self.on_ink_changed()

    def _erase_at(self, x, y):
        rad2 = self.ERASE_RADIUS ** 2
        hit = [st for st in self.strokes
               if any((px - x) ** 2 + (py - y) ** 2 <= rad2
                      for px, py in self._stroke_overlay_pts(st))]
        if not hit:
            return
        for st in hit:
            self.strokes.remove(st)      # mark kept so undo can restore it
        self._erased_now.extend(hit)
        if self.on_ink_changed:
            self.on_ink_changed()

    # ── ink undo (driven by the window's chronological timeline) ─────────────

    def undo_ink(self):
        if not self._undo_ops:
            return
        kind, strokes = self._undo_ops.pop()
        if kind == "add":
            for st in strokes:
                if st in self.strokes:
                    self.strokes.remove(st)
        else:
            self.strokes.extend(strokes)
        self._redo_ops.append((kind, strokes))
        if self.on_ink_changed:
            self.on_ink_changed()
        self.ink.queue_draw()

    def redo_ink(self):
        if not self._redo_ops:
            return
        kind, strokes = self._redo_ops.pop()
        if kind == "add":
            self.strokes.extend(strokes)
        else:
            for st in strokes:
                if st in self.strokes:
                    self.strokes.remove(st)
        self._undo_ops.append((kind, strokes))
        if self.on_ink_changed:
            self.on_ink_changed()
        self.ink.queue_draw()

    # ── rendering ────────────────────────────────────────────────────────────

    def _draw_ink(self, _area, ctx, _w, _h):
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        for st in self.strokes:
            pts = self._stroke_overlay_pts(st)
            if len(pts) < 2:
                continue
            f = self.font_px / max(st["font_px"], 1)
            ctx.set_source_rgba(*st["color"], st["opacity"])
            ctx.set_line_width(st["width"] * f)
            ctx.move_to(*pts[0])
            for p in pts[1:]:
                ctx.line_to(*p)
            ctx.stroke()
        if len(self.current_stroke) >= 2:
            color, width, opacity = self.pen_style(self.tool == "highlighter")
            ctx.set_source_rgba(*color, opacity)
            ctx.set_line_width(width)
            ctx.move_to(*self.current_stroke[0])
            for p in self.current_stroke[1:]:
                ctx.line_to(*p)
            ctx.stroke()

    # ── persistence ──────────────────────────────────────────────────────────
    # Anchors are serialised as (line, char offset, source-line hash). On load
    # the hash re-matches strokes whose paragraph moved because the .md was
    # edited outside Sidemark; a stroke whose line vanished entirely still
    # lands at its old line number, clamped — degraded but never lost.

    @staticmethod
    def _line_hash(text):
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    def ink_to_json(self):
        buf = self.view.get_buffer()
        src_lines = self.view.get_source_text().split("\n")
        out = []
        for st in self.strokes:
            it = buf.get_iter_at_mark(st["mark"])
            line = it.get_line()
            src = src_lines[line] if line < len(src_lines) else ""
            out.append({
                "line": line,
                "ch": it.get_line_offset(),
                "hash": self._line_hash(src),
                "pts": [[round(dx, 2), round(dy, 2)] for dx, dy in st["pts"]],
                "color": list(st["color"]),
                "width": st["width"],
                "opacity": st["opacity"],
                "font_px": st["font_px"],
            })
        return {"version": 1, "strokes": out}

    def load_ink(self, data):
        buf = self.view.get_buffer()
        for st in self.strokes:
            buf.delete_mark(st["mark"])
        self.strokes = []
        self._undo_ops.clear()
        self._redo_ops.clear()
        src_lines = self.view.get_source_text().split("\n")
        hashes = [self._line_hash(t) for t in src_lines]
        for rec in data.get("strokes", []):
            line = int(rec.get("line", 0))
            want = rec.get("hash")
            if not (0 <= line < len(hashes) and hashes[line] == want):
                # the paragraph moved: take the matching line nearest the
                # remembered position, or stay at the clamped old line
                matches = [i for i, h in enumerate(hashes) if h == want]
                if matches:
                    line = min(matches, key=lambda i: abs(i - line))
                else:
                    line = max(0, min(line, len(hashes) - 1))
            ok, ls = buf.get_iter_at_line(line)
            if not ok:
                ls = buf.get_end_iter()
            it = ls.copy()
            it.forward_chars(min(int(rec.get("ch", 0)),
                                 it.get_chars_in_line()))
            mark = buf.create_mark(None, it, True)
            self.strokes.append({
                "mark": mark,
                "pts": [(p[0], p[1]) for p in rec.get("pts", [])],
                "color": tuple(rec.get("color", (0.05, 0.05, 0.8)))[:3],
                "width": float(rec.get("width", 2.0)),
                "opacity": float(rec.get("opacity", 1.0)),
                "font_px": float(rec.get("font_px", 13)),
            })
        self.ink.queue_draw()


class _ThumbnailProvider:
    """What the sidebar's generic thumbnail builder needs from a mode.

    The sidebar *shell* — Ctrl+T toggle, revealer, rows, current marker,
    lazy rendering, drag-to-reorder — is one shared code path; a provider
    supplies the mode's content and semantics. Features written against this
    interface reach every mode that implements the hook; inherently mode-bound
    behaviors are gated by the capability flags instead of mode checks:
    `can_export` (drag rows out as files + Ctrl+click multi-select),
    `can_insert_files` (drop a PDF between rows inserts its pages),
    `confirm_reorder` (guard reorders behind the confirmation dialog)."""

    can_export = False
    can_insert_files = False
    confirm_reorder = False
    noun = "page"                     # for tooltips / toasts

    def count(self):
        raise NotImplementedError

    def thumb_size(self, i):
        """(width, height) the i-th thumbnail will occupy, pre-render."""
        raise NotImplementedError

    def render(self, i):
        """Gdk.Texture for the i-th item (called lazily from an idle)."""
        raise NotImplementedError

    def activate(self, i):
        """Row clicked: show item i in the editor."""
        raise NotImplementedError

    def reorder(self, src, dst):
        """Move item src so it lands at index dst."""
        raise NotImplementedError

    def invalidated(self):
        """True when the underlying document was swapped out — pending lazy
        renders must stop."""
        return False


class _PdfThumbnails(_ThumbnailProvider):
    can_export = True
    can_insert_files = True
    confirm_reorder = True

    def __init__(self, win):
        self.win = win
        self.doc = win.canvas.document

    def count(self):
        return len(self.doc)

    def thumb_size(self, i):
        rect = self.doc[i].rect
        scale = (self.win.THUMB_WIDTH / rect.width) if rect.width else 0.2
        return self.win.THUMB_WIDTH, int(rect.height * scale)

    def render(self, i):
        page = self.doc[i]
        s = self.win.THUMB_WIDTH / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
        return Gdk.MemoryTexture.new(
            pix.width, pix.height, Gdk.MemoryFormat.R8G8B8,
            GLib.Bytes.new(pix.samples), pix.stride)

    def activate(self, i):
        self.win._go_to_page(i)
        self.win.canvas.grab_focus()

    def reorder(self, src, dst):
        self.win._do_reorder(src, dst)

    def tooltip(self, i):
        return (f"Page {i + 1} — click to open (PageUp/PageDown to flip), "
                "Ctrl+click to select, drag to reorder or export")

    def invalidated(self):
        return self.win.canvas.document is not self.doc


class _DeckThumbnails(_ThumbnailProvider):
    noun = "slide"

    def __init__(self, win):
        self.win = win
        self.dv = win._deck_view
        self.model = win._deck_view.model

    def count(self):
        return len(self.model.slides)

    def thumb_size(self, i):
        w = self.win.THUMB_WIDTH
        return w, round(w * 9 / 16)

    def render(self, i):
        return _deck_module().render_slide_texture(
            self.model.slides[i], self.win.THUMB_WIDTH)

    def activate(self, i):
        self.dv.set_current(i)
        self.dv.canvas.grab_focus()

    def reorder(self, src, dst):
        self.dv.move_slide(src, dst)   # one Ctrl+Z away — no confirmation

    def tooltip(self, i):
        return f"Slide {i + 1} — click to open, drag to reorder"

    def invalidated(self):
        return (not self.win._deck_mode
                or self.win._deck_view is not self.dv
                or self.dv.model is not self.model)


def _deck_module():
    """Lazy import of the Deck presentation editor (deck.py beside this file).
    Deferred so plain PDF/notes sessions never pay for it."""
    import deck as _deck
    _deck.logger = logger
    # deck textboxes render inline math / Markdown with the same machinery as
    # callouts (\alpha→α, x^2 superscripts, **bold**, *italic*, `code`)
    _deck.notes_to_markup = _notes_to_pango_markup
    return _deck


def _session_prop(name):
    """A per-document attribute that physically lives on the active
    DocumentSession. The window's ~140 methods keep reading/writing self.<name>
    unchanged; it transparently follows whichever tab is active."""
    return property(
        lambda self: getattr(self._active_session, name),
        lambda self, value: setattr(self._active_session, name, value),
    )


class DocumentSession:
    """One open document — its canvas, notes editor, sidebar, search bar and all
    the per-document state. The window owns an Adw.TabView of these and proxies
    the active one's attributes onto itself via _session_prop, so multiple PDFs
    can be open as tabs without rewriting every method to thread a session.

    A session shows one of three document types — the window is one unified UI
    and `doc_mode` picks which set of tools it wears:
      "pdf"  — the PDF canvas with the notes panel beside it
      "text" — a text-first page (endless Markdown sheet with ink)
      "deck" — a slide deck (the Deck presentation editor)
    `_text_mode`/`_deck_mode` are compatibility views over doc_mode — the
    window's many call sites keep reading/writing the booleans unchanged."""

    @property
    def _text_mode(self):
        return self.doc_mode == "text"

    @_text_mode.setter
    def _text_mode(self, on):
        if on:
            self.doc_mode = "text"
        elif self.doc_mode == "text":
            self.doc_mode = "pdf"

    @property
    def _deck_mode(self):
        return self.doc_mode == "deck"

    @_deck_mode.setter
    def _deck_mode(self, on):
        if on:
            self.doc_mode = "deck"
        elif self.doc_mode == "deck":
            self.doc_mode = "pdf"

    # names proxied onto PDFEditorWindow via _session_prop — keep in sync with
    # the property declarations in the window class body.
    STATE = (
        "_path", "_notes_path", "_active_notes_path", "_is_untitled", "_dirty",
        "notes_model", "_undo_timeline", "_redo_timeline", "_notes_burst_open",
        "_burst_base", "_anchor_line_nos", "_anchor_para_ends", "_search_hits",
        "_note_hits", "_search_matches", "_search_current", "_presenter",
        "_last_anchor_mark", "_link_hint_shown", "_saved_pane_pos", "_pane_anim",
        "_thumb_idle_id", "_current_thumb_row", "_drag_export_dir",
        "_has_toc", "_toc_thumbs", "_drop_indicator_row", "_text_mode",
        "_deck_mode", "_deck_path",
    )
    WIDGETS = (
        "canvas", "_notes_view", "_panel_notes_view", "_notes_box",
        "_search_revealer", "_search_entry", "_search_label", "_paned",
        "_toc_list", "_toc_scroll", "_toc_revealer", "_toc_switch",
        "_toc_seg_outline", "_toc_seg_pages", "content", "_text_page",
        "_deck_view", "_canvas_box",
    )

    def __init__(self):
        self._path = None
        self._notes_path = None     # set when a .md is opened without a PDF
        self._active_notes_path = None
        self._is_untitled = False
        self._dirty = False
        self.notes_model = NotesModel()
        self._undo_timeline = []
        self._redo_timeline = []
        self._notes_burst_open = False
        self._burst_base = ""
        self._anchor_line_nos = []
        self._anchor_para_ends = []
        self._search_hits = {}
        self._note_hits = {}
        self._search_matches = []
        self._search_current = -1
        self._presenter = None
        self._last_anchor_mark = None
        self._link_hint_shown = False
        self._saved_pane_pos = 800
        self._pane_anim = None
        self._thumb_idle_id = None
        self._current_thumb_row = None
        self._drag_export_dir = None
        self._has_toc = False
        self._toc_thumbs = False
        self._drop_indicator_row = None
        self.doc_mode = "pdf"       # pdf | text | deck (see class docstring)
        self._deck_path = None      # the .smdeck path (kept off _path so the
                                    # PDF autosave/save paths never touch it)
        self._tab_page = None       # the Adw.TabPage hosting this session
        # widgets are built by the window and assigned through the proxies
        for w in self.WIDGETS:
            setattr(self, w, None)


class PDFEditorWindow(Adw.ApplicationWindow):
    # per-document attributes proxied to self._active_session (see DocumentSession)
    for _n in DocumentSession.STATE + DocumentSession.WIDGETS:
        locals()[_n] = _session_prop(_n)
    del _n

    def __init__(self, app):
        super().__init__(application=app, title="Sidemark")
        self.set_default_size(1280, 800)
        self._active_session = DocumentSession()
        self._sessions = [self._active_session]
        self._closed_tabs = []   # reopen stack for Ctrl+Shift+T (file paths)
        self._share_revision = 0  # bumps on every change; drives live phone share
        self._path = None
        self._notes_path = None   # set when a .md file is opened without an associated PDF
        self._active_notes_path = None  # the .md a loaded PDF saves notes to (default sidecar, or a user-chosen file remembered per-PDF)
        self._is_untitled = False  # True when working on an auto-created blank (no saved path yet)
        self._dirty = False
        self._suppress_dirty = False
        self._syncing_pen = False   # guard while pen popover mirrors tool state
        self._syncing_mode = False  # guard while tool-mode toggles mirror each other
        # responsive header: natural width (px) of the button clusters at each
        # collapse level, measured once from the real widgets — no hard-coded
        # threshold. _header_controls is the non-content width (window buttons +
        # edge padding) read from the real allocation.
        self._collapse_natural = None
        self._header_controls = 0
        self._collapse_level = -1
        # global chronological undo: one entry per canvas gesture, one per
        # uninterrupted typing burst in the notes panel.
        # ("canvas",) | ("notes", page_idx, text_before_burst)
        self._undo_timeline = []
        # redo: ("canvas",) | ("notes", page_idx, before, after)
        self._redo_timeline = []
        self._notes_burst_open = False
        self._burst_base = ""   # buffer text at the last burst boundary
        self.notes_model = NotesModel()
        self._anchor_line_nos = []   # line number in buffer for each anchor on current page
        self._anchor_para_ends = []  # last line of each anchor's paragraph (until next blank line)
        self._search_hits = {}      # {page_idx: [fitz.Rect, ...]} — PDF hits
        self._note_hits = {}        # {page_idx: [(start, end), ...]} — notes hits
        # unified flat list, ordered by page: ("pdf", page, rect_idx) or
        # ("note", page, start_offset, end_offset)
        self._search_matches = []
        self._search_current = -1   # index into _search_matches

        theme = _load_theme()
        bg = _hex_to_rgb(theme["background"])
        fg = _hex_to_rgb(theme["foreground"])
        acc = _hex_to_rgb(theme["accent"])
        surround = tuple(b + 0.12 * (f - b) for b, f in zip(bg, fg))
        _lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        _src_scheme = "Adwaita-dark" if _lum < 0.5 else "Adwaita"
        # stashed so _build_document_widgets can build each tab's subtree
        self._theme_surround = surround
        self._theme_acc = acc
        self._src_scheme = _src_scheme
        self._swatch_presets = [
            ("Accent", acc),
            ("Red",    _hex_to_rgb(theme["color1"])),
            ("Black",  _hex_to_rgb(theme["foreground"])),
            ("Brown",  _hex_to_rgb(theme["color3"])),
            ("Teal",   _hex_to_rgb(theme["color6"])),
            ("Gray",   _hex_to_rgb(theme["color8"])),
        ]

        # build the first document's canvas / notes / sidebar / search subtree
        # into the active session (additional tabs reuse the same builder)
        self._build_document_widgets(self._active_session)

        GLib.timeout_add_seconds(60, self._autosave_tick)
        self._transient_tool = None    # window-level: highlights a shared tool button
        self._alt_pen_restore = None   # text page: tool to restore when Alt lets go
        self._ocr_seen = set()         # PDFs we've already offered to OCR this session
        self._ocr_hint_shown = False   # one-time "install ocrmypdf" hint

        # ── CSS ───────────────────────────────────────────────────────────────
        acc_hex = "#{:02x}{:02x}{:02x}".format(*(int(c * 255) for c in acc))
        fg_hex  = theme["foreground"]
        bg_hex  = theme["background"]
        surround_hex = "#{:02x}{:02x}{:02x}".format(
            *(int(c * 255) for c in surround))
        css = f"""
            .notes-view {{
                font-family: monospace;
                background-color: {bg_hex};
                color: {fg_hex};
            }}
            /* text-first mode: the sheet always reads as white paper (like a
               PDF page), whatever the theme; the surround takes the theme */
            .text-page, .text-page text {{
                background-color: #ffffff;
                color: #16181c;
                caret-color: #16181c;
            }}
            .text-page {{
                border-radius: 3px;
                box-shadow: 0 2px 12px rgba(0, 0, 0, 0.30);
            }}
            .text-surround, .text-surround > viewport {{
                background-color: {surround_hex};
            }}
            .shortcut-key {{
                font-family: monospace;
                font-size: 12px;
                background-color: shade({bg_hex}, 0.93);
                border: 1px solid shade({bg_hex}, 0.82);
                border-radius: 3px;
                padding: 1px 5px;
            }}
            .pen-swatch {{
                min-width: 22px;
                min-height: 22px;
                padding: 0;
                border-radius: 4px;
                border: 1px solid shade({bg_hex}, 0.75);
            }}
            .pen-swatch:hover {{ border: 2px solid {fg_hex}; }}
            .tool-transient {{
                background-color: alpha({acc_hex}, 0.30);
                box-shadow: inset 0 0 0 1px {acc_hex};
            }}
            .current-page {{
                box-shadow: inset 0 0 0 2px {acc_hex};
                border-radius: 4px;
            }}
            .drop-before {{ box-shadow: inset 0 3px 0 0 {acc_hex}; }}
            .drop-after  {{ box-shadow: inset 0 -3px 0 0 {acc_hex}; }}
        """
        # (the presentation bar styles live in their own per-window provider —
        # _scale_present_bar — because they scale with the window size)
        for i, (_, rgb) in enumerate(self._swatch_presets):
            css += f"\n            .pen-swatch-{i} {{ background: " \
                   + "#{:02x}{:02x}{:02x}".format(*(int(c * 255) for c in rgb)) \
                   + "; }"
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Notes-panel font size lives in its own provider so Ctrl+± / Ctrl+scroll
        # can rescale it at runtime (handy for reading presenter notes). Added
        # after the base sheet so it wins; the value is a global preference.
        self._notes_font_px = max(
            self._NOTES_FONT_MIN,
            min(self._NOTES_FONT_MAX,
                int(_load_settings().get("notes_font_px", self._NOTES_FONT_DEFAULT))),
        )
        self._notes_font_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._notes_font_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )
        self._apply_notes_font()

        # ── header bar ────────────────────────────────────────────────────────
        # Adw.ApplicationWindow has no titlebar slot; the header goes in an
        # Adw.ToolbarView top bar, which (unlike set_titlebar) stays visible in
        # fullscreen.
        header = Gtk.HeaderBar()
        self._header = header
        # Suppress the default centred window-title label; the bar is purely
        # grouped button clusters flowing from the edges.
        header.set_title_widget(Gtk.Box())

        def vsep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(3)
            s.set_margin_end(3)
            return s

        # ── ☰ menu: occasional file actions live here, not on the bar ──────────
        # "Open recent" and "Keyboard shortcuts" used to open a *second* popover
        # on this same button; GTK4 suppresses a popup while the first popover is
        # still up/dismissing (the cause of the dead-first-click bug, #63). So the
        # menu is a Gtk.Stack instead: those items switch to an in-menu page
        # rather than opening a sibling popover — no second popover, no race.
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text("Menu")

        menu_pop = Gtk.Popover()
        menu_btn.set_popover(menu_pop)
        self._menu_pop = menu_pop
        self._menu_stack = Gtk.Stack()
        self._menu_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._menu_stack.set_transition_duration(120)
        menu_pop.set_child(self._menu_stack)
        # always reopen on the main page
        menu_pop.connect("show", lambda _p: self._menu_stack.set_visible_child_name("main"))

        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)
        self._menu_stack.add_named(menu_box, "main")

        # current filename shown at the top of the menu (no room on the bar)
        self._file_label = Gtk.Label(label="", xalign=0)
        self._file_label.add_css_class("dim-label")
        self._file_label.add_css_class("caption")
        self._file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._file_label.set_max_width_chars(28)
        self._file_label.set_margin_start(6)
        self._file_label.set_margin_bottom(2)
        menu_box.append(self._file_label)
        msep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        msep.set_margin_bottom(2)
        menu_box.append(msep)

        def _menu_item(label, callback, tooltip=None):
            item = Gtk.Button()
            item.add_css_class("flat")
            item.set_child(Gtk.Label(label=label, xalign=0))
            item.connect("clicked", lambda _b: callback())
            if tooltip:
                item.set_tooltip_text(tooltip)
            menu_box.append(item)
            return item

        _menu_item("Open…",
                   lambda: (menu_pop.popdown(), self._on_open(None)),
                   "Open a PDF, PowerPoint, or text/Markdown file (Ctrl+O)")
        self._recent_menu_item = _menu_item(
            "Open recent", lambda: self._show_menu_page("recent"),
            "Reopen a recently used file")
        _menu_item("New", lambda: (menu_pop.popdown(), self._on_new_pdf(None)),
                   "Start a new blank document (Ctrl+N)")
        _menu_item("New text page",
                   lambda: (menu_pop.popdown(), self._on_new_text_page()),
                   "A blank Markdown page you can write and draw on (Ctrl+Alt+N)")
        _menu_item("New presentation",
                   lambda: (menu_pop.popdown(), self._on_new_presentation()),
                   "Create a slide deck — 16:9 slides with text boxes and "
                   "images (Ctrl+Alt+P)")
        _menu_item("Save", lambda: (menu_pop.popdown(), self._on_save()),
                   "Save the document and its notes (Ctrl+S)")
        # PDF-only actions — hidden while a text-first page is active
        # (see _update_header_for_mode)
        self._pdf_menu_items = (
            _menu_item("Export with notes…",
                       lambda: (menu_pop.popdown(), self._on_export()),
                       "Export a PDF with your notes laid out after each page (Ctrl+E)"),
            _menu_item("Add text layer (OCR)",
                       lambda: (menu_pop.popdown(), self._ocr_current()),
                       "Run OCR so a scanned document's text becomes selectable and searchable"),
            _menu_item("Share to phone…",
                       lambda: (menu_pop.popdown(), self._on_share_to_phone()),
                       "Show a QR code to open this PDF on a phone on the same Wi-Fi"),
            _menu_item("Notes file…",
                       lambda: (menu_pop.popdown(), self._choose_notes_file()),
                       "Choose which Markdown file this document's notes are saved to"),
        )
        # text-page-only actions — the inverse of the group above
        self._text_menu_items = (
            _menu_item("Export as PDF…",
                       lambda: (menu_pop.popdown(), self._on_export_text_pdf()),
                       "Render this page — text and ink — into an A4 PDF"),
        )
        for item in self._text_menu_items:
            item.set_visible(False)
        # deck-only actions — shown while a presentation is active
        self._deck_menu_items = (
            _menu_item("Export as PDF…",
                       lambda: (menu_pop.popdown(), self._on_export_deck_pdf()),
                       "Render the slides into a 16:9 PDF, one page per slide"),
        )
        for item in self._deck_menu_items:
            item.set_visible(False)
        msep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        msep2.set_margin_top(2)
        msep2.set_margin_bottom(2)
        menu_box.append(msep2)
        _menu_item("Keyboard shortcuts", lambda: self._show_menu_page("shortcuts"),
                   "Show the full list of keyboard shortcuts")

        # sub-pages reached from the menu (recent files, keyboard shortcuts)
        self._recent_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        recent_scroll = Gtk.ScrolledWindow()
        recent_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        recent_scroll.set_max_content_height(420)
        recent_scroll.set_propagate_natural_height(True)
        recent_scroll.set_propagate_natural_width(True)
        recent_scroll.set_child(self._recent_list_box)
        self._menu_stack.add_named(
            self._menu_subpage("Recent files", recent_scroll), "recent")
        self._menu_stack.add_named(
            self._menu_subpage("Keyboard shortcuts",
                               self._build_shortcuts_content()), "shortcuts")

        # ── outline / thumbnails sidebar toggle ────────────────────────────────
        # stays sensitive even without a TOC — insensitive widgets get no
        # tooltip in GTK4, and the tooltip is how we explain the situation
        self._has_toc = False
        self._toc_thumbs = False
        self._thumb_idle_id = None
        self._thumb_provider = None      # the mode's _ThumbnailProvider
        self._current_thumb_row = None   # row carrying the .current-page CSS marker
        self._drop_indicator_row = None  # row carrying the drop-gap CSS marker
        self._drag_export_dir = None   # lazily created temp dir for drag-exported pages
        self._toc_btn = Gtk.ToggleButton()
        self._toc_btn.set_icon_name(
            _themed_icon("view-list-symbolic", "view-list-text-symbolic",
                         "format-justify-fill-symbolic"))
        self._toc_btn.set_tooltip_text("No document open")
        self._toc_btn.connect("toggled", self._on_toc_toggled)

        # ── page navigation ────────────────────────────────────────────────────
        prev_btn = Gtk.Button()
        prev_btn.set_icon_name("go-previous-symbolic")
        prev_btn.set_tooltip_text("Previous page (PageUp)")
        prev_btn.connect("clicked", lambda _: self._go_to_page(self.canvas.current_page_idx - 1))

        self._page_label = Gtk.Label(label="—")
        self._page_label.set_width_chars(7)

        next_btn = Gtk.Button()
        next_btn.set_icon_name("go-next-symbolic")
        next_btn.set_tooltip_text("Next page (PageDown)")
        next_btn.connect("clicked", lambda _: self._go_to_page(self.canvas.current_page_idx + 1))

        self._nav_box = nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav_box.add_css_class("linked")
        nav_box.append(prev_btn)
        nav_box.append(self._page_label)
        nav_box.append(next_btn)

        # ── add / delete page (linked group next to nav) ───────────────────────
        self._add_page_btn = Gtk.Button()
        self._add_page_btn.set_icon_name("list-add-symbolic")
        self._add_page_btn.set_tooltip_text("Add blank page after this one (Ctrl+Shift+N)")
        self._add_page_btn.connect("clicked", lambda _: self._add_blank_page())

        self._del_page_btn = Gtk.Button()
        self._del_page_btn.set_icon_name("list-remove-symbolic")
        self._del_page_btn.set_tooltip_text("Delete current page (Ctrl+Shift+Delete)")
        self._del_page_btn.connect("clicked", lambda _: self._delete_current_page())

        self._pages_box = pages_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        pages_box.add_css_class("linked")
        pages_box.append(self._add_page_btn)
        pages_box.append(self._del_page_btn)

        # ── tool-mode switch: pen / highlighter / select (icons, segmented) ─────
        # No themed icons exist for highlighter or text-select, so both are tiny
        # custom cairo glyphs; pen uses the standard pencil.
        def _draw_mode_hl(_a, ctx, w, h):
            r, g, b = self.canvas.hl_color
            ctx.set_source_rgba(r, g, b, max(self.canvas.hl_opacity, 0.55))
            ctx.set_line_width(7)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.move_to(4, h - 5)
            ctx.line_to(w - 4, 5)
            ctx.stroke()

        def _draw_mode_sel(_a, ctx, w, h):
            ctx.set_source_rgba(*acc, 0.40)
            ctx.rectangle(1.5, 3, w - 3, h - 6)
            ctx.fill()
            ctx.set_source_rgb(*fg)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(11)
            ext = ctx.text_extents("A")
            ctx.move_to((w - ext.width) / 2 - ext.x_bearing,
                        (h - ext.height) / 2 - ext.y_bearing)
            ctx.show_text("A")

        # the classic pointing-hand ("link") mouse cursor, the conventional
        # drag/grab affordance, as pixel-faithful polygons from the public-domain
        # Wikimedia cursor (commons "Mouse-cursor-hand-pointer", right glyph). The
        # outer silhouette is filled in the theme foreground and the inner region
        # cut out in the background, so the cursor adapts to light/dark like a
        # symbolic icon. Source bbox is x14..31, y1..23.
        _PAN_OUTLINE = ((19, 1), (21, 1), (21, 2), (22, 2), (22, 6), (24, 6),
                        (24, 7), (27, 7), (27, 8), (29, 8), (29, 9), (30, 9),
                        (30, 10), (31, 10), (31, 17), (30, 17), (30, 20), (29, 20),
                        (29, 23), (19, 23), (19, 20), (18, 20), (18, 18), (17, 18),
                        (17, 16), (16, 16), (16, 14), (15, 14), (15, 13), (14, 13),
                        (14, 10), (17, 10), (17, 11), (18, 11), (18, 2), (19, 2))
        _PAN_INNER = ((21, 2), (21, 11), (22, 11), (22, 7), (24, 7), (24, 11),
                      (25, 11), (25, 8), (27, 8), (27, 12), (28, 12), (28, 9),
                      (29, 9), (29, 10), (30, 10), (30, 17), (29, 17), (29, 20),
                      (28, 20), (28, 22), (20, 22), (20, 20), (19, 20), (19, 18),
                      (18, 18), (18, 16), (17, 16), (17, 14), (16, 14), (16, 13),
                      (15, 13), (15, 11), (17, 11), (17, 12), (18, 12), (18, 13),
                      (19, 13), (19, 2))
        pan_bg = _hex_to_rgb(theme["background"])

        def _draw_mode_pan(_a, ctx, w, h):
            # fit the source bbox (17×22) into the allocation, centred, with a
            # small margin — so the cursor scales with the drawing area and stays
            # centred in the button at any size.
            src_w, src_h = 17.0, 22.0
            m = 0.5 * (w / 16.0)
            sc = min((w - 2 * m) / src_w, (h - 2 * m) / src_h)
            offx = (w - src_w * sc) / 2
            offy = (h - src_h * sc) / 2

            def trace(pts):
                for i, (x, y) in enumerate(pts):
                    X, Y = (x - 14) * sc + offx, (y - 1) * sc + offy
                    ctx.line_to(X, Y) if i else ctx.move_to(X, Y)
                ctx.close_path()

            ctx.set_source_rgb(*fg);     trace(_PAN_OUTLINE); ctx.fill()
            ctx.set_source_rgb(*pan_bg); trace(_PAN_INNER);   ctx.fill()

        def _draw_mode_anchor(_a, ctx, w, h):
            cx, cy = w / 2, h / 2
            r = w / 2 - 3
            ctx.set_source_rgba(*acc, 0.92)
            ctx.arc(cx, cy, r, 0, 2 * math.pi)
            ctx.fill()
            ctx.set_source_rgb(1, 1, 1)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                 cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(9)
            ext = ctx.text_extents("1")
            ctx.move_to(cx - ext.width / 2 - ext.x_bearing,
                        cy - ext.height / 2 - ext.y_bearing)
            ctx.show_text("1")

        def _draw_mode_eraser(_a, ctx, w, h):
            # a tilted eraser block with a band marking the worn rubber tip
            ctx.set_source_rgb(*fg)
            s = w / 16.0
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            ctx.set_line_width(1.3 * s)
            ctx.translate(8 * s, 8.5 * s)
            ctx.rotate(math.radians(-38))
            ctx.translate(-8 * s, -8 * s)
            x, y, bw, bh, r = 3.5 * s, 5.5 * s, 9 * s, 5 * s, 1.3 * s
            ctx.new_sub_path()
            ctx.arc(x + bw - r, y + r, r, -math.pi / 2, 0)
            ctx.arc(x + bw - r, y + bh - r, r, 0, math.pi / 2)
            ctx.arc(x + r, y + bh - r, r, math.pi / 2, math.pi)
            ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
            ctx.close_path()
            ctx.stroke()
            ctx.move_to(x + bw * 0.36, y)
            ctx.line_to(x + bw * 0.36, y + bh)
            ctx.stroke()

        def _draw_mode_lasso(_a, ctx, w, h):
            # a dashed selection loop with a short rope tail — GoodNotes-style
            s = w / 16.0
            ctx.set_source_rgb(*fg)
            ctx.set_line_width(1.3 * s)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            # the loop: a slightly squashed ellipse (build the path under a scale,
            # then dash + stroke outside so the line width stays uniform)
            ctx.save()
            ctx.translate(8 * s, 6.4 * s)
            ctx.scale(1.0, 0.80)
            ctx.new_sub_path()
            ctx.arc(0, 0, 5.2 * s, 0, 2 * math.pi)
            ctx.restore()
            ctx.set_dash([1.7 * s, 1.7 * s])
            ctx.stroke()
            ctx.set_dash([])
            # rope tail curling down from the loop's bottom to a free end
            ctx.move_to(8 * s, 10.5 * s)
            ctx.curve_to(6.9 * s, 12.3 * s, 9.5 * s, 12.9 * s, 8.0 * s, 14.7 * s)
            ctx.stroke()
            # knot where the tail leaves the loop
            ctx.arc(8 * s, 10.5 * s, 1.05 * s, 0, 2 * math.pi)
            ctx.fill()

        def _draw_mode_text(_a, ctx, w, h):
            # an I-beam text cursor: vertical stem with short serifs top/bottom
            s = w / 16.0
            ctx.set_source_rgb(*fg)
            ctx.set_line_width(1.3 * s)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.move_to(5.6 * s, 2.2 * s)
            ctx.line_to(10.4 * s, 2.2 * s)
            ctx.move_to(5.6 * s, 13.8 * s)
            ctx.line_to(10.4 * s, 13.8 * s)
            ctx.move_to(8 * s, 2.2 * s)
            ctx.line_to(8 * s, 13.8 * s)
            ctx.stroke()

        def _glyph(fn, size=16):
            d = Gtk.DrawingArea()
            d.set_content_width(size)
            d.set_content_height(size)
            d.set_halign(Gtk.Align.CENTER)
            d.set_valign(Gtk.Align.CENTER)
            d.set_draw_func(fn)
            return d

        # Each tool button doubles as a discoverability cue: holding the matching
        # modifier (Ctrl=pan, Alt=select, Shift=zoom, Ctrl+Shift=highlighter,
        # Ctrl+Alt=anchor, Ctrl+Shift+Alt=lasso) lights the button up transiently,
        # so the hidden gestures are visible in the UI.
        self._mode_pen = Gtk.ToggleButton()
        self._mode_pen.set_icon_name("document-edit-symbolic")
        self._mode_pen.set_tooltip_text("Pen")
        self._mode_pen.set_active(True)
        self._mode_hl = Gtk.ToggleButton()
        self._mode_hl.set_child(_glyph(_draw_mode_hl))
        self._mode_hl.set_tooltip_text(
            "Highlighter (Ctrl+H · Ctrl+Shift+drag · long-press for free-hand / text)")
        self._mode_hl.set_group(self._mode_pen)
        self._mode_eraser = Gtk.ToggleButton()
        self._mode_eraser.set_child(_glyph(_draw_mode_eraser))
        self._mode_eraser.set_tooltip_text("Eraser (right-drag)")
        self._mode_eraser.set_group(self._mode_pen)
        self._mode_lasso = Gtk.ToggleButton()
        self._mode_lasso.set_child(_glyph(_draw_mode_lasso))
        self._mode_lasso.set_tooltip_text(
            "Lasso ink (Ctrl+Shift+Alt+drag · drag a loop to select, then drag "
            "to move · Delete · change colour to recolour)")
        self._mode_lasso.set_group(self._mode_pen)
        self._mode_select = Gtk.ToggleButton()
        self._mode_select.set_child(_glyph(_draw_mode_sel))
        self._mode_select.set_tooltip_text(
            "Select text (Alt+drag · Ctrl+M · long-press for reading-order / rectangular)")
        self._mode_select.set_group(self._mode_pen)
        # text-first pages swap the PDF select tool for this caret tool: same
        # "select" mode underneath, but visually an I-beam (the page is text)
        self._mode_text = Gtk.ToggleButton()
        self._mode_text.set_child(_glyph(_draw_mode_text))
        self._mode_text.set_tooltip_text(
            "Text cursor — click to place the caret and type "
            "(Alt+drag draws with the pen)")
        self._mode_text.set_group(self._mode_pen)
        self._mode_text.set_visible(False)
        self._mode_pan = Gtk.ToggleButton()
        self._mode_pan.set_child(_glyph(_draw_mode_pan, 20))
        self._mode_pan.set_tooltip_text(
            "Pan (Ctrl+drag · middle-drag · thumb gesture button)")
        self._mode_pan.set_group(self._mode_pen)
        self._mode_zoom = Gtk.ToggleButton()
        self._mode_zoom.set_icon_name(_themed_icon("zoom-in-symbolic"))
        self._mode_zoom.set_tooltip_text("Zoom to region (Shift+drag)")
        self._mode_zoom.set_group(self._mode_pen)
        self._mode_anchor = Gtk.ToggleButton()
        self._mode_anchor.set_child(_glyph(_draw_mode_anchor))
        self._mode_anchor.set_tooltip_text("Anchor / callout (Ctrl+Alt+click/drag)")
        self._mode_anchor.set_group(self._mode_pen)
        for b, m in ((self._mode_pen, "pen"), (self._mode_hl, "highlighter"),
                     (self._mode_eraser, "eraser"), (self._mode_lasso, "lasso"),
                     (self._mode_select, "select"),
                     (self._mode_pan, "pan"), (self._mode_zoom, "zoom"),
                     (self._mode_anchor, "anchor"),
                     (self._mode_text, "select")):
            b.connect("toggled", lambda b, m=m: b.get_active() and self._set_tool_mode(m))

        self._tools_box = tools_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tools_box.add_css_class("linked")
        self._tool_btns = (self._mode_pen, self._mode_hl, self._mode_eraser,
                           self._mode_lasso, self._mode_select, self._mode_pan,
                           self._mode_zoom, self._mode_anchor)
        tools_box.append(self._mode_text)   # leftmost — first tool on a text page
        for b in self._tool_btns:
            tools_box.append(b)

        # ── pen settings popover: width / colour / smoothing (mode is on bar) ──
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popover_box.set_margin_start(16)
        popover_box.set_margin_end(16)
        popover_box.set_margin_top(12)
        popover_box.set_margin_bottom(12)

        # ── tool-mode mirror: only shown when the bar collapses the segmented
        # switch off the header; kept in sync with the header toggles ──────────
        self._pen_modes_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mode_label = Gtk.Label(label="Tool", xalign=0)
        mode_label.add_css_class("dim-label")
        self._pen_modes_section.append(mode_label)

        self._pmode_pen = Gtk.ToggleButton()
        self._pmode_pen.set_icon_name("document-edit-symbolic")
        self._pmode_pen.set_tooltip_text("Pen")
        self._pmode_pen.set_active(True)
        self._pmode_hl = Gtk.ToggleButton()
        self._pmode_hl.set_child(_glyph(_draw_mode_hl))
        self._pmode_hl.set_tooltip_text(
            "Highlighter (Ctrl+H · Ctrl+Shift+drag · long-press for free-hand / text)")
        self._pmode_hl.set_group(self._pmode_pen)
        self._pmode_eraser = Gtk.ToggleButton()
        self._pmode_eraser.set_child(_glyph(_draw_mode_eraser))
        self._pmode_eraser.set_tooltip_text("Eraser (right-drag)")
        self._pmode_eraser.set_group(self._pmode_pen)
        self._pmode_lasso = Gtk.ToggleButton()
        self._pmode_lasso.set_child(_glyph(_draw_mode_lasso))
        self._pmode_lasso.set_tooltip_text(
            "Lasso ink (Ctrl+Shift+Alt+drag · drag a loop to select, then drag "
            "to move · Delete · change colour to recolour)")
        self._pmode_lasso.set_group(self._pmode_pen)
        self._pmode_select = Gtk.ToggleButton()
        self._pmode_select.set_child(_glyph(_draw_mode_sel))
        self._pmode_select.set_tooltip_text(
            "Select text (Alt+drag · Ctrl+M · long-press for reading-order / rectangular)")
        self._pmode_select.set_group(self._pmode_pen)
        self._pmode_text = Gtk.ToggleButton()
        self._pmode_text.set_child(_glyph(_draw_mode_text))
        self._pmode_text.set_tooltip_text(
            "Text cursor — click to place the caret and type "
            "(Alt+drag draws with the pen)")
        self._pmode_text.set_group(self._pmode_pen)
        self._pmode_text.set_visible(False)
        self._pmode_pan = Gtk.ToggleButton()
        self._pmode_pan.set_child(_glyph(_draw_mode_pan, 20))
        self._pmode_pan.set_tooltip_text(
            "Pan (Ctrl+drag · middle-drag · thumb gesture button)")
        self._pmode_pan.set_group(self._pmode_pen)
        self._pmode_zoom = Gtk.ToggleButton()
        self._pmode_zoom.set_icon_name(_themed_icon("zoom-in-symbolic"))
        self._pmode_zoom.set_tooltip_text("Zoom to region (Shift+drag)")
        self._pmode_zoom.set_group(self._pmode_pen)
        self._pmode_anchor = Gtk.ToggleButton()
        self._pmode_anchor.set_child(_glyph(_draw_mode_anchor))
        self._pmode_anchor.set_tooltip_text("Anchor / callout (Ctrl+Alt+click/drag)")
        self._pmode_anchor.set_group(self._pmode_pen)
        for b, m in ((self._pmode_pen, "pen"), (self._pmode_hl, "highlighter"),
                     (self._pmode_eraser, "eraser"), (self._pmode_lasso, "lasso"),
                     (self._pmode_select, "select"),
                     (self._pmode_pan, "pan"), (self._pmode_zoom, "zoom"),
                     (self._pmode_anchor, "anchor"),
                     (self._pmode_text, "select")):
            b.connect("toggled", lambda b, m=m: b.get_active() and self._set_tool_mode(m))
        pmode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        pmode_box.add_css_class("linked")
        self._ptool_btns = (self._pmode_pen, self._pmode_hl, self._pmode_eraser,
                            self._pmode_lasso, self._pmode_select, self._pmode_pan,
                            self._pmode_zoom, self._pmode_anchor)
        pmode_box.append(self._pmode_text)
        for b in self._ptool_btns:
            pmode_box.append(b)
        self._pen_modes_section.append(pmode_box)
        self._pen_modes_section.set_visible(False)
        popover_box.append(self._pen_modes_section)

        # long-press either select button → choose reading-order vs rectangular
        self._select_style_radios = []
        self._attach_select_style_menu(self._mode_select)
        self._attach_select_style_menu(self._pmode_select)
        # long-press either highlighter button → free-hand vs text marking
        self._highlight_style_radios = []
        self._attach_highlight_style_menu(self._mode_hl)
        self._attach_highlight_style_menu(self._pmode_hl)

        width_label = Gtk.Label(label="Width", xalign=0)
        width_label.add_css_class("dim-label")
        popover_box.append(width_label)

        self._width_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 5.0, 0.1)
        self._width_scale.set_value(2.0)
        self._width_scale.set_draw_value(True)
        self._width_scale.set_size_request(200, -1)
        self._width_scale.connect("value-changed", self._on_width_changed)
        popover_box.append(self._width_scale)

        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_row.set_margin_top(4)
        color_label = Gtk.Label(label="Color", xalign=0, hexpand=True)
        color_label.add_css_class("dim-label")
        color_dialog = Gtk.ColorDialog.new()
        color_dialog.set_with_alpha(True)
        self._color_btn = Gtk.ColorDialogButton.new(color_dialog)
        init_rgba = Gdk.RGBA()
        init_rgba.red, init_rgba.green, init_rgba.blue, init_rgba.alpha = *acc, 1.0
        self._color_btn.set_rgba(init_rgba)
        self._color_btn.connect("notify::rgba", self._on_color_changed)
        self.canvas.pen_color = acc
        color_row.append(color_label)
        color_row.append(self._color_btn)
        popover_box.append(color_row)

        swatches_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        swatches_box.set_margin_top(2)
        for i, (name, rgb) in enumerate(self._swatch_presets):
            swatch = Gtk.Button()
            swatch.set_tooltip_text(name)
            swatch.add_css_class("pen-swatch")
            swatch.add_css_class(f"pen-swatch-{i}")

            def _make_handler(r, g, b):
                def _on_click(_btn):
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue, rgba.alpha = r, g, b, 1.0
                    # routes through _on_color_changed → active tool
                    self._color_btn.set_rgba(rgba)
                return _on_click

            swatch.connect("clicked", _make_handler(*rgb))
            swatches_box.append(swatch)
        popover_box.append(swatches_box)

        smooth_label = Gtk.Label(label="Smoothing", xalign=0)
        smooth_label.add_css_class("dim-label")
        smooth_label.set_margin_top(6)
        popover_box.append(smooth_label)

        self._smooth_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self._smooth_scale.set_value(self.canvas.smoothing * 100)
        self._smooth_scale.set_draw_value(True)
        self._smooth_scale.set_size_request(200, -1)
        self._smooth_scale.set_tooltip_text(
            "How much freehand strokes are smoothed when you lift the pen")
        self._smooth_scale.connect("value-changed", self._on_smoothing_changed)
        popover_box.append(self._smooth_scale)

        popover = Gtk.Popover()
        popover.set_child(popover_box)

        # the settings button shows the active tool's colour as a swatch
        self._color_swatch = Gtk.DrawingArea()
        self._color_swatch.set_content_width(18)
        self._color_swatch.set_content_height(18)

        def _draw_swatch(_a, ctx, w, h):
            color = self.canvas.hl_color if self.canvas.highlighter else self.canvas.pen_color
            ctx.set_source_rgb(*color)
            ctx.arc(w / 2, h / 2, min(w, h) / 2 - 2, 0, 6.2832)
            ctx.fill()
        self._color_swatch.set_draw_func(_draw_swatch)

        self._pen_btn = Gtk.MenuButton()
        self._pen_btn.set_child(self._color_swatch)
        self._pen_btn.set_tooltip_text("Pen settings (colour, width, smoothing)")
        self._pen_btn.set_popover(popover)

        # ── deck cluster: slide + object tools, shown only in deck mode ───────
        # (routes to the active session's DeckView; see _MODE_CHROME)
        self._deck_bar = self._build_deck_bar()

        # ── undo / redo ────────────────────────────────────────────────────────
        self._undo_btn = Gtk.Button()
        self._undo_btn.set_icon_name("edit-undo-symbolic")
        self._undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        self._undo_btn.connect("clicked", lambda _: self._global_undo())

        self._redo_btn = Gtk.Button()
        self._redo_btn.set_icon_name("edit-redo-symbolic")
        self._redo_btn.set_tooltip_text("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        self._redo_btn.connect("clicked", lambda _: self._global_redo())

        # ── right side: search + notes panel ───────────────────────────────────
        self._search_btn = search_btn = Gtk.Button()
        search_btn.set_icon_name("edit-find-symbolic")
        search_btn.set_tooltip_text("Search PDF & notes (Ctrl+F)")
        search_btn.connect("clicked", lambda _: self._show_search())

        self._present_btn = Gtk.ToggleButton()
        self._present_btn.set_icon_name(
            _themed_icon("video-display-symbolic", "display-symbolic"))
        self._present_btn.set_tooltip_text(
            "Presenter view — mirror the page on a second screen (F5)")
        self._present_btn.connect("toggled", self._on_present_toggled)

        # Sibling of the presenter button: mirror the page to a phone instead of
        # a second screen — a live view the audience can follow, QR to connect.
        self._share_btn = Gtk.Button()
        self._share_btn.set_icon_name(
            _themed_icon("qr-code-symbolic", "phone-symbolic"))
        self._share_btn.set_tooltip_text(
            "Share to phone — live view + QR code")
        self._share_btn.connect("clicked", lambda _: self._on_share_to_phone())

        self._notes_toggle = Gtk.ToggleButton()
        self._notes_toggle.set_icon_name(
            _themed_icon("view-sidebar-symbolic", "sidebar-show-symbolic"))
        self._notes_toggle.set_tooltip_text("Toggle notes (Ctrl+\\)")
        self._notes_toggle.set_active(True)
        self._notes_toggled_id = self._notes_toggle.connect(
            "toggled", self._on_notes_toggled)

        # ── assemble: two cluster boxes so the bar's real content width can be
        # measured directly (the HeaderBar's own natural width is inflated by the
        # symmetric space it reserves to centre the — here empty — title) ───────
        self._undo_sep = vsep()
        self._header_start = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for w in (menu_btn, vsep(), self._toc_btn, nav_box, pages_box, vsep(),
                  tools_box, self._pen_btn, self._deck_bar, self._undo_sep,
                  self._undo_btn, self._redo_btn):
            self._header_start.append(w)

        self._header_end = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._header_end.append(search_btn)
        self._header_end.append(self._present_btn)
        self._header_end.append(self._share_btn)
        self._header_end.append(self._notes_toggle)

        # Icon buttons don't compress, so the cluster's *minimum* width equals
        # its natural width — which would become the whole window's minimum and
        # stop it ever getting narrow enough to collapse. Wrapping it in a
        # non-scrolling ScrolledWindow makes the reported minimum ~0 (it could
        # scroll) while still asking for the natural width when there's room, so
        # the window can shrink and our resize hook collapses before any
        # scrolling is ever needed.
        start_scroll = Gtk.ScrolledWindow()
        start_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        start_scroll.set_propagate_natural_width(True)
        start_scroll.set_child(self._header_start)
        header.pack_start(start_scroll)
        header.pack_end(self._header_end)

        self._set_tool_mode("pen")

        # responsive collapse: each session's canvas fires ::resize on every
        # window resize (the HeaderBar does not) and is hooked in
        # _build_document_widgets; re-check when the header maps too.
        header.connect("map", lambda *_: self._update_header_collapse())

        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

        # ── tabs ───────────────────────────────────────────────────────────────
        # Each open document is a page in an Adw.TabView; a real Adw.TabBar (so
        # native reorder / cross-window drag / tear-off come for free) sits as a
        # second top bar BELOW the header. set_autohide(True) hides it entirely
        # for a single document — so one PDF costs no vertical space and the PDF
        # never moves down — and reveals a full-width, usable strip only once a
        # second tab exists. (Putting it inline in the header title slot left the
        # tabs clipped to ~half their width because the toolbar is button-dense.)
        self._tab_view = Adw.TabView()
        self._add_session_tab(self._active_session)
        self._tab_view.connect("notify::selected-page", self._on_tab_switched)
        self._tab_view.connect("close-page", self._on_tab_close)
        self._tab_view.connect("page-detached", self._on_page_detached)
        # tab dragged out to empty space / onto another window
        self._tab_view.connect("create-window", self._on_tab_create_window)
        self._tab_view.connect("page-attached", self._on_page_attached)

        self._tab_bar = Adw.TabBar()
        self._tab_bar.set_view(self._tab_view)
        self._tab_bar.set_autohide(True)

        # An overlay over the whole tab area floats the presentation control bar
        # (timer + large prev/next) at the bottom, above whichever tab is active.
        self._present_overlay = Gtk.Overlay()
        self._present_overlay.set_child(self._tab_view)
        self._build_present_bar()

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self._present_overlay)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(self._tab_bar)
        toolbar_view.set_content(self.toast_overlay)
        self.set_content(toolbar_view)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        # drag a file from the file manager onto the window to open it.
        # Use the *async* target: Wayland file managers transfer files through
        # the desktop portal (application/vnd.portal.filetransfer), which the
        # synchronous Gtk.DropTarget can't read inline at drop time, so its
        # "drop" never fires. DropTargetAsync reads the value asynchronously.
        drop = Gtk.DropTargetAsync.new(
            Gdk.ContentFormats.new_for_gtype(Gdk.FileList), Gdk.DragAction.COPY)
        drop.connect("accept", self._on_drop_accept)
        # drag-enter / drag-motion MUST return the action, or the negotiated
        # action stays none and the compositor rejects the drop on release.
        drop.connect("drag-enter", self._on_drop_motion)
        drop.connect("drag-motion", self._on_drop_motion)
        drop.connect("drop", self._on_drop_async)
        self.add_controller(drop)

        # Ctrl+Z must work globally; the notes TextView consumes it before the
        # bubble-phase controller above, so intercept it in the capture phase.
        undo_ctrl = Gtk.EventControllerKey()
        undo_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        undo_ctrl.connect("key-pressed", self._on_undo_key)
        self.add_controller(undo_ctrl)

        # PDF-level shortcuts (page flip, panel toggle, close) must work no matter
        # what has focus. The notes TextView otherwise swallows some before the
        # bubble handler: PageUp/PageDown scroll its text, Ctrl+\ deselects. A
        # capture-phase controller intercepts just those, before the focus widget.
        global_ctrl = Gtk.EventControllerKey()
        global_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        global_ctrl.connect("key-pressed", self._on_global_key)
        self.add_controller(global_ctrl)

        # The canvas key controller only tracks held modifiers while the canvas
        # is focused, so the tool-button highlight died once the notes editor (or
        # any other widget) took focus. A window-wide capture-phase controller
        # keeps the modifier state — and the highlight — live everywhere. It only
        # reads modifier keys and never consumes the event, so typing is intact.
        mod_ctrl = Gtk.EventControllerKey()
        mod_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        mod_ctrl.connect("key-pressed",
                         lambda c, kv, kc, st: self.canvas._on_modifier_key(c, kv, kc, st, True))
        mod_ctrl.connect("key-released",
                         lambda c, kv, kc, st: self.canvas._on_modifier_key(c, kv, kc, st, False))
        self.add_controller(mod_ctrl)

        # Mouse side buttons (back/forward, 8/9) flip pages from anywhere in the
        # window — including while typing in the notes editor. Extra-button
        # press/release isn't reliably reported through the gesture APIs (see the
        # canvas thumb-button note), so a capture-phase legacy controller is used.
        nav_ctrl = Gtk.EventControllerLegacy()
        nav_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        nav_ctrl.connect("event", self._on_window_button)
        self.add_controller(nav_ctrl)

    def _on_window_button(self, ctrl, event):
        if event is None:   # PyGObject occasionally fails to marshal the arg
            event = ctrl.get_current_event()
        if event is None or event.get_event_type() != Gdk.EventType.BUTTON_PRESS:
            return False
        btn = event.get_button()
        if btn in (8, 9):
            self._nav_page(1 if btn == 8 else -1)
            return True
        return False

    def _build_document_widgets(self, s):
        """Build one document's canvas / notes editor / search bar / sidebar
        subtree into the given DocumentSession. Every signal is routed through
        `s.win` (not `self`) so that when a tab is torn off into another window,
        re-pointing s.win retargets the whole document — no disconnecting needed.
        Writes per-document widgets onto `s`, never onto self. Called once/tab."""
        s.win = self
        s.canvas = PDFCanvas()
        s.canvas.surround_color = self._theme_surround
        s.canvas.zoom_accent = self._theme_acc
        s.canvas.set_vexpand(True)
        s.canvas.set_hexpand(True)
        s.canvas.on_page_changed = lambda *a: s.win._on_page_changed(*a)
        s.canvas.on_change = lambda *a: s.win._mark_dirty(*a)
        s.canvas.on_text_copied = lambda *a: s.win._on_text_copied(*a)
        s.canvas.on_nav_button = lambda d: s.win._nav_page(d)
        # commit the current note before any canvas-initiated page change
        # (scroll flip, link jump, undo on another page)
        s.canvas.on_page_will_change = lambda *a: s.win._commit_note()
        s.canvas.on_anchor_placed = lambda *a: s.win._on_anchor_placed(*a)
        s.canvas.on_anchor_clicked = lambda *a: s.win._on_anchor_clicked(*a)
        s.canvas.on_anchor_moved = lambda *a: s.win._on_anchor_moved(*a)
        s.canvas.on_callout_placed = lambda *a: s.win._on_callout_placed(*a)
        s.canvas.on_callout_moved = lambda *a: s.win._on_callout_moved(*a)
        s.canvas.on_textbox_placed = lambda *a: s.win._on_textbox_placed(*a)
        s.canvas.on_textbox_moved = lambda *a: s.win._on_textbox_moved(*a)
        s.canvas.on_user_action = lambda *a: s.win._on_canvas_action(*a)
        s.canvas.on_canvas_press = lambda *a: s.win._clear_thumb_selection(*a)
        s.canvas.on_nav_history = lambda *a: s.win._on_nav_history(*a)
        s.canvas.on_modifier_tool = lambda *a: s.win._highlight_transient_tool(*a)
        # responsive collapse tick (the HeaderBar itself never fires ::resize)
        s.canvas.connect("resize", lambda *_: s.win._update_header_collapse())

        # ── notes panel ───────────────────────────────────────────────────────
        s._notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        notes_header = Gtk.Label(label="Notes")
        notes_header.add_css_class("dim-label")
        notes_header.set_xalign(0)
        notes_header.set_margin_start(10)
        notes_header.set_margin_top(6)
        notes_header.set_margin_bottom(4)
        s._notes_box.append(notes_header)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_vexpand(True)
        notes_scroll.set_hexpand(True)
        s._notes_view = MarkdownNotesView(self._src_scheme)
        # remembered so text-first mode can swap _notes_view to the page's
        # editor and back — every window method follows _notes_view
        s._panel_notes_view = s._notes_view
        s._notes_view.font_zoom_cb = lambda d: s.win._change_notes_font(d)
        s._notes_view.get_buffer().connect("changed", lambda *a: s.win._on_notes_changed(*a))
        s._notes_view.get_buffer().connect("notify::cursor-position", lambda *a: s.win._on_notes_cursor_moved(*a))
        notes_scroll.set_child(s._notes_view)
        s._notes_box.append(notes_scroll)

        # ── search bar ────────────────────────────────────────────────────────
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.set_margin_start(6)
        search_box.set_margin_end(6)
        search_box.set_margin_top(4)
        search_box.set_margin_bottom(4)
        s._search_entry = Gtk.SearchEntry()
        s._search_entry.set_hexpand(True)
        s._search_entry.set_placeholder_text("Search PDF & notes…")
        s._search_entry.connect("search-changed", lambda *a: s.win._on_search_changed(*a))
        s._search_entry.connect("stop-search", lambda _: s.win._hide_search())
        s._search_entry.connect("activate", lambda _: s.win._search_next())
        search_key = Gtk.EventControllerKey()
        search_key.connect("key-pressed", lambda *a: s.win._on_search_key(*a))
        s._search_entry.add_controller(search_key)
        search_prev_btn = Gtk.Button()
        search_prev_btn.set_icon_name("go-up-symbolic")
        search_prev_btn.set_tooltip_text("Previous match")
        search_prev_btn.connect("clicked", lambda _: s.win._search_prev())
        search_next_btn = Gtk.Button()
        search_next_btn.set_icon_name("go-down-symbolic")
        search_next_btn.set_tooltip_text("Next match")
        search_next_btn.connect("clicked", lambda _: s.win._search_next())
        s._search_label = Gtk.Label(label="")
        s._search_label.add_css_class("dim-label")
        s._search_label.set_width_chars(7)
        search_close_btn = Gtk.Button()
        search_close_btn.set_icon_name("window-close-symbolic")
        search_close_btn.connect("clicked", lambda _: s.win._hide_search())
        search_box.append(s._search_entry)
        search_box.append(search_prev_btn)
        search_box.append(search_next_btn)
        search_box.append(s._search_label)
        search_box.append(search_close_btn)
        s._search_revealer = Gtk.Revealer()
        s._search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        s._search_revealer.set_child(search_box)
        s._search_revealer.set_reveal_child(False)

        canvas_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        canvas_box.append(s._search_revealer)
        canvas_box.append(s.canvas)

        # ── split pane ────────────────────────────────────────────────────────
        s._saved_pane_pos = 800
        s._pane_anim = None   # running notes show/hide animation
        s._canvas_box = canvas_box   # deck mode swaps the deck view in here
        s._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        s._paned.set_start_child(canvas_box)
        s._paned.set_resize_start_child(True)
        s._paned.set_shrink_start_child(False)
        s._paned.set_end_child(s._notes_box)
        s._paned.set_resize_end_child(True)
        s._paned.set_shrink_end_child(True)
        s._paned.set_hexpand(True)

        # ── outline (TOC) sidebar ─────────────────────────────────────────────
        s._toc_list = Gtk.ListBox()
        s._toc_list.set_selection_mode(Gtk.SelectionMode.NONE)
        s._toc_list.connect("row-activated", lambda *a: s.win._on_toc_row_activated(*a))
        # clicking the empty area below the thumbnails clears the export selection
        toc_click = Gtk.GestureClick()
        toc_click.connect("pressed", lambda *a: s.win._on_toc_list_pressed(*a))
        s._toc_list.add_controller(toc_click)
        s._toc_scroll = Gtk.ScrolledWindow()
        s._toc_scroll.set_child(s._toc_list)
        s._toc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        s._toc_scroll.set_size_request(230, -1)
        s._toc_scroll.set_vexpand(True)
        # Outline ⇄ Pages view switcher, shown only when the PDF has a TOC
        s._toc_seg_outline = Gtk.ToggleButton(label="Outline")
        s._toc_seg_outline.set_active(True)
        s._toc_seg_outline.set_tooltip_text(
            "Show the document outline (Ctrl+T toggles this sidebar)")
        s._toc_seg_pages = Gtk.ToggleButton(label="Pages")
        s._toc_seg_pages.set_tooltip_text(
            "Show page thumbnails (Ctrl+T toggles this sidebar)")
        s._toc_seg_pages.set_group(s._toc_seg_outline)
        s._toc_seg_pages.connect("toggled", lambda *a: s.win._on_toc_view_toggled(*a))
        s._toc_switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        s._toc_switch.add_css_class("linked")
        s._toc_switch.set_homogeneous(True)
        s._toc_switch.set_margin_top(8)
        s._toc_switch.set_margin_bottom(4)
        s._toc_switch.set_margin_start(8)
        s._toc_switch.set_margin_end(8)
        s._toc_switch.append(s._toc_seg_outline)
        s._toc_switch.append(s._toc_seg_pages)
        s._toc_switch.set_visible(False)
        toc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toc_box.append(s._toc_switch)
        toc_box.append(s._toc_scroll)
        s._toc_revealer = Gtk.Revealer()
        s._toc_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        s._toc_revealer.set_child(toc_box)
        s._toc_revealer.set_reveal_child(False)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(s._toc_revealer)
        content.append(s._paned)
        s.content = content
        return s

    # ── text-first mode ───────────────────────────────────────────────────────

    def _ensure_text_page(self, s):
        """Lazily build the session's text-first page (sheet + ink overlay)."""
        if s._text_page is not None:
            return s._text_page
        tp = TextPageView(font_px=self._notes_font_px)
        # ink strokes use the same shared pen/highlighter settings as the canvas
        tp.pen_style = lambda hl, s=s: (
            (s.canvas.hl_color, s.canvas.hl_width, s.canvas.hl_opacity) if hl
            else (s.canvas.pen_color, s.canvas.pen_width, 1.0))
        # Ctrl+scroll / Ctrl+= on the sheet zooms the whole paper (text, ink
        # and margins together), not the persistent notes-font setting
        tp.view.font_zoom_cb = lambda d: tp.zoom_step(d)
        tp.view.get_buffer().connect(
            "changed", lambda *a: s.win._on_notes_changed(*a))
        tp.on_ink_action = lambda: s.win._on_ink_action()
        tp.on_ink_changed = lambda: s.win._mark_dirty()
        tp.set_visible(False)
        s.content.append(tp)
        s._text_page = tp
        return tp

    def _enter_text_mode(self, s=None):
        """Switch a session's UI to the text-first page: no sidebars, the sheet
        is the document. _notes_view is repointed at the sheet's editor, so
        search, undo, font zoom and note commits keep working unchanged."""
        s = s or self._active_session
        self._leave_deck_mode(s)
        tp = self._ensure_text_page(s)
        s._text_mode = True
        s._notes_view = tp.view
        s._paned.set_visible(False)
        s._toc_revealer.set_reveal_child(False)
        tp.set_visible(True)
        if s is self._active_session:
            self._update_header_for_mode()
            self._set_tool_mode("select")   # text editing first; pen on demand
            tp.view.grab_focus()

    def _leave_text_mode(self, s=None):
        """Back to the PDF layout (a PDF got opened into this tab)."""
        s = s or self._active_session
        if not s._text_mode:
            return
        s._text_mode = False
        s._notes_view = s._panel_notes_view
        if s._text_page is not None:
            s._text_page.set_visible(False)
        s._paned.set_visible(True)
        if s is self._active_session:
            self._update_header_for_mode()

    # ── deck (presentation) mode ─────────────────────────────────────────────

    # keep in sync with deck.LAYOUT_LABELS (duplicated here so the header can
    # be built without importing the deck module)
    _DECK_LAYOUTS = (("title", "Title slide"),
                     ("content", "Heading + text"),
                     ("blank", "Blank"))

    def _build_deck_bar(self):
        """The deck mode's extra header tools: slide ops, object insertion and
        textbox styling. One cluster per window — it always drives the active
        session's DeckView."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._syncing_deck_cluster = False

        new_btn = Gtk.MenuButton()
        new_btn.set_icon_name("list-add-symbolic")
        new_btn.set_tooltip_text("New slide")
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for key, label in self._DECK_LAYOUTS:
            b = Gtk.Button()
            b.add_css_class("flat")
            b.set_child(Gtk.Label(label=label, xalign=0))
            b.connect("clicked", lambda _b, k=key: (
                pop.popdown(), self._deck_view.add_slide(k)))
            box.append(b)
        pop.set_child(box)
        new_btn.set_popover(pop)
        bar.append(new_btn)

        del_btn = Gtk.Button()
        del_btn.set_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text("Delete slide")
        del_btn.connect("clicked", lambda _b: self._deck_view.delete_slide())
        bar.append(del_btn)
        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        bar.append(sep1)

        text_btn = Gtk.Button()
        text_btn.set_icon_name("insert-text-symbolic")
        text_btn.set_tooltip_text("Add text box")
        text_btn.connect("clicked", lambda _b: self._deck_view.add_textbox())
        bar.append(text_btn)
        img_btn = Gtk.Button()
        img_btn.set_icon_name("insert-image-symbolic")
        img_btn.set_tooltip_text("Add image… (or paste / drop one)")
        img_btn.connect("clicked", lambda _b: self._deck_view.pick_image())
        bar.append(img_btn)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # style controls apply to the selected textbox
        self._deck_size_spin = Gtk.SpinButton.new_with_range(8, 200, 2)
        self._deck_size_spin.set_tooltip_text("Font size")
        self._deck_size_spin.connect("value-changed", self._on_deck_style_changed)
        bar.append(self._deck_size_spin)
        self._deck_bold_btn = Gtk.ToggleButton()
        self._deck_bold_btn.set_icon_name("format-text-bold-symbolic")
        self._deck_bold_btn.set_tooltip_text("Bold")
        self._deck_bold_btn.connect("toggled", self._on_deck_style_changed)
        bar.append(self._deck_bold_btn)
        self._deck_align_btns = {}
        group = None
        for align, icon in (("left", "format-justify-left-symbolic"),
                            ("center", "format-justify-center-symbolic"),
                            ("right", "format-justify-right-symbolic")):
            b = Gtk.ToggleButton()
            b.set_icon_name(icon)
            b.set_tooltip_text(f"Align {align}")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", self._on_deck_style_changed)
            self._deck_align_btns[align] = b
            bar.append(b)

        self._deck_slide_label = Gtk.Label(label="1 / 1")
        self._deck_slide_label.add_css_class("dim-label")
        self._deck_slide_label.set_margin_start(6)
        bar.append(self._deck_slide_label)
        bar.set_visible(False)   # _update_header_for_mode shows it in deck mode
        return bar

    def _on_deck_style_changed(self, _w):
        if (self._syncing_deck_cluster or not self._deck_mode
                or self._deck_view is None):
            return
        align = next((a for a, b in self._deck_align_btns.items()
                      if b.get_active()), None)
        self._deck_view.apply_style(size=self._deck_size_spin.get_value(),
                                    bold=self._deck_bold_btn.get_active(),
                                    align=align)

    def _sync_deck_cluster(self, s):
        """Reflect the deck selection's style in the cluster (no feedback)."""
        if s is not self._active_session or s._deck_view is None:
            return
        self._syncing_deck_cluster = True
        is_text, size, bold, align = s._deck_view.selection_style()
        for w in (self._deck_size_spin, self._deck_bold_btn,
                  *self._deck_align_btns.values()):
            w.set_sensitive(is_text)
        if is_text:
            self._deck_size_spin.set_value(size)
            self._deck_bold_btn.set_active(bold)
            self._deck_align_btns.get(
                align, self._deck_align_btns["left"]).set_active(True)
        self._syncing_deck_cluster = False
        dv = s._deck_view
        self._deck_slide_label.set_label(
            f"{dv.current + 1} / {len(dv.model.slides)}")

    def _ensure_deck_view(self, s):
        """Lazily build the session's Deck editor (the slide canvas)."""
        if s._deck_view is not None:
            return s._deck_view
        dv = _deck_module().DeckView()
        dv.on_changed = lambda: s.win._on_deck_changed(s)
        # deck ink strokes use the same shared pen/highlighter settings as the
        # PDF canvas and the text page
        dv.pen_style = lambda hl, s=s: (
            (s.canvas.hl_color, s.canvas.hl_width, s.canvas.hl_opacity) if hl
            else (s.canvas.pen_color, s.canvas.pen_width, 1.0))
        dv.on_slide_switched = lambda: s.win._on_deck_slide_switched(s)
        dv.on_before_slide_switch = lambda: s.win._commit_note_for(s)
        dv.on_slides_changed = lambda: s.win._on_deck_slides_changed(s)
        dv.on_selection_changed = lambda: s.win._sync_deck_cluster(s)
        dv.set_visible(False)
        # into the paned's canvas side, so the notes panel (speaker notes)
        # keeps working beside the slides exactly like beside a PDF page
        s._canvas_box.append(dv)
        s._deck_view = dv
        return dv

    def _on_deck_changed(self, s):
        """Any deck mutation: dirty tracking + refresh the slide thumbnails
        (in-place redraw; structural changes go through _on_deck_slides_changed)."""
        self._mark_dirty()
        if (s is self._active_session and s._deck_mode
                and self._toc_revealer.get_reveal_child()):
            self._refresh_thumb_images()

    def _on_deck_slides_changed(self, s):
        """Slide count or order changed: rebuild the sidebar rows and the
        slide counter."""
        if s is not self._active_session:
            return
        if self._toc_revealer.get_reveal_child():
            self._populate_toc()
        self._sync_deck_cluster(s)

    def _on_deck_slide_switched(self, s):
        """The deck moved to another slide: restore its speaker notes and keep
        the sidebar marker and the presenter in step (mirrors a PDF page flip)."""
        if s is not self._active_session:
            return
        self._restore_note()
        self._sync_deck_cluster(s)
        if self._toc_revealer.get_reveal_child():
            self._select_thumb(self._deck_view.current)
        if s._presenter is not None:
            s._presenter.sync_page()

    def _enter_deck_mode(self, s=None):
        """Switch a session's UI to the Deck editor: same window chrome, the
        deck's set of tools (see _MODE_CHROME). The notes panel stays — it
        edits the current slide's speaker notes."""
        s = s or self._active_session
        self._leave_text_mode(s)
        dv = self._ensure_deck_view(s)
        s._deck_mode = True
        s._paned.set_visible(True)
        s.canvas.set_visible(False)   # the deck view takes the canvas side
        s._toc_revealer.set_reveal_child(False)
        dv.set_visible(True)
        if s is self._active_session:
            self._update_header_for_mode()
            self._set_tool_mode("select")
            self._sync_deck_cluster(s)
            dv.canvas.grab_focus()

    def _leave_deck_mode(self, s=None):
        """Back to the PDF layout (another document got opened into this tab)."""
        s = s or self._active_session
        if not s._deck_mode:
            return
        s._deck_mode = False
        s._deck_path = None
        if s._deck_view is not None:
            s._deck_view.set_visible(False)
        s.canvas.set_visible(True)
        s._paned.set_visible(True)
        if s is self._active_session:
            self._update_header_for_mode()

    def _update_header_for_mode(self):
        """Hide the PDF-only chrome while a text-first page is shown. Pen,
        highlighter and eraser stay; the PDF select tool swaps for the caret
        tool (leftmost, so 'just type' reads as the default)."""
        mode = (self._active_session.doc_mode if self._active_session
                else "pdf")
        for name, modes in self._MODE_CHROME.items():
            vis = mode in modes
            getattr(self, name).set_visible(vis)
            if name.startswith("_mode_"):
                # each tool button has a twin inside the pen popover (shown
                # when the bar collapses) — keep it in step
                twin = getattr(self, "_pmode_" + name[len("_mode_"):], None)
                if twin is not None:
                    twin.set_visible(vis)
        # leaving a text page with the caret active: hand it to the select
        # button (same mode underneath, different face)
        if mode != "text" and self._mode_text.get_active():
            self._set_tool_mode("select")
        # the ☰ menu shows only the active mode's actions
        for m, items in (("pdf", self._pdf_menu_items),
                         ("text", self._text_menu_items),
                         ("deck", self._deck_menu_items)):
            for item in items:
                item.set_visible(mode == m)
        # cluster widths changed: re-measure the collapse levels and force the
        # current level to re-apply (it also gates presenter/share visibility)
        self._collapse_natural = None
        self._collapse_level = -1
        self._update_header_collapse()

    def _on_ink_action(self):
        """An ink gesture on the text page finished: chronological undo entry."""
        self._notes_burst_open = False
        self._burst_base = self._notes_view.get_source_text()
        self._undo_timeline.append(("ink",))
        self._redo_timeline.clear()

    # ── shortcuts popover ─────────────────────────────────────────────────────

    def _build_shortcuts_content(self):
        shortcuts = [
            ("Draw",          None),
            ("Left-drag",     "Draw stroke"),
            ("Right-drag",    "Erase stroke"),
            ("Ctrl+H",        "Toggle highlighter"),
            ("Ctrl+Z",        "Undo last action (draw, erase, typing)"),
            ("Ctrl+Y",        "Redo (also Ctrl+Shift+Z)"),
            ("Text",          None),
            ("Ctrl+M",        "Toggle draw / select-text mode"),
            ("Alt+Drag",      "Select text (word-level)"),
            ("Left-drag",     "Select text (in select-text mode)"),
            ("Ctrl+C",        "Copy selected text"),
            ("Alt+Click",     "Follow link under cursor (footnote, citation, URL)"),
            ("Alt+Left",      "Back to where you were before following a link"),
            ("Ctrl+Alt+Click","Place anchor marker in notes"),
            ("Ctrl+Alt+Drag","Place anchor + callout box at drag end"),
            ("Ctrl+Alt+Right-click","Place a standalone text box on the page"),
            ("Ctrl+T",       "Toggle outline / page-thumbnail sidebar"),
            ("Navigate",      None),
            ("PageDown",      "Next page"),
            ("PageUp",        "Previous page"),
            ("Ctrl+Shift+N",  "Add blank page after current"),
            ("Ctrl+Shift+Del","Delete current page"),
            ("Zoom & Pan",    None),
            ("Ctrl+Scroll",   "Zoom in / out"),
            ("Scroll",        "Pan"),
            ("Ctrl+Drag",     "Pan"),
            ("Shift+Drag",    "Zoom to region"),
            ("Shift+Click",   "Fit page"),
            ("File",          None),
            ("Ctrl+O",        "Open file…"),
            ("Ctrl+N",        "New blank PDF"),
            ("Ctrl+Alt+N",    "New text page (write and draw on endless paper)"),
            ("Ctrl+Alt+P",    "New presentation (16:9 slides, saved as .smdeck)"),
            ("Text page",     None),
            ("Alt+Drag",      "Draw with the pen while the text tool is active"),
            ("Ctrl+Scroll",   "Zoom the sheet (paper, text and ink together)"),
            ("Ctrl+0",        "Reset the sheet zoom"),
            ("Ctrl+Drag",     "Pan the sheet (also middle-drag, thumb button)"),
            ("Ctrl+F",        "Search text in PDF"),
            ("Ctrl+S",        "Save"),
            ("Ctrl+Shift+S",  "Save as…"),
            ("Ctrl+E",        "Export PDF with notes"),
            ("Ctrl+R",        "Reload (new instance)"),
            ("Ctrl+\\",       "Toggle notes"),
            ("Notes panel",   None),
            ("Ctrl++ / Ctrl+-", "Bigger / smaller notes font"),
            ("Ctrl+0",        "Reset notes font size"),
            ("Ctrl+Scroll",   "Bigger / smaller notes font"),
            ("Tabs",          None),
            ("Ctrl+W",        "Close tab"),
            ("Ctrl+Shift+T",  "Reopen the last closed tab"),
        ]

        grid = Gtk.Grid()
        grid.set_row_spacing(5)
        grid.set_column_spacing(12)
        grid.set_margin_start(16)
        grid.set_margin_end(16)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)

        row = 0
        for key, desc in shortcuts:
            if desc is None:
                heading = Gtk.Label(label=key)
                heading.add_css_class("dim-label")
                heading.set_xalign(0)
                heading.set_margin_top(8 if row > 0 else 0)
                grid.attach(heading, 0, row, 2, 1)
            else:
                key_lbl = Gtk.Label(label=key)
                key_lbl.add_css_class("shortcut-key")
                key_lbl.set_xalign(1)
                desc_lbl = Gtk.Label(label=desc)
                desc_lbl.set_xalign(0)
                grid.attach(key_lbl,  0, row, 1, 1)
                grid.attach(desc_lbl, 1, row, 1, 1)
            row += 1

        # The list is taller than small windows — without a height-capped
        # scroller GTK refuses to map a popover that does not fit.
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(grid)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_width(True)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(480)
        return scroll

    def _menu_subpage(self, title, content):
        """A menu stack sub-page: a '← title' back header above the content. The
        back button returns to the main menu page (no popover involved)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        back = Gtk.Button()
        back.add_css_class("flat")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        arrow = Gtk.Image.new_from_icon_name("go-previous-symbolic")
        head.append(arrow)
        head.append(Gtk.Label(label=title, xalign=0))
        back.set_child(head)
        back.connect("clicked",
                     lambda _b: self._menu_stack.set_visible_child_name("main"))
        box.append(back)
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_bottom(2)
        box.append(sep)
        content.set_vexpand(True)
        box.append(content)
        return box

    def _show_menu_page(self, name):
        """Switch the ☰ menu to a sub-page in place (recent / shortcuts) — keeps
        the single menu popover open instead of opening a racing sibling one."""
        if name == "recent":
            self._rebuild_recent_menu()
        self._menu_stack.set_visible_child_name(name)

    # ── page & notes handshake ────────────────────────────────────────────────

    def _set_file_title(self, subtitle, full_path=None):
        self._file_label.set_label(subtitle)
        self._file_label.set_tooltip_text(full_path or subtitle)
        self.set_title(f"Sidemark — {subtitle}")
        self._update_tab_title(self._active_session)

    # ── tabs ──────────────────────────────────────────────────────────────────

    def _add_session_tab(self, s):
        """Append a session's document subtree as a new tab page."""
        page = self._tab_view.append(s.content)
        page.session = s          # so a torn-off page can find its session
        s._tab_page = page
        self._update_tab_title(s)
        return page

    def _update_tab_title(self, s):
        if s is None or s._tab_page is None:
            return
        if s._path:
            title = os.path.basename(s._path)
        elif s._notes_path:
            title = os.path.basename(s._notes_path)
        else:
            title = "Untitled"
        s._tab_page.set_title(title)
        s._tab_page.set_tooltip(s._path or s._notes_path or "")

    def _session_for_page(self, page):
        for s in self._sessions:
            if s._tab_page is page:
                return s
        return None

    def _on_tab_switched(self, tab_view, _pspec):
        page = tab_view.get_selected_page()
        if page is None:
            return
        s = self._session_for_page(page)
        if s is not None and s is not self._active_session:
            self._activate_session(s)

    def _activate_session(self, s):
        """Make `s` the active document and sync the shared header chrome, which
        all acts on self._active_session through the per-document proxies."""
        # flush the outgoing document's in-progress note into its model first
        if self._active_session is not None and self._active_session is not s:
            self._commit_note_for(self._active_session)
        self._active_session = s
        # notes panel toggle reflects this document's panel visibility
        self._notes_toggle.handler_block(self._notes_toggled_id)
        self._notes_toggle.set_active(
            bool(s._notes_box) and s._notes_box.get_visible())
        self._notes_toggle.handler_unblock(self._notes_toggled_id)
        # sidebar toggle reflects this document's revealer (idempotent set)
        self._toc_btn.set_active(
            bool(s._toc_revealer) and s._toc_revealer.get_reveal_child())
        # menu filename label + window subtitle
        name = (os.path.basename(s._path) if s._path
                else os.path.basename(s._notes_path) if s._notes_path
                else "")
        self._set_file_title(name, s._path or s._notes_path)
        self._update_header_for_mode()   # also re-measures header collapse

    def _new_tab(self):
        """Create a fresh document session in a new tab and make it active."""
        s = DocumentSession()
        self._build_document_widgets(s)
        self._sessions.append(s)
        page = self._add_session_tab(s)
        # selecting the page fires notify::selected-page -> _activate_session
        self._tab_view.set_selected_page(page)
        return s

    def _session_is_pristine(self, s):
        """True for an untitled, unmodified, empty scratchpad tab — safe to reuse
        for the next open instead of leaving it behind as an empty tab."""
        return (s._path is None and s._notes_path is None
                and not s._deck_mode
                and not s._dirty and not s.notes_model.has_content())

    def open_file_in_tab(self, path):
        """Open from *within* the window: reuse a pristine scratchpad tab if the
        active one is empty, else add a new tab, then load the file into it.
        (Opens that originate outside the window get their own window instead.)"""
        if not self._session_is_pristine(self._active_session):
            self._new_tab()
        self.open_file(path)

    def _close_active_tab(self):
        page = self._tab_view.get_selected_page()
        if page is None:
            self.close()
        else:
            self._tab_view.close_page(page)   # fires close-page below

    def _remember_closed(self, s):
        """Push a just-closed document onto the reopen stack so Ctrl+Shift+T can
        bring it back. Only files with a real path are reopenable (their notes
        are remembered per-PDF); untitled scratch tabs are skipped."""
        path = getattr(s, "_path", None)
        if path and not getattr(s, "_is_untitled", False):
            self._closed_tabs.append(path)
            del self._closed_tabs[:-20]   # keep the stack bounded

    def _reopen_closed_tab(self):
        while self._closed_tabs:
            path = self._closed_tabs.pop()
            if os.path.exists(path):
                self.open_file_in_tab(path)
                return
        self._toast("No recently closed tabs")

    def _on_tab_close(self, tab_view, page):
        """A tab's close button (or Ctrl+W) was used. Prompt for unsaved changes
        in that document, then confirm or veto the close asynchronously."""
        s = self._session_for_page(page)
        if s is None or not s._dirty:
            self._remember_closed(s)
            tab_view.close_page_finish(page, True)
            return True
        tab_view.set_selected_page(page)   # show which document is being closed
        dlg = Adw.AlertDialog.new(
            "Unsaved changes",
            f"Save changes to "
            f"{os.path.basename(s._path or s._notes_path or 'Untitled')}?")
        dlg.add_response("discard", "Discard")
        dlg.add_response("cancel",  "Cancel")
        dlg.add_response("save",    "Save")
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, r):
            if r == "save":
                self._remember_closed(s)
                self._on_save(after=lambda: tab_view.close_page_finish(page, True))
            elif r == "discard":
                if s._path:
                    _discard_autosave(s._path)
                self._remember_closed(s)
                tab_view.close_page_finish(page, True)
            else:
                tab_view.close_page_finish(page, False)   # veto
        dlg.connect("response", on_response)
        dlg.present(self)
        return True

    def _on_page_detached(self, tab_view, page, _position):
        """A page was closed (or torn off into another window). Drop its session
        from this window and close the window once its last tab is gone."""
        s = getattr(page, "session", None)
        if s is not None and s in self._sessions:
            self._sessions.remove(s)
            # the presenter is bound to this window; drop it on the way out
            if s._presenter is not None:
                s._presenter.close()
                s._presenter = None
        if tab_view.get_n_pages() == 0:
            self.close()

    def _on_tab_create_window(self, _tab_view):
        """A tab was dragged out to empty space: host it in a fresh window and
        hand libadwaita that window's TabView to move the page into."""
        new_win = PDFEditorWindow(self.get_application())
        new_win.present()
        return new_win._tab_view

    def _on_page_attached(self, _tab_view, page, _position):
        """Adopt a page torn off from another window. The per-document signals
        already route through s.win, so re-pointing it (plus repopulating the
        dynamically-built sidebar) hands the whole document to this window."""
        s = getattr(page, "session", None)
        if s is None or s in self._sessions:
            return   # our own append — already tracked
        s.win = self
        self._sessions.append(s)
        self._activate_session(s)
        # the thumbnail/outline rows are built dynamically and were wired to the
        # old window; rebuild them against this one
        self._populate_toc()
        # this window opened with an empty scratchpad tab — drop it now
        GLib.idle_add(self._prune_pristine_tabs, s)

    def _prune_pristine_tabs(self, keep):
        for s in list(self._sessions):
            if s is not keep and s._tab_page is not None \
                    and self._session_is_pristine(s):
                self._tab_view.close_page(s._tab_page)
        return False

    def _on_realize(self, _widget):
        GLib.idle_add(self._init_pane_position)

    def _init_pane_position(self):
        width = self.get_width()
        if width < 200:
            return GLib.SOURCE_CONTINUE
        self._saved_pane_pos = int(width * 0.62)
        self._paned.set_position(self._saved_pane_pos)
        return GLib.SOURCE_REMOVE

    def _add_blank_page(self):
        if not self.canvas.document:
            return
        self._commit_note()
        # Shift notes before the canvas inserts and navigates to the new page,
        # so _restore_note already sees the re-keyed model.
        idx = self.canvas.current_page_idx + 1
        self.notes_model.shift_for_insert(idx)
        self._undo_timeline = [
            ("notes", op[1] + 1, op[2]) if op[0] == "notes" and op[1] >= idx else op
            for op in self._undo_timeline
        ]
        self._redo_timeline = [
            ("notes", op[1] + 1) + op[2:] if op[0] == "notes" and op[1] >= idx else op
            for op in self._redo_timeline
        ]
        self.canvas.add_blank_page()
        self._populate_toc()
        self._mark_dirty()

    def _delete_current_page(self):
        if not self.canvas.document:
            return
        if self.canvas.n_pages <= 1:
            toast = Adw.Toast.new("Cannot delete the only page")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
            return
        idx = self.canvas.current_page_idx
        self.notes_model.shift_for_delete(idx)
        self._undo_timeline = [
            ("notes", op[1] - 1, op[2]) if op[0] == "notes" and op[1] > idx else op
            for op in self._undo_timeline
            if not (op[0] == "notes" and op[1] == idx)
        ]
        self._redo_timeline = [
            ("notes", op[1] - 1) + op[2:] if op[0] == "notes" and op[1] > idx else op
            for op in self._redo_timeline
            if not (op[0] == "notes" and op[1] == idx)
        ]
        self.canvas.delete_current_page()
        self._populate_toc()
        self._mark_dirty()

    def _move_page(self, src, dst):
        """Reorder pages: move page src to index dst, re-keying notes too."""
        if not self.canvas.document or src == dst:
            return
        n = self.canvas.n_pages
        if not (0 <= src < n and 0 <= dst < n):
            return
        self._commit_note()
        order = PDFCanvas._move_order(n, src, dst)
        old_to_new = {old: new for new, old in enumerate(order)}
        self.notes_model.reorder(old_to_new)
        self._undo_timeline = [
            ("notes", old_to_new[op[1]], op[2]) if op[0] == "notes" else op
            for op in self._undo_timeline
        ]
        self._redo_timeline = [
            ("notes", old_to_new[op[1]]) + op[2:] if op[0] == "notes" else op
            for op in self._redo_timeline
        ]
        self.canvas.move_page(src, dst)
        self._populate_toc()
        self._mark_dirty()

    # ── dirty tracking ────────────────────────────────────────────────────────

    def _mark_dirty(self, *_):
        if not self._suppress_dirty:
            self._dirty = True
        # bump the revision so any live phone-share view knows to refresh
        self._share_revision += 1
        if self._presenter is not None:
            self._presenter.refresh()

    def _on_notes_changed(self, _buf):
        # A real user edit (not a page restore, not the symbol-substitution
        # machinery) opens a typing burst: one timeline entry that covers all
        # typing until the next canvas action or page switch.
        if (not self._suppress_dirty and not self._notes_view._in_highlight
                and not self._notes_burst_open):
            self._undo_timeline.append(
                ("notes", self.canvas.current_page_idx, self._burst_base))
            self._notes_burst_open = True
            self._redo_timeline.clear()   # typing is a new action
        self._mark_dirty()
        self._update_canvas_anchors()

    def _clear_dirty(self):
        self._dirty = False

    # ── autosave ──────────────────────────────────────────────────────────────

    def _autosave_tick(self):
        # snapshot every dirty tab, not just the active one
        for s in self._sessions:
            try:
                if s._dirty and s._path:
                    self._write_autosave_for(s)
                elif s._dirty and s._deck_mode and s._deck_path:
                    self._write_deck_autosave_for(s)
            except Exception:
                logger.error("autosave failed:\n" + traceback.format_exc())
        return True   # keep the timer running

    def _write_deck_autosave_for(self, s):
        d = _autosave_dir_for(s._deck_path)
        os.makedirs(d, exist_ok=True)
        self._commit_note_for(s)   # speaker notes live inside the model
        s._deck_view.model.save(os.path.join(d, "deck.smdeck"))
        meta = {"path": os.path.abspath(s._deck_path), "saved_at": time.time()}
        tmp = os.path.join(d, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(d, "meta.json"))
        logger.info(f"autosave: deck snapshot written for {s._deck_path}")

    def _maybe_offer_deck_recovery(self, path):
        """Offer to restore an autosave snapshot newer than the .smdeck itself
        (Sidemark closed with unsaved deck changes)."""
        d = _autosave_dir_for(path)
        snap = os.path.join(d, "deck.smdeck")
        meta_path = os.path.join(d, "meta.json")
        if not (os.path.exists(snap) and os.path.exists(meta_path)):
            return
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return
        if meta.get("path") != os.path.abspath(path):
            return   # hash collision or moved file — don't recover blindly
        saved_at = meta.get("saved_at", 0)
        try:
            if os.path.getmtime(path) >= saved_at:
                return   # the file was saved/modified after the snapshot
        except OSError:
            pass
        when = time.strftime("%H:%M on %Y-%m-%d", time.localtime(saved_at))
        dlg = Adw.AlertDialog.new(
            "Recover unsaved changes?",
            f"Sidemark closed with unsaved changes for this presentation "
            f"(autosaved at {when}).")
        dlg.add_response("later", "Not now")
        dlg.add_response("discard", "Discard them")
        dlg.add_response("recover", "Recover")
        dlg.set_response_appearance("recover",
                                    Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("recover")
        dlg.set_close_response("later")

        def on_response(_d, response):
            if response == "recover":
                try:
                    model = _deck_module().DeckModel.load(snap)
                except (OSError, ValueError) as e:
                    self._show_error("Could not recover snapshot", str(e))
                    return
                self._deck_view.model = model
                self._deck_view._reset_view()
                self._restore_note()
                self._mark_dirty()   # recovered ≠ saved: Ctrl+S writes it back
            elif response == "discard":
                _discard_autosave(path)
        dlg.connect("response", on_response)
        dlg.present(self)

    def _write_autosave(self):
        self._write_autosave_for(self._active_session)

    def _write_autosave_for(self, s):
        d = _autosave_dir_for(s._path)
        os.makedirs(d, exist_ok=True)
        s.canvas.save_copy(os.path.join(d, "doc.pdf"))
        self._commit_note_for(s)
        s.notes_model.save(os.path.join(d, "notes.md"))
        meta = {"path": os.path.abspath(s._path), "saved_at": time.time()}
        tmp = os.path.join(d, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(d, "meta.json"))
        logger.info(f"autosave: snapshot written for {s._path}")

    def _maybe_offer_recovery(self, path):
        found = _find_autosave(path)
        if not found:
            return
        snap_pdf, snap_notes, saved_at = found
        when = time.strftime("%H:%M on %Y-%m-%d", time.localtime(saved_at))
        dlg = Adw.AlertDialog.new(
            "Recover unsaved changes?",
            f"Sidemark closed with unsaved changes for this file "
            f"(autosaved at {when}).",
        )
        dlg.add_response("later",   "Not now")
        dlg.add_response("discard", "Discard them")
        dlg.add_response("recover", "Recover")
        dlg.set_response_appearance("recover", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("recover")
        dlg.set_close_response("later")

        def on_response(d, r):
            if r == "recover":
                if snap_notes:
                    self.notes_model.load(snap_notes)
                self.canvas.load(snap_pdf)   # _path stays the original file
                self._populate_toc()
                self._mark_dirty()
            elif r == "discard":
                _discard_autosave(path)
            # "later": keep the snapshot for the next open

        dlg.connect("response", on_response)
        dlg.present(self)

    # ── unsaved-changes dialog ────────────────────────────────────────────────

    def _on_close_request(self, _win):
        if not any(s._dirty for s in self._sessions):
            self._destroy_all()
            return False   # allow close
        self._prompt_close_next_dirty()
        return True        # block default close; destroy() called once resolved

    def _prompt_close_next_dirty(self):
        """Walk the dirty tabs one at a time on window close, then destroy."""
        dirty = [s for s in self._sessions if s._dirty]
        if not dirty:
            self._destroy_all()
            return
        s = dirty[0]
        self._tab_view.set_selected_page(s._tab_page)   # show which document
        dlg = Adw.AlertDialog.new(
            "Unsaved changes",
            f"Save changes to "
            f"{os.path.basename(s._path or s._notes_path or 'Untitled')}?")
        dlg.add_response("discard", "Discard")
        dlg.add_response("cancel",  "Cancel")
        dlg.add_response("save",    "Save")
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, r):
            if r == "save":
                self._on_save(after=lambda: (self._clear_dirty(),
                                             self._prompt_close_next_dirty()))
            elif r == "discard":
                if s._path:
                    _discard_autosave(s._path)
                self._clear_dirty()
                self._prompt_close_next_dirty()
            # cancel: stop closing, leave the window open
        dlg.connect("response", on_response)
        dlg.present(self)

    def _destroy_all(self):
        for s in self._sessions:
            if s._presenter is not None:
                s._presenter.close()
        self.destroy()

    def _ask_save_then(self, callback):
        dlg = Adw.AlertDialog.new(
            "Unsaved changes",
            "Save before continuing?",
        )
        dlg.add_response("discard", "Discard")
        dlg.add_response("cancel",  "Cancel")
        dlg.add_response("save",    "Save")
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        def on_response(d, r):
            if r == "save":
                # Run callback only once the save actually succeeded — the
                # untitled path opens an async save-as dialog, and a failed
                # save must not proceed (e.g. destroy the window).
                self._on_save(after=callback)
            elif r == "discard":
                if self._path:
                    _discard_autosave(self._path)   # user chose to drop them
                callback()
            # cancel: do nothing
        dlg.connect("response", on_response)
        dlg.present(self)

    # ── page & notes handshake ────────────────────────────────────────────────

    def _on_page_changed(self, idx, n):
        self._page_label.set_label(f"{idx + 1} / {n}")
        self._restore_note()
        self._update_search_canvas()
        if self._toc_thumbs and self._toc_revealer.get_reveal_child():
            self._select_thumb(idx)
        if self._presenter is not None:
            self._presenter.sync_page()

    def _go_to_page(self, idx):
        self._commit_note()
        self.canvas.go_to_page(idx)

    def _nav_page(self, delta):
        """Relative page navigation (PageUp/Down, mouse back/forward): zoomed
        views keep their zoom and align to the new page's top/bottom, fitted
        views re-fit — same behavior as scroll-past-edge flips."""
        if self._deck_mode:
            if self._deck_view is not None:
                # set_current commits the slide's speaker notes on the way out
                self._deck_view.set_current(self._deck_view.current + delta)
            return
        c = self.canvas
        if not c.document:
            return
        target = max(0, min(c.n_pages - 1, c.current_page_idx + delta))
        if target == c.current_page_idx:
            return
        self._commit_note()
        if self._presenter is not None:
            # While presenting, each slide should show whole and centred — re-fit
            # the new page rather than carrying a zoomed-in reading position over.
            c.go_to_page(target)
        else:
            c._flip_page(target - c.current_page_idx)

    def _on_nav_history(self, can_back):
        """Canvas pushed/popped a link-jump location. The first time a link is
        followed in a session, hint that Alt+Left returns — the jump itself is
        otherwise silent."""
        if can_back and not self._link_hint_shown:
            self._link_hint_shown = True
            self._toast("Jumped to link — press Alt+Left to go back")

    def _commit_note(self):
        self._commit_note_for(self._active_session)

    def _commit_note_for(self, s):
        if s._deck_mode:
            # deck: the notes panel edits the current slide's speaker notes,
            # stored inside the .smdeck (no sidecar)
            if s._deck_view is not None:
                slide = s._deck_view.model.slides[s._deck_view.current]
                slide["notes"] = s._notes_view.get_source_text()
            return
        if not s._path and not s._notes_path and not s._is_untitled:
            return
        text = s._notes_view.get_source_text()
        s.notes_model.set(s.canvas.current_page_idx, text)

    def _restore_note(self):
        self._suppress_dirty = True
        if self._deck_mode and self._deck_view is not None:
            dv = self._deck_view
            text = dv.model.slides[dv.current].get("notes", "")
        else:
            text = self.notes_model.get(self.canvas.current_page_idx)
        self._last_anchor_mark = None   # set_text would strand the mark at offset 0
        buf = self._notes_view.get_buffer()
        # Programmatic page loads must not enter the undo history — otherwise
        # Ctrl+Z in the notes view could resurrect another page's text here
        buf.begin_irreversible_action()
        buf.set_text(text)
        buf.end_irreversible_action()
        self._notes_view.reset_render_state()
        self._suppress_dirty = False
        # a page switch ends any typing burst; future bursts diff against this text
        self._notes_burst_open = False
        self._burst_base = text
        self._update_canvas_anchors()

    # ── global undo ───────────────────────────────────────────────────────────

    def _on_canvas_action(self):
        """A draw/erase gesture finished: record it and end any typing burst."""
        self._notes_burst_open = False
        self._burst_base = self._notes_view.get_source_text()
        self._undo_timeline.append(("canvas",))
        self._redo_timeline.clear()   # canvas already cleared its own redo

    def _on_global_key(self, ctrl, keyval, keycode, state):
        """PDF-level shortcuts that must fire regardless of focus, intercepted in
        the capture phase so the notes editor can't swallow them first. Only these
        specific keys are consumed; every other key passes through to typing."""
        ctrl_held = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift_held = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl_held and shift_held and keyval in (Gdk.KEY_t, Gdk.KEY_T):
            # reopen the most recently closed tab (browser-style)
            self._reopen_closed_tab()
            return True
        if ctrl_held and keyval in (Gdk.KEY_w, Gdk.KEY_W):
            # close the current tab (with its own unsaved-changes prompt); the
            # window closes once its last tab is gone.
            self._close_active_tab()
            return True
        if ctrl_held and keyval == Gdk.KEY_backslash:
            if not self._text_mode:   # a text page has no notes panel to toggle
                self._notes_toggle.set_active(not self._notes_toggle.get_active())
            return True
        if not ctrl_held and keyval == Gdk.KEY_Page_Down:
            self._nav_page(1)
            return True
        if not ctrl_held and keyval == Gdk.KEY_Page_Up:
            self._nav_page(-1)
            return True
        if (state & Gdk.ModifierType.ALT_MASK) and keyval == Gdk.KEY_Left:
            # return to the reading spot we left when following a link (footnote,
            # citation, internal cross-reference)
            if self.canvas.nav_back():
                self._toast("Back to where you were")
            return True
        return False

    def _on_undo_key(self, ctrl, keyval, keycode, state):
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        is_z = keyval in (Gdk.KEY_z, Gdk.KEY_Z)
        is_y = keyval in (Gdk.KEY_y, Gdk.KEY_Y)
        if not is_z and not is_y:
            return False
        # leave undo/redo alone inside entries (search bar, dialogs)
        focus = self.get_focus()
        if isinstance(focus, Gtk.Editable):
            return False
        if is_y or (state & Gdk.ModifierType.SHIFT_MASK):
            self._global_redo()   # Ctrl+Y or Ctrl+Shift+Z
        else:
            self._global_undo()
        return True

    def _set_notes_text(self, page, text):
        """Put text into the notes buffer and model without touching the
        timeline — shared by global undo and redo."""
        if page != self.canvas.current_page_idx:
            self._go_to_page(page)   # show the user what is being changed
        buf = self._notes_view.get_buffer()
        self._suppress_dirty = True
        self._last_anchor_mark = None
        buf.begin_irreversible_action()
        buf.set_text(text)
        buf.end_irreversible_action()
        self._notes_view.reset_render_state()
        self._suppress_dirty = False
        self._notes_burst_open = False
        self._burst_base = text
        self.notes_model.set(page, text)
        self._mark_dirty()
        self._update_canvas_anchors()

    def _global_undo(self):
        """Undo the most recent user action — a stroke, an erase gesture, or a
        typing burst — in chronological order across canvas and notes."""
        if self._deck_mode and self._deck_view is not None:
            # typing in the speaker-notes panel undoes locally (the source
            # view's own history); everything else is the deck's timeline
            if self._notes_view.has_focus():
                self._notes_view.get_buffer().undo()
                return
            self._deck_view.undo()   # the deck owns its whole timeline
            return
        if not self._undo_timeline:
            return
        op = self._undo_timeline.pop()
        if op[0] == "canvas":
            self.canvas.undo_last()
            self._redo_timeline.append(("canvas",))
            return
        if op[0] == "ink":
            if self._text_page is not None:
                self._text_page.undo_ink()
            self._redo_timeline.append(("ink",))
            return
        _, page, before = op
        if page != self.canvas.current_page_idx:
            self._go_to_page(page)
        after = self._notes_view.get_source_text()
        self._redo_timeline.append(("notes", page, before, after))
        self._set_notes_text(page, before)

    def _global_redo(self):
        """Re-apply the most recently undone action (Ctrl+Y / Ctrl+Shift+Z)."""
        if self._deck_mode and self._deck_view is not None:
            if self._notes_view.has_focus():
                self._notes_view.get_buffer().redo()
                return
            self._deck_view.redo()
            return
        if not self._redo_timeline:
            return
        op = self._redo_timeline.pop()
        if op[0] == "canvas":
            self.canvas.redo_last()
            self._undo_timeline.append(("canvas",))
            return
        if op[0] == "ink":
            if self._text_page is not None:
                self._text_page.redo_ink()
            self._undo_timeline.append(("ink",))
            return
        _, page, before, after = op
        self._set_notes_text(page, after)
        self._undo_timeline.append(("notes", page, before))

    def _update_canvas_anchors(self):
        page_idx = self.canvas.current_page_idx
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        parsed = _parse_anchors(text)
        self.canvas._anchors[page_idx] = parsed
        self.canvas._textboxes[page_idx] = _parse_textboxes(text)
        self._anchor_line_nos = [a["line"] for a in parsed]
        self._anchor_para_ends = [a["para_end"] for a in parsed]
        self._on_notes_cursor_moved(buf, None)

    def _on_notes_cursor_moved(self, buf, _param):
        cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        active = {i for i, (ln, end) in enumerate(zip(self._anchor_line_nos, self._anchor_para_ends))
                  if ln <= cursor_line <= end}
        if active != self.canvas._active_anchors:
            self.canvas._active_anchors = active
            self.canvas.queue_draw()

    def _on_anchor_placed(self, page_idx, px, py):
        buf = self._notes_view.get_buffer()
        ins = buf.get_iter_at_mark(buf.get_insert())
        buf.insert(ins, f"\n<!-- anchor:{px}:{py} -->\n")
        # remember the spot right after the anchor comment so a callout
        # marker from the same gesture can be appended next to it
        after = buf.get_iter_at_mark(buf.get_insert())
        after.backward_char()   # before the trailing newline
        if self._last_anchor_mark is not None:
            buf.delete_mark(self._last_anchor_mark)
        self._last_anchor_mark = buf.create_mark(None, after, True)
        self._notes_view.grab_focus()
        self._mark_dirty()

    def _on_callout_placed(self, px, py):
        if self._last_anchor_mark is None:
            return
        buf = self._notes_view.get_buffer()
        it = buf.get_iter_at_mark(self._last_anchor_mark)
        buf.insert(it, f" <!-- callout:{px}:{py} -->")
        self._mark_dirty()

    def _on_callout_moved(self, idx, cx, cy):
        """Rewrite the callout marker belonging to the idx-th anchor with its new
        position. The buffer's changed handler refreshes the canvas anchors."""
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        anchors = list(_ANCHOR_RE.finditer(text))
        if idx >= len(anchors):
            return
        # the callout belongs to this anchor: the first callout marker after it,
        # before the next anchor (matches how _on_callout_placed appends it)
        region_start = anchors[idx].end()
        region_end = anchors[idx + 1].start() if idx + 1 < len(anchors) else len(text)
        cm = _CALLOUT_RE.search(text, region_start, region_end)
        if not cm:
            return
        start = buf.get_iter_at_offset(cm.start())
        end = buf.get_iter_at_offset(cm.end())
        buf.delete(start, end)
        buf.insert(start, f"<!-- callout:{cx}:{cy} -->")
        self._mark_dirty()

    def _on_anchor_moved(self, idx, px, py):
        """Rewrite the idx-th anchor marker in the notes with its new
        position. The buffer's changed handler refreshes the canvas anchors."""
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        matches = list(_ANCHOR_RE.finditer(text))
        if idx >= len(matches):
            return
        m = matches[idx]
        start = buf.get_iter_at_offset(m.start())
        end = buf.get_iter_at_offset(m.end())
        buf.delete(start, end)
        buf.insert(start, f"<!-- anchor:{px}:{py} -->")
        self._mark_dirty()

    def _on_textbox_placed(self, page_idx, px, py):
        """Ctrl+Alt+right-click dropped a standalone text box. Insert its marker
        and a placeholder paragraph in the notes and select the placeholder so
        the user can type straight over it."""
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        if text == "" or text.endswith("\n\n"):
            prefix = ""
        elif text.endswith("\n"):
            prefix = "\n"
        else:
            prefix = "\n\n"
        placeholder = "Text"
        buf.insert(buf.get_end_iter(),
                   f"{prefix}<!-- textbox:{px}:{py} -->\n{placeholder}\n")
        # select the placeholder line so typing replaces it
        end = buf.get_end_iter()
        ok, ls = buf.get_iter_at_line(end.get_line() - 1)
        if ok:
            le = ls.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            buf.select_range(ls, le)
        if not self._notes_toggle.get_active():
            self._notes_toggle.set_active(True)   # reveal the panel to edit
        self._notes_view.grab_focus()
        self._mark_dirty()

    def _on_textbox_moved(self, idx, x, y):
        """Rewrite the idx-th text-box marker with its new position."""
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        matches = list(_TEXTBOX_RE.finditer(text))
        if idx >= len(matches):
            return
        m = matches[idx]
        start = buf.get_iter_at_offset(m.start())
        end = buf.get_iter_at_offset(m.end())
        buf.delete(start, end)
        buf.insert(start, f"<!-- textbox:{x}:{y} -->")
        self._mark_dirty()

    def _on_anchor_clicked(self, idx):
        if idx >= len(self._anchor_line_nos):
            return
        buf = self._notes_view.get_buffer()
        _, it = buf.get_iter_at_line(self._anchor_line_nos[idx])
        buf.place_cursor(it)
        self._notes_view.scroll_to_mark(buf.get_insert(), 0.1, False, 0.0, 0.0)
        self._notes_view.grab_focus()
        self.canvas._active_anchors = {idx}

    # ── outline (TOC) sidebar ─────────────────────────────────────────────────

    def _on_toc_toggled(self, btn):
        if btn.get_active() and not self.canvas.document and not self._deck_mode:
            btn.set_active(False)   # bounce; re-fires toggled with False
            toast = Adw.Toast.new("No document open")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
            return
        self._toc_revealer.set_reveal_child(btn.get_active())
        if btn.get_active() and self._deck_mode:
            # deck thumbnails are built lazily: only while the sidebar shows
            self._populate_toc()
            return
        if btn.get_active() and self._toc_thumbs:
            self._select_thumb(self.canvas.current_page_idx)

    def _on_toc_row_activated(self, _list, row):
        page = getattr(row, "toc_page", None)
        if page is None:
            return
        p = self._thumb_provider
        if self._toc_thumbs and p is not None:
            # a plain click (no Ctrl/Shift) collapses any multi-item selection
            # to just this row; Ctrl/Shift keep extending it for drag-export
            if (p.can_export
                    and not (self.canvas._ctrl_held or self.canvas._shift_held)):
                self._toc_list.unselect_all()
                self._toc_list.select_row(row)
            p.activate(page)
            return
        # outline (TOC) rows always target the PDF canvas
        self._go_to_page(page)
        self.canvas.grab_focus()

    def _on_toc_list_pressed(self, _gesture, _n, _x, y):
        # a click that misses every row (empty space below the thumbnails)
        # clears the multi-page export selection
        if self._toc_thumbs and self._toc_list.get_row_at_y(int(y)) is None:
            self._toc_list.unselect_all()

    def _clear_thumb_selection(self):
        """Drop the multi-page export selection — used when the user clicks away
        into the PDF canvas."""
        if self._toc_thumbs:
            self._toc_list.unselect_all()

    def _populate_toc(self):
        if self._thumb_idle_id is not None:
            GLib.source_remove(self._thumb_idle_id)
            self._thumb_idle_id = None
        while (child := self._toc_list.get_first_child()) is not None:
            self._toc_list.remove(child)
        self._thumb_provider = None
        if self._deck_mode and self._deck_view is not None:
            # deck: no outline — the sidebar is always slide thumbnails
            self._has_toc = False
            self._toc_thumbs = True
            self._toc_switch.set_visible(False)
            self._toc_btn.set_tooltip_text("Toggle slide thumbnails (Ctrl+T)")
            self._build_thumb_rows(_DeckThumbnails(self))
            if self._toc_revealer.get_reveal_child():
                self._select_thumb(self._deck_view.current)
            return
        toc = []
        if self.canvas.document:
            try:
                toc = self.canvas.document.get_toc(simple=True)
            except Exception:
                toc = []
        self._has_toc = bool(toc)
        if self.canvas.document is None:
            self._toc_thumbs = False
            self._toc_switch.set_visible(False)
            self._toc_btn.set_active(False)   # also hides the revealer
            self._toc_btn.set_tooltip_text("No document open")
            return
        # with a TOC the user can flip between outline and thumbnails;
        # without one, thumbnails are the only view
        self._toc_switch.set_visible(self._has_toc)
        self._toc_thumbs = not self._has_toc or self._toc_seg_pages.get_active()
        if self._toc_thumbs:
            self._populate_thumbnails()
            # sidebar hugs the thumbnails (margins + row padding)
            self._toc_scroll.set_size_request(self.THUMB_WIDTH + 32, -1)
            if self._toc_revealer.get_reveal_child():
                self._select_thumb(self.canvas.current_page_idx)
            self._toc_btn.set_tooltip_text(
                "Toggle outline (Ctrl+T)" if self._has_toc else
                "Toggle page thumbnails (Ctrl+T) — no outline in this document")
        else:
            self._toc_list.set_selection_mode(Gtk.SelectionMode.NONE)
            self._toc_scroll.set_size_request(230, -1)
            for level, title, page in toc:
                label = Gtk.Label(label=title.strip() or "—", xalign=0)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_margin_start(8 + 14 * max(0, level - 1))
                label.set_margin_end(8)
                label.set_margin_top(4)
                label.set_margin_bottom(4)
                row = Gtk.ListBoxRow()
                row.set_child(label)
                row.toc_page = page - 1   # get_toc() pages are 1-based
                row.set_tooltip_text(
                    (title.strip() or "—")
                    + " — click to jump (PageUp/PageDown to flip pages)")
                self._toc_list.append(row)
            self._toc_btn.set_tooltip_text("Toggle outline (Ctrl+T)")

    def _on_toc_view_toggled(self, _btn):
        if self.canvas.document:
            self._populate_toc()

    # ── page thumbnails (outline fallback) ────────────────────────────────────

    THUMB_WIDTH = 96

    def _populate_thumbnails(self):
        """Fill the outline sidebar with page thumbnails (PDF mode)."""
        self._build_thumb_rows(_PdfThumbnails(self))

    def _build_thumb_rows(self, provider):
        """Generic sidebar thumbnails: rows, numbers, tooltips, DnD and a lazy
        render queue — the mode's provider supplies content and semantics
        (see _ThumbnailProvider)."""
        self._thumb_provider = provider
        # MULTIPLE so several items can be selected and dragged out together
        # (export-capable modes); the current indicator is a CSS class
        # (.current-page), not the listbox selection, so the two never fight.
        self._toc_list.set_selection_mode(
            Gtk.SelectionMode.MULTIPLE if provider.can_export
            else Gtk.SelectionMode.NONE)
        self._toc_scroll.set_size_request(self.THUMB_WIDTH + 32, -1)
        self._current_thumb_row = None
        pictures = []
        for i in range(provider.count()):
            pic = Gtk.Picture()
            pic.set_size_request(*provider.thumb_size(i))
            num = Gtk.Label(label=str(i + 1))
            num.add_css_class("dim-label")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(2)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.append(pic)
            box.append(num)
            row = Gtk.ListBoxRow()
            row.set_child(box)
            row.toc_page = i
            row.thumb_pic = pic   # in-place refresh (_refresh_thumb_images)
            row.set_tooltip_text(provider.tooltip(i))
            self._add_thumb_dnd(row, i)
            self._toc_list.append(row)
            pictures.append(pic)

        queue = list(enumerate(pictures))

        def render_next():
            # a swapped-out document invalidates the queue
            if not queue or provider.invalidated():
                self._thumb_idle_id = None
                return False
            i, pic = queue.pop(0)
            try:
                pic.set_paintable(provider.render(i))
            except Exception:
                logger.error("thumbnail render failed:\n" + traceback.format_exc())
            if not queue:
                self._thumb_idle_id = None
                return False
            return True

        self._thumb_idle_id = GLib.idle_add(render_next)

    def _refresh_thumb_images(self):
        """Re-render every thumbnail in place (content edits, not structure)."""
        p = self._thumb_provider
        if p is None or p.invalidated():
            return
        i = 0
        while (row := self._toc_list.get_row_at_index(i)) is not None:
            pic = getattr(row, "thumb_pic", None)
            if pic is not None and i < p.count():
                try:
                    pic.set_paintable(p.render(i))
                except Exception:
                    logger.error("thumbnail refresh failed:\n"
                                 + traceback.format_exc())
            i += 1

    def _add_thumb_dnd(self, row, idx):
        """Make a thumbnail row draggable and a drop target. Dropping a thumbnail
        onto another reorders the pages (intra-app MOVE, int payload); dropping a
        PDF *file* from a file manager inserts its pages at that spot (COPY,
        async because Wayland file managers transfer through the portal, which a
        synchronous DropTarget can't read at drop time); dragging a thumbnail out
        exports it as a standalone PDF (COPY, a GdkFileList like macOS Preview).
        A drop-gap indicator shows where the page(s) will land. The export and
        file-insert behaviors attach only when the provider has the matching
        capability (PDF pages yes, deck slides no)."""
        p = self._thumb_provider
        src = Gtk.DragSource()
        src.set_actions(Gdk.DragAction.MOVE
                        | (Gdk.DragAction.COPY if p.can_export else 0))
        src.connect("prepare", self._on_thumb_drag_prepare, idx)
        row.add_controller(src)

        # intra-app reorder: synchronous int target is fine (no portal involved)
        reorder = Gtk.DropTarget.new(int, Gdk.DragAction.MOVE)
        reorder.connect("motion", self._on_thumb_reorder_motion, idx)
        reorder.connect("leave", lambda _t: self._clear_drop_indicator())
        reorder.connect("drop", self._on_thumb_reorder_drop, idx)
        row.add_controller(reorder)

        if p.can_insert_files:
            # external PDF file insert: async target (portal-safe). Internal
            # reorder drags also carry a FileList (the drag-out export), so
            # _accept rejects anything offering the int reorder payload — those
            # go to the target above.
            finsert = Gtk.DropTargetAsync.new(
                Gdk.ContentFormats.new_for_gtype(Gdk.FileList),
                Gdk.DragAction.COPY)
            finsert.connect("accept", self._on_thumb_file_accept)
            finsert.connect("drag-enter", self._on_thumb_file_motion, idx)
            finsert.connect("drag-motion", self._on_thumb_file_motion, idx)
            finsert.connect("drag-leave",
                            lambda _t, _d: self._clear_drop_indicator())
            finsert.connect("drop", self._on_thumb_file_drop, idx)
            row.add_controller(finsert)

        if p.can_export:
            # Ctrl+click toggles this page in/out of the multi-page export
            # selection (file-manager / Preview behaviour). Handled in the
            # capture phase and claimed so neither the row's DragSource nor
            # GtkListBox's own selection also acts on it — otherwise a
            # stationary Ctrl+click can be swallowed by the drag source and
            # never deselect. Plain/Shift clicks fall through.
            ctrl_click = Gtk.GestureClick()
            ctrl_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            ctrl_click.connect("pressed", self._on_thumb_ctrl_pressed, row)
            row.add_controller(ctrl_click)

    def _on_thumb_ctrl_pressed(self, gesture, _n_press, _x, _y, row):
        state = gesture.get_current_event_state()
        # only plain Ctrl (no Shift) toggles; Ctrl+Shift stays a native range op
        if (state & Gdk.ModifierType.CONTROL_MASK
                and not (state & Gdk.ModifierType.SHIFT_MASK)):
            self._toggle_thumb_selection(row)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _toggle_thumb_selection(self, row):
        """Add/remove a single page from the multi-page export selection."""
        if row.is_selected():
            self._toc_list.unselect_row(row)
        else:
            self._toc_list.select_row(row)

    # ── drop-gap indicator + target geometry ──────────────────────────────────
    @staticmethod
    def _gap_for(row, idx, y):
        """Gap index the cursor points at: before this row (idx) when in its top
        half, after it (idx+1) in the bottom half."""
        return idx + 1 if (row is not None and y > row.get_height() / 2) else idx

    def _show_drop_indicator(self, row, after):
        self._clear_drop_indicator()
        if row is not None:
            row.add_css_class("drop-after" if after else "drop-before")
            self._drop_indicator_row = row

    def _clear_drop_indicator(self):
        row = self._drop_indicator_row
        if row is not None:
            row.remove_css_class("drop-before")
            row.remove_css_class("drop-after")
            self._drop_indicator_row = None

    @staticmethod
    def _gap_to_dst(src, gap):
        """Index move_page must use so the page at src lands in gap (a boundary
        before original page `gap`): removing src first shifts later boundaries
        down by one."""
        return gap if gap <= src else gap - 1

    # ── reorder (internal thumbnail drag) ─────────────────────────────────────
    def _on_thumb_reorder_motion(self, target, _x, y, idx):
        row = target.get_widget()
        self._show_drop_indicator(row, y > row.get_height() / 2 if row else False)
        return Gdk.DragAction.MOVE

    def _on_thumb_reorder_drop(self, target, value, _x, y, idx):
        self._clear_drop_indicator()
        if not isinstance(value, int):
            return False
        row = target.get_widget()
        self._reorder_to_gap(value, self._gap_for(row, idx, y))
        return True

    def _reorder_to_gap(self, src, gap):
        dst = self._gap_to_dst(src, gap)
        p = self._thumb_provider
        if dst == src or p is None or p.invalidated():
            return
        if p.confirm_reorder:
            self._confirm_page_change(
                f"Move {p.noun} {src + 1} to position {dst + 1}?",
                lambda: p.reorder(src, dst))
        else:
            p.reorder(src, dst)

    def _do_reorder(self, src, dst):
        self._move_page(src, dst)
        self._toast(f"Moved page {src + 1} → {dst + 1}")

    # ── insert (external PDF file dropped on the sidebar) ──────────────────────
    def _on_thumb_file_accept(self, _target, gdk_drop):
        fmts = gdk_drop.get_formats()
        # let the reorder target handle our own thumbnail drags (they carry int)
        return not (fmts and fmts.contain_gtype(GObject.TYPE_INT))

    def _on_thumb_file_motion(self, target, _drop, _x, y, idx):
        row = target.get_widget()
        self._show_drop_indicator(row, y > row.get_height() / 2 if row else False)
        return Gdk.DragAction.COPY   # advertise COPY so the drop is permitted

    def _on_thumb_file_drop(self, target, gdk_drop, _x, y, idx):
        self._clear_drop_indicator()
        row = target.get_widget()
        gap = self._gap_for(row, idx, y)
        gdk_drop.read_value_async(
            Gdk.FileList, GLib.PRIORITY_DEFAULT, None,
            self._on_thumb_file_read, (gdk_drop, gap))
        return True

    def _on_thumb_file_read(self, _src, result, data):
        gdk_drop, gap = data
        try:
            paths = self._dnd_paths(gdk_drop.read_value_finish(result))
        except Exception as e:
            logger.warning("thumbnail file-drop read failed: %s", e)
            paths = []
        pdfs = [p for p in paths if p.lower().endswith(".pdf")]
        if pdfs:
            self._insert_files_to_gap(pdfs, gap)
            gdk_drop.finish(Gdk.DragAction.COPY)
        else:
            if paths:
                self._toast("Only PDF files can be inserted into a document")
            gdk_drop.finish(0)

    def _insert_files_to_gap(self, paths, gap):
        if not self.canvas.document:
            return
        names = ", ".join(os.path.basename(p) for p in paths)
        self._confirm_page_change(
            f"Insert pages from {names} at position {gap + 1}?",
            lambda: self._do_insert_pdfs(paths, gap))

    def _do_insert_pdfs(self, paths, gap):
        at = max(0, min(gap, self.canvas.n_pages))
        inserted = 0
        for path in paths:
            inserted += self._insert_one_pdf(at + inserted, path)
        if inserted:
            self._populate_toc()
            self._mark_dirty()
            self._toast(f"Inserted {inserted} page{'s' if inserted != 1 else ''}")

    def _insert_one_pdf(self, at, path):
        try:
            probe = fitz.open(path)
            count = len(probe)
            probe.close()
        except Exception:
            logger.error("insert: cannot open %s\n%s", path, traceback.format_exc())
            self._toast(f"Could not read {os.path.basename(path)}")
            return 0
        if count == 0:
            return 0
        # re-key notes/undo before the canvas inserts and navigates, so the
        # page restore already sees the shifted model (mirrors _add_blank_page)
        self._commit_note()
        self.notes_model.shift_for_insert(at, count)
        self._undo_timeline = [
            ("notes", op[1] + count, op[2])
            if op[0] == "notes" and op[1] >= at else op
            for op in self._undo_timeline
        ]
        self._redo_timeline = [
            ("notes", op[1] + count) + op[2:]
            if op[0] == "notes" and op[1] >= at else op
            for op in self._redo_timeline
        ]
        return self.canvas.insert_pdf_pages(at, path)

    # ── confirmation (#60) ─────────────────────────────────────────────────────
    def _confirm_page_change(self, message, on_confirm):
        """Confirm a thumbnail-drop page change before applying it, unless the
        user has ticked 'Don't ask again' (persisted in settings.json)."""
        if not _load_settings().get("confirm_page_drops", True):
            on_confirm()
            return
        dialog = Adw.AlertDialog.new("Apply page change?", message)
        check = Gtk.CheckButton(label="Don't ask again")
        dialog.set_extra_child(check)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Apply")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp == "apply":
                if check.get_active():
                    _save_setting("confirm_page_drops", False)
                on_confirm()

        dialog.connect("response", on_resp)
        dialog.present(self)

    def _toast(self, msg, timeout=2):
        toast = Adw.Toast.new(msg)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def _on_thumb_drag_prepare(self, _src, _x, _y, idx):
        # reorder uses only the grabbed page; export uses the whole selection
        # when the grabbed row is part of it (macOS Finder / Preview behaviour)
        reorder = Gdk.ContentProvider.new_for_value(GObject.Value(int, idx))
        p = self._thumb_provider
        if p is None or not p.can_export:
            return reorder
        try:
            gfile = self._export_pages_tempfile(self._drag_export_indices(idx))
        except Exception:
            logger.error("thumbnail drag-export failed:\n" + traceback.format_exc())
            return reorder   # reorder still works even if export couldn't be built
        flist = Gdk.FileList.new_from_list([gfile])
        export = Gdk.ContentProvider.new_for_value(
            GObject.Value(Gdk.FileList, flist))
        return Gdk.ContentProvider.new_union([reorder, export])

    def _drag_export_indices(self, idx):
        """Pages to export when dragging thumbnail idx: the whole multi-selection
        if the grabbed row belongs to it, otherwise just the grabbed page."""
        selected = sorted(
            r.toc_page for r in self._toc_list.get_selected_rows()
            if getattr(r, "toc_page", None) is not None)
        if idx in selected and len(selected) > 1:
            return selected
        return [idx]

    def _export_pages_tempfile(self, indices):
        """Export the given pages to a temp PDF (ink baked in, plus a notes page
        after any page that has notes — the Ctrl+E layout, scoped to these
        pages) named like Preview, returned as a GFile for drag-out. The temp
        dir is swept on exit; the file the user drops is the manager's own copy."""
        if self._drag_export_dir is None:
            self._drag_export_dir = tempfile.mkdtemp(prefix="sidemark-pages-")
            atexit.register(shutil.rmtree, self._drag_export_dir,
                            ignore_errors=True)
        indices = sorted(set(indices))   # match canvas.export_pages' page order
        base = "page"
        if self._path:
            base = os.path.splitext(os.path.basename(self._path))[0] or "page"
        if len(indices) == 1:
            suffix = f"p{indices[0] + 1}"
        elif indices == list(range(indices[0], indices[-1] + 1)):
            suffix = f"p{indices[0] + 1}-{indices[-1] + 1}"   # contiguous run
        else:
            suffix = f"{len(indices)}pages"
        out_path = os.path.join(self._drag_export_dir,
                                _safe_filename(f"{base}-{suffix}.pdf"))

        self._commit_note()   # flush the live notes buffer for the current page
        pages_pdf = os.path.join(self._drag_export_dir, ".pages.tmp.pdf")
        self.canvas.export_pages(indices, pages_pdf)

        # Re-key the dragged pages' notes to the exported page order; if any has
        # notes, bake them in (also renders anchors/callouts) via the export path.
        sub = NotesModel()
        has_notes = False
        for new_idx, orig in enumerate(indices):
            text = self.notes_model.get(orig)
            sub.set(new_idx, text)
            has_notes = has_notes or bool(text.strip())
        if has_notes:
            _export_pdf_with_notes(pages_pdf, out_path, sub,
                                   include_empty=False,
                                   accent=self.canvas.zoom_accent)
            os.remove(pages_pdf)
        else:
            os.replace(pages_pdf, out_path)
        return Gio.File.new_for_path(out_path)

    def _select_thumb(self, idx):
        """Mark the current page's thumbnail (a CSS class, not the listbox
        selection — which the user owns for multi-page drag-export) and scroll
        it into view."""
        row = self._toc_list.get_row_at_index(idx)
        if row is None:
            return
        if self._current_thumb_row is not None and self._current_thumb_row is not row:
            self._current_thumb_row.remove_css_class("current-page")
        row.add_css_class("current-page")
        self._current_thumb_row = row
        ok, bounds = row.compute_bounds(self._toc_list)
        if ok:
            adj = self._toc_scroll.get_vadjustment()
            target = bounds.get_y() + bounds.get_height() / 2 - adj.get_page_size() / 2
            adj.set_value(max(0.0, min(target, adj.get_upper() - adj.get_page_size())))

    # One unified window, three document types: which header chrome each mode
    # shows (see DocumentSession.doc_mode). Tool buttons ("_mode_*") apply to
    # their popover twins ("_pmode_*") automatically in _update_header_for_mode.
    _MODE_CHROME = {
        "_toc_btn":      ("pdf", "deck"),
        "_nav_box":      ("pdf",),
        "_pages_box":    ("pdf",),
        "_share_btn":    ("pdf",),
        "_notes_toggle": ("pdf", "deck"),   # deck: speaker notes
        "_present_btn":  ("pdf", "deck"),
        "_deck_bar":     ("deck",),
        "_mode_text":    ("text",),
        "_mode_pen":     ("pdf", "text", "deck"),
        "_mode_hl":      ("pdf", "text", "deck"),
        "_mode_eraser":  ("pdf", "text", "deck"),
        "_mode_lasso":   ("pdf",),
        "_mode_select":  ("pdf", "deck"),   # deck: the object-select arrow
        "_mode_pan":     ("pdf", "text"),
        "_mode_zoom":    ("pdf",),
        "_mode_anchor":  ("pdf",),
        "_pen_btn":      ("pdf", "text", "deck"),
    }

    _TOOL_ORDER = {"pen": 0, "highlighter": 1, "eraser": 2, "lasso": 3,
                   "select": 4, "pan": 5, "zoom": 6, "anchor": 7}

    def _attach_select_style_menu(self, button):
        """Long-press a select-tool button to pick reading-order vs rectangular."""
        pop = Gtk.Popover()
        pop.set_parent(button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(8); box.set_margin_bottom(8)
        r_read = Gtk.CheckButton(label="Reading order")
        r_rect = Gtk.CheckButton(label="Rectangular")
        r_rect.set_group(r_read)
        r_read.set_active(self.canvas.select_style != "rect")
        r_rect.set_active(self.canvas.select_style == "rect")
        r_read.connect("toggled",
                       lambda b: b.get_active() and self._set_select_style("reading"))
        r_rect.connect("toggled",
                       lambda b: b.get_active() and self._set_select_style("rect"))
        box.append(r_read); box.append(r_rect)
        pop.set_child(box)
        self._select_style_radios += [("reading", r_read), ("rect", r_rect)]
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda g, x, y: self._tool_style_popup(pop))
        button.add_controller(lp)

    def _set_select_style(self, style):
        """Switch text-selection style and keep both radio menus in sync."""
        if getattr(self, "_syncing_select_style", False):
            return
        self._syncing_select_style = True
        self.canvas.select_style = style
        for s, cb in self._select_style_radios:
            cb.set_active(s == style)
        self._syncing_select_style = False

    def _attach_highlight_style_menu(self, button):
        """Long-press a highlighter button to pick free-hand vs text marking."""
        pop = Gtk.Popover()
        pop.set_parent(button)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_start(8); box.set_margin_end(8)
        box.set_margin_top(8); box.set_margin_bottom(8)
        r_free = Gtk.CheckButton(label="Free-hand")
        r_text = Gtk.CheckButton(label="Mark text")
        r_text.set_group(r_free)
        r_free.set_active(self.canvas.highlight_style != "text")
        r_text.set_active(self.canvas.highlight_style == "text")
        r_free.connect("toggled",
                       lambda b: b.get_active() and self._set_highlight_style("free"))
        r_text.connect("toggled",
                       lambda b: b.get_active() and self._set_highlight_style("text"))
        box.append(r_free); box.append(r_text)
        pop.set_child(box)
        self._highlight_style_radios += [("free", r_free), ("text", r_text)]
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda g, x, y: self._tool_style_popup(pop))
        button.add_controller(lp)

    def _tool_style_popup(self, pop):
        """Long-press variant menus (reading-order/rectangular select, free-hand
        /text highlighter) describe PDF behaviours; on a text-first page the
        variants don't apply, so the menus stay shut."""
        if self._active_session and self._active_session._text_mode:
            return
        pop.popup()

    def _set_highlight_style(self, style):
        """Switch highlighter style (free-hand / text) and sync both radio menus."""
        if getattr(self, "_syncing_highlight_style", False):
            return
        self._syncing_highlight_style = True
        self.canvas.highlight_style = style
        for s, cb in self._highlight_style_radios:
            cb.set_active(s == style)
        self._syncing_highlight_style = False

    def _set_tool_mode(self, mode):
        """mode in pen / highlighter / select / pan / zoom / anchor — the bar's
        segmented switch. The tool is the modifier-free shortcut for the matching
        drag gesture (pan↔Ctrl, zoom↔Shift, anchor↔Ctrl+Alt).

        Mirrored by a second toggle group inside the pen popover (shown when the
        bar collapses); keep both in sync without re-entering on the echo.
        """
        if self._syncing_mode:
            return
        self._syncing_mode = True
        try:
            self.canvas.tool = mode
            self.canvas.highlighter = (mode == "highlighter")
            self.canvas.select_mode = (mode == "select")
            if mode != "lasso":
                self.canvas.clear_lasso_selection()
            # cursor reflects the active tool (text/grab/crosshair, or default)
            self.canvas.set_cursor(self.canvas._default_cursor())
            # text-first page: pen/highlighter/eraser ink the sheet, everything
            # else falls back to the text caret
            if self._text_page is not None:
                self._text_page.set_tool(mode)
            # deck: the same ink tools draw on the slide; "select" is the
            # object arrow (move/resize/edit)
            if self._deck_view is not None:
                self._deck_view.set_tool(mode)
            # on a text page "select" is represented by the caret button
            in_text = bool(self._active_session and self._active_session._text_mode)
            if mode == "select" and in_text:
                self._mode_text.set_active(True)
                self._pmode_text.set_active(True)
            else:
                idx = self._TOOL_ORDER[mode]
                for grp in (self._tool_btns, self._ptool_btns):
                    grp[idx].set_active(True)
        finally:
            self._syncing_mode = False
        self._sync_pen_popover()
        self._color_swatch.queue_draw()

    def _highlight_transient_tool(self, tool):
        """Light up the tool button matching the modifiers currently held, so the
        Ctrl/Alt/Shift gestures are discoverable. Purely visual — the selected
        tool and behaviour are untouched."""
        if self._active_session and self._active_session._text_mode:
            # on a text page Alt draws with the pen and Ctrl pans; the other
            # modifier gestures don't apply
            tool = {"select": "pen", "pan": "pan"}.get(tool)
            # Alt doesn't just hint here — it holds the pen for real (so
            # left-drag draws, right-drag erases) and lets go with the key
            if tool == "pen":
                if (self._alt_pen_restore is None
                        and self.canvas.tool == "select"):
                    self._alt_pen_restore = "select"
                    self._set_tool_mode("pen")
            elif self._alt_pen_restore is not None:
                self._restore_after_alt_pen()
        if tool == self._transient_tool:
            return
        self._transient_tool = tool
        for grp in (self._tool_btns, self._ptool_btns):
            for b in grp:
                b.remove_css_class("tool-transient")
        if tool is not None:
            idx = self._TOOL_ORDER[tool]
            for grp in (self._tool_btns, self._ptool_btns):
                grp[idx].add_css_class("tool-transient")

    def _restore_after_alt_pen(self):
        """Hand the tool back to the caret once Alt is released — deferred
        while an ink gesture is still in flight so the stroke isn't lost."""
        tp = self._text_page
        if tp is not None and (tp._drag.is_active() or tp.current_stroke
                               or tp._erased_now):
            GLib.timeout_add(
                60, lambda: bool(self._restore_after_alt_pen()) and False)
            return
        if self._alt_pen_restore is not None:
            mode, self._alt_pen_restore = self._alt_pen_restore, None
            self._set_tool_mode(mode)

    # ── notes-panel font size ────────────────────────────────────────────────
    _NOTES_FONT_DEFAULT = 13
    _NOTES_FONT_MIN = 9
    _NOTES_FONT_MAX = 40
    _NOTES_FONT_STEP = 2

    def _apply_notes_font(self):
        self._notes_font_provider.load_from_data(
            f".notes-view {{ font-size: {self._notes_font_px}px; }}".encode())
        # text-first ink scales with the font so drawings stay glued to words
        for s in self._sessions:
            if s._text_page is not None:
                s._text_page.set_font_px(self._notes_font_px)

    def _change_notes_font(self, direction):
        """direction: +1 bigger, -1 smaller, 0 reset to default. Persisted."""
        if direction == 0:
            self._notes_font_px = self._NOTES_FONT_DEFAULT
        else:
            self._notes_font_px = max(
                self._NOTES_FONT_MIN,
                min(self._NOTES_FONT_MAX,
                    self._notes_font_px + self._NOTES_FONT_STEP * direction))
        self._apply_notes_font()
        _save_setting("notes_font_px", self._notes_font_px)

    # ── responsive header collapse ──────────────────────────────────────────
    def _apply_collapse_level(self, level):
        """0: full · 1: fold pen modes into the popover · 2: also hide
        undo/redo · 3: also hide find/presenter/share. Idempotent."""
        if level == self._collapse_level:
            return
        self._collapse_level = level
        collapse_pen = level >= 1
        self._tools_box.set_visible(not collapse_pen)
        self._pen_modes_section.set_visible(collapse_pen)
        # undo/redo go first (level 2); the rest hold on until it gets tighter
        for b in (self._undo_btn, self._redo_btn, self._undo_sep):
            b.set_visible(level < 2)
        # presenter/share/search stay within what the mode allows (the chrome
        # table in _update_header_for_mode) at every collapse level
        mode = (self._active_session.doc_mode if self._active_session
                else "pdf")
        self._search_btn.set_visible(level < 3 and mode != "deck")
        self._present_btn.set_visible(
            level < 3 and mode in self._MODE_CHROME["_present_btn"])
        self._share_btn.set_visible(
            level < 3 and mode in self._MODE_CHROME["_share_btn"])

    # widen-to-expand needs this much extra slack over the bare fit, so a level
    # change doesn't flicker on 1px resize jitter at the boundary
    _COLLAPSE_HYSTERESIS = 16

    def _measure_controls(self):
        """Non-content width: window buttons + edge padding, read from the real
        allocation (left offset of the start cluster + right gap after the end
        cluster). Re-read each tick so a too-early first reading can't stick."""
        oks, rs = self._header_start.compute_bounds(self._header)
        oke, re = self._header_end.compute_bounds(self._header)
        if not (oks and oke):
            return self._header_controls or 80
        left = rs.origin.x
        right = self._header.get_width() - (re.origin.x + re.get_width())
        return int(max(0, left) + max(0, right))

    def _calibrate_header(self):
        """Record each collapse level's real content width (both clusters), once,
        from the actual widgets — so the breakpoints are derived, not guessed."""
        saved = self._collapse_level
        nat = {}
        for lvl in (3, 2, 1, 0):
            self._apply_collapse_level(lvl)
            s = self._header_start.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            e = self._header_end.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            nat[lvl] = s + e
        self._collapse_natural = nat
        if saved >= 0:
            self._apply_collapse_level(saved)

    def _update_header_collapse(self, *_):
        w = self._header.get_width()
        if w <= 1:
            return
        if self._collapse_natural is None:
            self._calibrate_header()
        self._header_controls = self._measure_controls()
        avail = w - self._header_controls
        nat = self._collapse_natural
        # least collapse whose real content width still fits the space left after
        # the window buttons
        level = (0 if avail >= nat[0] else
                 1 if avail >= nat[1] else
                 2 if avail >= nat[2] else 3)
        # hysteresis: only expand (show more) when there's clear extra room, so
        # we don't oscillate sitting exactly on a breakpoint
        cur = self._collapse_level
        if 0 <= level < cur and avail < nat[level] + self._COLLAPSE_HYSTERESIS:
            level = cur
        self._apply_collapse_level(level)

    def _toggle_select_mode(self):
        """Ctrl+M: flip select-text on/off, falling back to pen."""
        if self._mode_select.get_active():
            self._mode_pen.set_active(True)
        else:
            self._mode_select.set_active(True)

    # ── presentation control bar (timer + large prev/next) ───────────────────
    def _build_present_bar(self):
        """A bottom overlay bar on the editor (presenter) window, shown only
        while presentation mode is active: a timer that can be paused and reset,
        plus large prev/next page buttons for easy navigation while presenting.
        It lives here — on the presenter's own screen — not on the projected
        slide (PresenterWindow stays bare)."""
        self._present_elapsed = 0
        self._present_timer_running = True
        self._present_timer_id = None

        self._present_timer_label = Gtk.Label(label="0:00")
        self._present_timer_label.add_css_class("present-timer")
        self._present_pause_btn = Gtk.Button.new_from_icon_name(
            "media-playback-pause-symbolic")
        self._present_pause_btn.set_tooltip_text("Pause / resume the timer")
        self._present_pause_btn.connect(
            "clicked", lambda *_: self._toggle_present_timer())
        reset_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        reset_btn.set_tooltip_text("Reset the timer to 0:00")
        reset_btn.connect("clicked", lambda *_: self._reset_present_timer())

        prev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        prev_btn.set_tooltip_text("Previous page")
        prev_btn.add_css_class("present-nav")
        prev_btn.connect("clicked", lambda *_: self._nav_page(-1))
        next_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        next_btn.set_tooltip_text("Next page")
        next_btn.add_css_class("present-nav")
        next_btn.connect("clicked", lambda *_: self._nav_page(1))

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        bar.add_css_class("present-bar")
        # styled by a per-window provider so it can scale with the window; the
        # unique class keeps two windows' bars from styling each other
        self._present_bar_class = f"present-bar-{id(self):x}"
        bar.add_css_class(self._present_bar_class)
        for w in (self._present_timer_label, self._present_pause_btn, reset_btn,
                  Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
                  prev_btn, next_btn):
            bar.append(w)
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_valign(Gtk.Align.END)
        bar.set_visible(False)
        self._present_bar = bar
        self._present_overlay.add_overlay(bar)
        self._present_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._present_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._present_bar_scale = 0.0
        self._scale_present_bar(1280, 800)

    # baseline window size for the presentation bar (scale factor 1.0)
    _PRESENT_BAR_BASE = (1280, 800)

    def _scale_present_bar(self, w, h):
        """Size the presentation bar relative to the window, OSD-styled so it
        blends with the theme (translucent pill, flat buttons, accent hover)
        while staying clearly readable over the page."""
        bw, bh = self._PRESENT_BAR_BASE
        f = max(0.8, min(2.5, min(w / bw, h / bh)))
        if abs(f - self._present_bar_scale) < 0.04:
            return   # ignore resize jitter; only restyle on real changes
        self._present_bar_scale = f
        acc_hex = "#{:02x}{:02x}{:02x}".format(
            *(int(c * 255) for c in self._theme_acc))
        cls = self._present_bar_class
        css = f"""
            .{cls} {{
                background-color: rgba(15, 15, 18, 0.62);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: {round(34 * f)}px;
                padding: {round(6 * f)}px {round(16 * f)}px;
            }}
            .{cls} separator {{ background: rgba(255, 255, 255, 0.20); }}
            .{cls} .present-timer {{
                color: #ffffff;
                font-size: {round(24 * f)}px;
                font-feature-settings: "tnum";
                margin: 0 {round(6 * f)}px;
            }}
            .{cls} button {{
                color: #ffffff;
                background: transparent;
                border-radius: {round(26 * f)}px;
                min-width: {round(40 * f)}px;
                min-height: {round(40 * f)}px;
                -gtk-icon-size: {round(16 * f)}px;
            }}
            .{cls} button:hover {{ background: alpha({acc_hex}, 0.50); }}
            .{cls} button:active {{ background: alpha({acc_hex}, 0.75); }}
            .{cls} button.present-nav {{
                min-width: {round(88 * f)}px;
                min-height: {round(52 * f)}px;
                -gtk-icon-size: {round(30 * f)}px;
            }}
        """
        self._present_css.load_from_data(css.encode())
        self._present_bar.set_margin_bottom(round(20 * f))

    def do_size_allocate(self, width, height, baseline):
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        # keep the presentation bar proportional to the window while it shows
        bar = getattr(self, "_present_bar", None)
        if bar is not None and bar.get_visible():
            self._scale_present_bar(width, height)

    def _show_present_bar(self, shown):
        self._present_bar.set_visible(shown)
        # the editor canvas shows the next page behind the current one while
        # presenting (stack look — PowerPoint-style next-slide preview); the
        # deck editor has no PDF canvas to stack, so the bar/timer stand alone
        if not self._deck_mode:
            self.canvas.set_stack_preview(shown)
        if shown:
            # catch up on any resizing that happened while the bar was hidden
            if self.get_width() and self.get_height():
                self._scale_present_bar(self.get_width(), self.get_height())
            self._reset_present_timer()
            self._present_timer_running = True
            self._present_pause_btn.set_icon_name("media-playback-pause-symbolic")
            if self._present_timer_id is None:
                self._present_timer_id = GLib.timeout_add_seconds(
                    1, self._present_tick)
        elif self._present_timer_id is not None:
            GLib.source_remove(self._present_timer_id)
            self._present_timer_id = None

    def _present_tick(self):
        if self._present_timer_running:
            self._present_elapsed += 1
            self._present_timer_label.set_label(_fmt_clock(self._present_elapsed))
        return True

    def _toggle_present_timer(self):
        self._present_timer_running = not self._present_timer_running
        self._present_pause_btn.set_icon_name(
            "media-playback-pause-symbolic" if self._present_timer_running
            else "media-playback-start-symbolic")

    def _reset_present_timer(self):
        self._present_elapsed = 0
        self._present_timer_label.set_label(_fmt_clock(0))

    # ── presenter (second-screen) view ──────────────────────────────────────
    def _on_present_toggled(self, btn):
        if btn.get_active():
            self._open_presenter()
        else:
            self._close_presenter()

    def _open_presenter(self):
        if self._presenter is not None:
            return
        if self._deck_mode:
            if self._deck_view is None:
                self._present_btn.set_active(False)
                return
            pres = _deck_module().DeckPresenterWindow(
                self.get_application(), self._deck_view, on_nav=self._nav_page)
        elif not self.canvas.document:
            self._toast("Open a PDF first")
            self._present_btn.set_active(False)
            return
        else:
            pres = PresenterWindow(self.get_application(), self.canvas,
                                   on_nav=self._nav_page)
        pres.connect("close-request", self._on_presenter_closed)
        self._presenter = pres
        self._show_present_bar(True)   # after _presenter: the preview reads it
        pres.present()
        self._place_presenter(pres)

    def _place_presenter(self, pres):
        """Fullscreen on a monitor other than the editor's; fall back to a
        normal window when there's only one screen."""
        display = self.get_display()
        monitors = display.get_monitors()
        n = monitors.get_n_items() if monitors else 0
        target = None
        if n > 1:
            editor_mon = None
            surface = self.get_surface()
            if surface is not None:
                editor_mon = display.get_monitor_at_surface(surface)
            for i in range(n):
                mon = monitors.get_item(i)
                if mon is not editor_mon:
                    target = mon
                    break
        if target is not None:
            pres.fullscreen_on_monitor(target)
        else:
            pres.set_default_size(960, 720)   # single-monitor: windowed mirror

    def _close_presenter(self):
        if self._presenter is not None:
            pres, self._presenter = self._presenter, None
            pres.detach()
            pres.close()
        self._show_present_bar(False)

    def _on_presenter_closed(self, win):
        # fired when the presenter closes itself (Esc / window close)
        win.detach()
        self._presenter = None
        self._show_present_bar(False)
        if self._present_btn.get_active():
            self._present_btn.set_active(False)   # untoggle the header button
        return False

    def _current_notes_path(self):
        """The .md file the current document's notes are read from / written to."""
        if self._notes_path:          # a .md opened directly (no PDF)
            return self._notes_path
        if self._path:
            return self._active_notes_path or notes_path_for(self._path)
        return None

    def _set_notes_shown(self, shown):
        """Show/hide the notes panel immediately (no slide animation), keeping
        the toggle button in sync without re-triggering its handler. Used on
        document open to start collapsed when there are no notes yet."""
        self._notes_toggle.handler_block(self._notes_toggled_id)
        self._notes_toggle.set_active(shown)
        self._notes_toggle.handler_unblock(self._notes_toggled_id)
        self._notes_box.set_visible(shown)
        if shown:
            w = self.get_width() or 1280
            pos = self._saved_pane_pos
            if not (100 < pos < w - 150):
                pos = int(w * 0.62)
                self._saved_pane_pos = pos
            self._paned.set_position(pos)

    def _choose_notes_file(self):
        if not self._path and not self._notes_path:
            self._toast("Open a document first")
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose notes file")
        md_filter = Gtk.FileFilter()
        md_filter.set_name("Markdown / text")
        for pat in ("*.md", "*.markdown", "*.txt"):
            md_filter.add_pattern(pat)
        any_file = Gtk.FileFilter()
        any_file.set_name("All files")
        any_file.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(md_filter)
        filters.append(any_file)
        dialog.set_filters(filters)
        dialog.set_default_filter(md_filter)
        folder = self._current_dir_gfile()
        if folder:
            dialog.set_initial_folder(folder)
        # an OPEN dialog: we only *read* the chosen file (no overwrite prompt)
        dialog.open(self, None, self._on_notes_file_chosen)

    def _on_notes_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # user cancelled
        if gfile is None:
            return
        path = gfile.get_path()
        if not path:
            return
        self._switch_notes_file(path)

    def _switch_notes_file(self, new_path):
        """Point the notes panel at a different .md: save the current notes to
        their existing file first (lazily), then load the chosen file (or start
        empty if it doesn't exist yet) and remember the choice for this PDF."""
        self._commit_note()
        old = self._current_notes_path()
        if old and old != new_path and (self.notes_model.has_content()
                                        or os.path.exists(old)):
            try:
                self.notes_model.save(old)
            except OSError:
                logger.debug("saving notes to old path failed", exc_info=True)
        self._active_notes_path = new_path
        if self._path:
            self._notes_path = None
            _remember_notes_file(self._path, new_path)
        else:
            self._notes_path = new_path   # notes-only mode
        self.notes_model = NotesModel()
        if self._path:
            self.notes_model.pdf_name = os.path.basename(self._path)
        self.notes_model.load(new_path)   # no-op if the file doesn't exist yet
        self._restore_note()              # refresh the panel for the current page
        self._set_notes_shown(True)       # the user explicitly wants notes now
        self._notes_view.grab_focus()
        self._toast(f"Notes file: {os.path.basename(new_path)}")

    def _on_notes_toggled(self, btn):
        w = self.get_width() or 1280
        if btn.get_active():
            self._notes_box.set_visible(True)
            pos = self._saved_pane_pos
            if pos > w - 150 or pos < 100:
                pos = int(w * 0.62)
                self._saved_pane_pos = pos
            # slide in from the right edge, like the outline revealer
            self._paned.set_position(w)
            self._animate_pane(w, pos)
        else:
            pos = self._paned.get_position()
            if 100 < pos < w - 50:
                self._saved_pane_pos = pos
            self._animate_pane(pos, w, hide_after=True)

    def _animate_pane(self, frm, to, hide_after=False):
        if self._pane_anim is not None:
            self._pane_anim.pause()
        target = Adw.CallbackAnimationTarget.new(
            lambda v: self._paned.set_position(int(v)))
        anim = Adw.TimedAnimation.new(self._paned, frm, to, 250, target)
        anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        if hide_after:
            # only hide if the user didn't re-toggle during the animation
            anim.connect("done", lambda *_: self._notes_box.set_visible(False)
                         if not self._notes_toggle.get_active() else None)
        anim.play()
        self._pane_anim = anim

        # If no frames arrive (headless/offscreen, hidden window) the
        # animation never finishes — jump to the end so the panel state
        # stays correct.
        def force_finish():
            if self._pane_anim is anim and anim.get_state() == Adw.AnimationState.PLAYING:
                anim.skip()
            return False
        GLib.timeout_add(600, force_finish)

    # ── standard helpers ──────────────────────────────────────────────────────

    def _show_error(self, title, detail, tb=None):
        # logger reaches stderr via its stream handler and flags the session
        # log for retention, so crashes stay diagnosable after exit.
        logger.error(f"{title}: {detail}")
        if tb:
            logger.error(tb)
        dlg = Adw.AlertDialog.new(title, detail)
        dlg.add_response("close", "Close")
        dlg.add_response("copy", "Copy Error")
        dlg.set_default_response("close")
        def on_response(d, r):
            if r == "copy":
                content = Gdk.ContentProvider.new_for_value(GLib.Variant('s', detail))
                Gdk.Display.get_default().get_clipboard().set_content(content)
        dlg.connect("response", on_response)
        dlg.present(self)

    def _on_text_copied(self, text):
        if text:
            preview = text[:48].replace("\n", " ")
            if len(text) > 48:
                preview += "…"
            msg = f"Copied: \"{preview}\""
        else:
            msg = "No text in selection"
        toast = Adw.Toast.new(msg)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)

    def _on_width_changed(self, scale):
        if self._syncing_pen:
            return
        if self.canvas.highlighter:
            self.canvas.hl_width = scale.get_value()
        else:
            self.canvas.pen_width = scale.get_value()
        self._recolor_lasso_if_any()

    def _on_smoothing_changed(self, scale):
        self.canvas.smoothing = scale.get_value() / 100.0

    def _on_color_changed(self, btn, _param=None):
        if self._syncing_pen:
            return
        rgba = btn.get_rgba()
        rgb = (rgba.red, rgba.green, rgba.blue)
        if self.canvas.highlighter:
            self.canvas.hl_color = rgb
            self._mode_hl.get_child().queue_draw()
        else:
            self.canvas.pen_color = rgb
        self._color_swatch.queue_draw()
        self._recolor_lasso_if_any()

    def _recolor_lasso_if_any(self):
        """When the lasso tool holds a selection, picking a new colour/width in
        the pen popover retints the selected strokes (one undo entry)."""
        if self.canvas.tool == "lasso" and self.canvas.has_lasso_selection():
            color, width, opacity = self.canvas._pen_attrs()
            self.canvas.recolor_selected(color, width, opacity)

    def _toggle_highlighter(self):
        """Ctrl+H: flip highlighter on/off, falling back to pen."""
        if self._mode_hl.get_active():
            self._mode_pen.set_active(True)
        else:
            self._mode_hl.set_active(True)

    def _sync_pen_popover(self):
        """Point the width scale and color button at the active tool."""
        self._syncing_pen = True
        try:
            if self.canvas.highlighter:
                self._width_scale.set_range(4.0, 24.0)
                self._width_scale.set_value(self.canvas.hl_width)
                color = self.canvas.hl_color
            else:
                self._width_scale.set_range(0.3, 5.0)
                self._width_scale.set_value(self.canvas.pen_width)
                color = self.canvas.pen_color
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = *color, 1.0
            self._color_btn.set_rgba(rgba)
        finally:
            self._syncing_pen = False

    # ── recent files ──────────────────────────────────────────────────────────

    def _remember_recent(self, path):
        path = os.path.abspath(path)
        # the scratchpad and unsaved blanks are noise in a recents list
        data_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "sidemark")
        if path in (os.path.join(data_dir, "scratchpad.md"),
                    os.path.join(data_dir, "scratchpad.pdf")):
            return
        if os.path.basename(path).startswith("sidemark_blank_"):
            return
        try:
            _add_recent(path)
        except OSError:
            logger.error("recent list update failed:\n" + traceback.format_exc())
        # also register with the XDG recent-files store (recently-used.xbel) —
        # GTK/GNOME file dialogs and KDE's KRecentDocument/krunner read it.
        # Skipped under test so the user's real recents aren't polluted (tests
        # run on the live Wayland session; GTK4 has no offscreen backend).
        if not os.environ.get("SIDEMARK_TEST"):
            try:
                Gtk.RecentManager.get_default().add_item(
                    Gio.File.new_for_path(path).get_uri())
            except Exception:
                pass

    def _rebuild_recent_menu(self, _popover=None):
        items = _load_recent()
        box = self._recent_list_box
        child = box.get_first_child()
        while child is not None:            # clear previous rows
            box.remove(child)
            child = box.get_first_child()
        if not items:
            empty = Gtk.Label(label="No recent files")
            empty.add_css_class("dim-label")
            empty.set_margin_start(12)
            empty.set_margin_end(12)
            empty.set_margin_top(8)
            empty.set_margin_bottom(8)
            box.append(empty)
        for it in items:
            path = it["path"]
            row = Gtk.Button()
            row.add_css_class("flat")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            name = Gtk.Label(label=os.path.basename(path), xalign=0)
            where = Gtk.Label(label=os.path.dirname(path), xalign=0)
            where.add_css_class("dim-label")
            where.set_ellipsize(Pango.EllipsizeMode.START)
            where.set_max_width_chars(38)
            inner.append(name)
            inner.append(where)
            row.set_child(inner)

            def _make_open(p):
                def _on_click(_btn):
                    self._menu_pop.popdown()
                    self.open_file_in_tab(p)
                return _on_click
            row.connect("clicked", _make_open(path))
            box.append(row)

    SUPPORTED_DND = (".pdf", ".pptx", ".md", ".smdeck")
    IMAGE_DND = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg")

    def _on_drop_accept(self, _target, gdk_drop):
        # fires repeatedly during hover; keep at debug to avoid log spam
        fmts = gdk_drop.get_formats()
        logger.debug("DnD accept: offered formats = %s",
                     fmts.to_string() if fmts else None)
        # A tab being dragged (within this process it carries AdwTabPage; from
        # another instance only the "x-rootwindow-drop" marker survives the
        # process boundary) must not be swallowed by the file-open target — that
        # would warn "Drop a PDF…". Decline so it falls through to the tab bar.
        if fmts and (fmts.contain_gtype(Adw.TabPage.__gtype__)
                     or fmts.contain_mime_type("application/x-rootwindow-drop")):
            return False
        return True

    def _on_drop_motion(self, _target, _drop, _x, _y):
        # advertise COPY so the drop is permitted on release
        return Gdk.DragAction.COPY

    def _on_drop_async(self, _target, gdk_drop, x, y):
        """Read the dropped file list asynchronously (portal-safe)."""
        logger.info("DnD drop at (%.0f, %.0f) — reading FileList async", x, y)
        gdk_drop.read_value_async(
            Gdk.FileList, GLib.PRIORITY_DEFAULT, None,
            self._on_drop_read, gdk_drop)
        return True

    def _on_drop_read(self, gdk_drop, result, _user_data):
        try:
            value = gdk_drop.read_value_finish(result)
            logger.info("DnD read value = %r (type %s)", value, type(value))
            paths = self._dnd_paths(value)
        except Exception as e:
            logger.warning("DnD read_value failed: %s", e)
            paths = []
        opened = self._open_dropped(paths)
        gdk_drop.finish(Gdk.DragAction.COPY if opened else 0)

    def _dnd_paths(self, value):
        """Extract filesystem paths from whatever the drop delivered."""
        paths = []
        if isinstance(value, Gdk.FileList):
            paths = [f.get_path() for f in value.get_files()]
        elif isinstance(value, Gio.File):
            paths = [value.get_path()]
        elif isinstance(value, str):
            for line in value.splitlines():
                line = line.strip()
                if not line:
                    continue
                paths.append(Gio.File.new_for_uri(line).get_path()
                             if line.startswith("file://") else line)
        else:
            logger.warning("DnD: unhandled value type %s", type(value))
        return [p for p in paths if p]

    def _open_dropped(self, paths):
        """Open the first supported path; toast and return False otherwise.
        On a deck, a dropped image lands on the current slide instead."""
        logger.info("DnD: candidate paths = %s", paths)
        if self._deck_mode and self._deck_view is not None:
            added = False
            for path in paths:
                if path.lower().endswith(self.IMAGE_DND):
                    added = self._deck_view.add_image_file(path) or added
            if added:
                return True
        for path in paths:
            if path.lower().endswith(self.SUPPORTED_DND):
                logger.info("DnD: opening %s", path)
                self.open_file_in_tab(path)
                return True
        logger.info("DnD: no supported file among %s", paths)
        self.toast_overlay.add_toast(
            Adw.Toast.new("Drop a PDF, PPTX, or Markdown file"))
        return False

    def open_file(self, path):
        if self._dirty:
            self._ask_save_then(lambda: self._do_open_file(path))
        else:
            self._do_open_file(path)

    # files larger than this are unlikely to be text the user wants in the
    # notes panel; opening them is gated behind a confirmation
    MAX_TEXT_BYTES = 5 * 1024 * 1024

    def _do_open_file(self, path):
        if path.lower().endswith(".pptx"):
            self._convert_pptx_then_open(path)
            return
        if path.lower().endswith(".smdeck"):
            self._open_deck(path)
            return
        if path.lower().endswith((".md", ".markdown")):
            self._open_markdown(path)
            return
        if path.lower().endswith(".pdf"):
            self._open_pdf(path)
            return
        # any other file: interpret it as text/Markdown, but warn first if it
        # looks binary, isn't valid UTF-8, or is very large
        warning = self._text_open_warning(path)
        if warning:
            self._confirm_open_as_text(path, warning)
        else:
            self._open_markdown(path)

    def _text_open_warning(self, path):
        """Return a human-readable reason this file may not be text (so opening
        it as notes should be confirmed), or None if it looks fine."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size > self.MAX_TEXT_BYTES:
            return (f"This file is large ({size / 1024 / 1024:.1f} MB) — "
                    "opening it as text may be slow.")
        try:
            with open(path, "rb") as f:
                chunk = f.read(4096)
        except OSError:
            return None
        if b"\x00" in chunk:
            return "This file looks binary (it contains NUL bytes), not text."
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return ("This file isn't valid UTF-8 text and may display "
                    "with replacement characters.")
        return None

    def _confirm_open_as_text(self, path, warning):
        dialog = Adw.AlertDialog.new(
            "Open as text?",
            f"{os.path.basename(path)}\n\n{warning}\n\n"
            "Open it as a text / Markdown note anyway?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("open", "Open anyway")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, r: self._open_markdown(path) if r == "open" else None)
        dialog.present(self)

    def _open_pdf(self, path):
        self._leave_text_mode()
        self._leave_deck_mode()
        self._path = path
        self._notes_path = None
        self._active_notes_path = _notes_file_for_pdf(path) or notes_path_for(path)
        self._is_untitled = False
        self._set_file_title(os.path.basename(path), path)
        self.notes_model = NotesModel()
        self.notes_model.pdf_name = os.path.basename(path)
        self.notes_model.load(self._active_notes_path)
        self._hide_search()
        self.canvas.load(path)  # fires on_page_changed → _restore_note for page 0
        self._populate_toc()
        self._clear_dirty()
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._notes_burst_open = False
        # A PDF with no notes yet opens with the notes panel collapsed; it
        # springs open as soon as the user toggles it (Ctrl+\) and the .md is
        # created lazily on first save once something is actually written.
        self._set_notes_shown(self.notes_model.has_content())
        self._remember_recent(path)
        self._maybe_offer_recovery(path)
        self._maybe_offer_ocr(path)

    def _open_markdown(self, md_path):
        # If there's an associated PDF (e.g. lecture-notes.md → lecture.pdf), open it.
        pdf_path = None
        if md_path.endswith("-notes.md"):
            candidate = md_path[:-len("-notes.md")] + ".pdf"
            if os.path.exists(candidate):
                pdf_path = candidate
        if pdf_path:
            self.open_file(pdf_path)
            return
        # Text-first mode: no PDF — the page IS the note. The .md loads into
        # the sheet's editor; ink comes from the -ink.json sidecar if present.
        self._path = None
        self._notes_path = md_path
        self._set_file_title(os.path.basename(md_path), md_path)
        # the file is taken verbatim — no page markers, no stripping — so a
        # save round-trips byte-for-byte through external editors
        try:
            with open(md_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            raw = ""
        self.notes_model = NotesModel()
        self.notes_model.set(0, raw)
        self._page_label.set_label("—")
        self._enter_text_mode()
        buf = self._notes_view.get_buffer()
        buf.begin_irreversible_action()
        buf.set_text(raw)
        buf.end_irreversible_action()
        self._notes_view.reset_render_state()
        ink_file = _ink_path_for(md_path)
        if os.path.exists(ink_file):
            try:
                with open(ink_file, encoding="utf-8") as f:
                    self._text_page.load_ink(json.load(f))
            except (OSError, ValueError) as e:
                logger.warning("Could not load ink sidecar %s: %s", ink_file, e)
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._notes_burst_open = False
        self._burst_base = self.notes_model.get(0)
        self._clear_dirty()
        self._remember_recent(md_path)

    def _on_new_pdf(self, _btn):
        # New, like Open, originates from within the window: a fresh tab rather
        # than discarding the current document (reuse a pristine scratchpad tab).
        if not self._session_is_pristine(self._active_session):
            self._new_tab()
        self._create_blank()

    def _create_blank(self):
        """Open a blank A4 page — user will be prompted for a name on first save."""
        fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="sidemark_blank_")
        os.close(fd)
        surf = cairo.PDFSurface(tmp, 595, 842)
        cairo.Context(surf).show_page()
        surf.finish()
        self._do_open_file(tmp)
        self._path = tmp           # track temp file so canvas.save() works
        self._is_untitled = True
        self._set_file_title("Untitled")
        self._clear_dirty()

    def _on_new_text_page(self):
        """A fresh untitled text-first page (☰ New text page / Ctrl+Alt+N).
        No file exists yet — the save-as dialog names the .md on first save."""
        if not self._session_is_pristine(self._active_session):
            self._new_tab()
        self._path = None
        self._notes_path = None
        self._is_untitled = True
        self.notes_model = NotesModel()
        self._page_label.set_label("—")
        self._enter_text_mode()
        buf = self._notes_view.get_buffer()
        buf.begin_irreversible_action()
        buf.set_text("")
        buf.end_irreversible_action()
        self._notes_view.reset_render_state()
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._notes_burst_open = False
        self._burst_base = ""
        self._set_file_title("Untitled note")
        self._clear_dirty()

    def _on_new_presentation(self):
        """A fresh untitled slide deck (☰ New presentation / Ctrl+Alt+P).
        No file exists yet — the save-as dialog names the .smdeck on first save."""
        if not self._session_is_pristine(self._active_session):
            self._new_tab()
        self._path = None
        self._notes_path = None
        self._is_untitled = True
        self.notes_model = NotesModel()
        self._page_label.set_label("—")
        self._enter_deck_mode()
        self._deck_view.reset()
        self._restore_note()          # empty speaker notes for slide 1
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._set_file_title("Untitled presentation")
        self._set_notes_shown(False)  # panel springs open on Ctrl+\
        self._clear_dirty()

    def _open_deck(self, path):
        """Open a .smdeck presentation into this tab."""
        try:
            deck_mod = _deck_module()
            model = deck_mod.DeckModel.load(path)
        except (OSError, ValueError, KeyError) as e:
            self._show_error("Could not open presentation", str(e))
            return
        self._open_deck_model(model, os.path.basename(path), path=path)
        self._remember_recent(path)
        self._maybe_offer_deck_recovery(path)

    def _open_deck_model(self, model, title, path=None):
        """Mount a DeckModel into this tab. With `path` it is a saved .smdeck
        (titled, clean); without one it is an in-memory deck — e.g. a PPTX
        import — that opens untitled and dirty so its first save prompts for a
        .smdeck name."""
        self._path = None
        self._notes_path = None
        self._is_untitled = path is None
        self.notes_model = NotesModel()
        self._page_label.set_label("—")
        self._enter_deck_mode()
        self._deck_view.reset()
        self._deck_view.model = model
        self._deck_view._reset_view()
        self._deck_path = path
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._restore_note()
        self._set_file_title(title, path)
        # open with the notes panel only when the deck carries speaker notes
        self._set_notes_shown(any(s.get("notes") for s in model.slides))
        if path is None:
            self._mark_dirty()
        else:
            self._clear_dirty()

    def _save_deck(self):
        self._deck_view.save(self._deck_path)

    def _on_save_as_deck(self, after=None):
        """Name an untitled presentation."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save presentation as…")
        dialog.set_initial_name("presentation.smdeck")
        f = Gtk.FileFilter()
        f.set_name("Sidemark Deck files")
        f.add_pattern("*.smdeck")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)
        folder = self._current_dir_gfile()
        if folder:
            dialog.set_initial_folder(folder)

        def done(dlg, result):
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return
            if not file or not file.get_path():
                return
            path = file.get_path()
            if not path.endswith(".smdeck"):
                path += ".smdeck"
            try:
                self._commit_note()
                self._deck_view.save(path)
            except OSError as e:
                self._show_error("Save failed", str(e))
                return
            self._deck_path = path
            self._is_untitled = False
            self._set_file_title(os.path.basename(path), path)
            self._clear_dirty()
            self._remember_recent(path)
            toast = Adw.Toast.new("Saved")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
            if after:
                after()
        dialog.save(self, None, done)

    def _on_export_deck_pdf(self):
        """Render the deck's slides into a 16:9 PDF (☰ Export as PDF…)."""
        if not self._deck_mode or self._deck_view is None:
            return
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Export as PDF…")
        base = (os.path.splitext(os.path.basename(self._deck_path))[0]
                if self._deck_path else "presentation")
        dialog.set_initial_name(base + ".pdf")
        folder = self._current_dir_gfile()
        if folder:
            dialog.set_initial_folder(folder)

        def done(dlg, result):
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return
            if not file or not file.get_path():
                return
            path = file.get_path()
            if not path.endswith(".pdf"):
                path += ".pdf"
            try:
                self._deck_view.export_pdf(path)
            except Exception as e:
                self._show_error("Export failed", str(e))
                return
            toast = Adw.Toast.new(f"Exported {os.path.basename(path)}")
            toast.set_timeout(3)
            self.toast_overlay.add_toast(toast)
        dialog.save(self, None, done)

    def _open_scratchpad(self):
        """Open (or create) the persistent scratchpad — a text-first page at
        ~/.local/share/sidemark/scratchpad.md (ink in scratchpad-ink.json).
        Earlier versions used a scratchpad.pdf there; it stays on disk and can
        still be opened like any other PDF."""
        data_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "sidemark")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "scratchpad.md")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8"):
                pass
        self._do_open_file(path)
        self._set_file_title("Scratchpad", path)
        self._clear_dirty()

    def _save_text_ink(self):
        """Write the text-first page's ink sidecar next to its .md. Lazy like
        the notes file: only once there is ink (or a sidecar already exists,
        so erasing every stroke still persists)."""
        if not self._text_mode or not self._notes_path:
            return
        tp = self._active_session._text_page
        if tp is None:
            return
        ink_file = _ink_path_for(self._notes_path)
        if tp.strokes or os.path.exists(ink_file):
            with open(ink_file, "w", encoding="utf-8") as f:
                json.dump(tp.ink_to_json(), f)

    def _on_save_as_md(self, after=None):
        """Name an untitled text-first page (its ink sidecar follows along)."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save note as…")
        dialog.set_initial_name("note.md")
        f = Gtk.FileFilter()
        f.set_name("Markdown files")
        f.add_pattern("*.md")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return   # dialog dismissed by user
            if not file:
                return
            path = file.get_path()
            if not path.lower().endswith((".md", ".markdown")):
                path += ".md"
            self._notes_path = path
            self._is_untitled = False
            self._set_file_title(os.path.basename(path), path)
            self._remember_recent(path)
            self._on_save(after=after)

        dialog.save(self, None, done)

    def _on_export_text_pdf(self):
        """Export the text-first page — rendered Markdown plus ink — to PDF."""
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Export as PDF…")
        base = (os.path.splitext(os.path.basename(self._notes_path))[0]
                if self._notes_path else "note")
        dialog.set_initial_name(base + ".pdf")
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)

        def done(d, result):
            try:
                file = d.save_finish(result)
            except GLib.Error:
                return   # dialog dismissed by user
            if not file:
                return
            path = file.get_path()
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            try:
                n = self._write_text_pdf(path)
            except (OSError, GLib.Error, cairo.Error) as e:
                logger.exception("Text-page PDF export failed")
                self._toast(f"Export failed: {e}")
                return
            self._toast(f"Exported {n} page{'s' if n != 1 else ''} "
                        f"→ {os.path.basename(path)}")

        dialog.save(self, None, done)

    def _write_text_pdf(self, out_path):
        """Write the sheet to an A4 PDF: the rendered page as a 2× raster
        (crisp text, exactly what's on screen) sliced at display-line
        boundaries, with the ink drawn on top as vectors. Returns the page
        count."""
        tp = self._active_session._text_page
        view = tp.view
        w, h = view.get_width(), view.get_height()
        if w < 1 or h < 1:
            raise OSError("the page has no layout yet")
        scale = 2
        paintable = Gtk.WidgetPaintable.new(view)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, w * scale, h * scale)
        node = snapshot.to_node()
        # a just-presented window may not have produced a frame yet, which
        # leaves the paintable empty — pump the loop briefly and retry
        deadline = time.time() + 2.0
        while node is None and time.time() < deadline:
            GLib.MainContext.default().iteration(False)
            time.sleep(0.01)
            snapshot = Gtk.Snapshot()
            paintable.snapshot(snapshot, w * scale, h * scale)
            node = snapshot.to_node()
        if node is None:
            raise OSError("nothing to render")
        renderer = self.get_native().get_renderer()
        pw, ph = 595.0, 842.0                    # A4 in PDF points
        page_px = ph / pw * w                    # sheet px per PDF page
        offs = tp.page_break_offsets(page_px)
        pt_per_px = pw / w
        strokes = [(tp.stroke_view_pts(st), st) for st in tp.strokes]
        surf = cairo.PDFSurface(out_path, pw, ph)
        ctx = cairo.Context(surf)
        for y0, y1 in zip(offs, offs[1:]):
            # white paper first — the last slice is shorter than a full page
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            tex = renderer.render_texture(node, Graphene.Rect().init(
                0, y0 * scale, w * scale, max((y1 - y0) * scale, 1)))
            png = tex.save_to_png_bytes()
            img = cairo.ImageSurface.create_from_png(
                io.BytesIO(png.get_data()))
            ctx.save()
            ctx.scale(pt_per_px / scale, pt_per_px / scale)
            ctx.set_source_surface(img, 0, 0)
            ctx.paint()
            ctx.restore()
            # ink as vectors, clipped to this page (strokes may span pages)
            ctx.save()
            ctx.rectangle(0, 0, pw, ph)
            ctx.clip()
            ctx.scale(pt_per_px, pt_per_px)
            ctx.translate(0, -y0)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            for pts, st in strokes:
                if len(pts) < 2:
                    continue
                f = tp.font_px / max(st["font_px"], 1)
                ctx.set_source_rgba(*st["color"], st["opacity"])
                ctx.set_line_width(st["width"] * f)
                ctx.move_to(*pts[0])
                for p in pts[1:]:
                    ctx.line_to(*p)
                ctx.stroke()
            ctx.restore()
            ctx.show_page()
        surf.finish()
        return len(offs) - 1

    def _on_save_as(self, after=None):
        if self._text_mode:
            self._on_save_as_md(after=after)
            return
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Save PDF as…")
        default_name = os.path.basename(self._path) if self._path else "notes.pdf"
        dialog.set_initial_name(default_name)
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, lambda d, r: self._save_as_done(d, r, after))

    def _save_as_done(self, dialog, result, after=None):
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return   # dialog dismissed by user
        if not file:
            return
        try:
            path = file.get_path()
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            import shutil
            shutil.copy2(self._path, path)   # copy blank/temp as starting point
            old_tmp = self._path if self._is_untitled else None
            self._path = path
            self._is_untitled = False
            self._set_file_title(os.path.basename(path), path)
            self._remember_recent(path)
            self._on_save(after=after)
            if old_tmp and os.path.exists(old_tmp):
                try:
                    os.unlink(old_tmp)
                except OSError:
                    pass
        except Exception as e:
            self._show_error("Could not save", str(e))

    # ── Export ────────────────────────────────────────────────────────────────

    def _on_export(self):
        if not self._path:
            self._show_error("Export failed", "No PDF is open.")
            return
        # The export reads the PDF from disk and the notes from the model:
        # commit the current page's note and offer to save unsaved changes
        # first, otherwise they would silently be missing from the export.
        self._commit_note()
        if self._dirty:
            dlg = Adw.AlertDialog.new(
                "Save before exporting?",
                "The export is created from the last saved version. "
                "Unsaved changes will not be included.",
            )
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("export", "Export without saving")
            dlg.add_response("save",   "Save and export")
            dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dlg.set_default_response("save")
            dlg.set_close_response("cancel")

            def on_response(d, r):
                if r == "save":
                    self._on_save(after=self._show_export_options)
                elif r == "export":
                    self._show_export_options()

            dlg.connect("response", on_response)
            dlg.present(self)
            return
        self._show_export_options()

    def _show_export_options(self):
        group = Gtk.CheckButton(label="Group small notes together")
        group.set_active(True)
        empty = Gtk.CheckButton(label="Include pages with no notes")
        empty.set_active(False)
        # "Include pages with no notes" only makes sense one-page-per-page;
        # grouping packs notes densely and never emits an empty notes page.
        empty.set_sensitive(not group.get_active())
        group.connect("toggled",
                      lambda b: empty.set_sensitive(not b.get_active()))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.append(group)
        box.append(empty)

        dlg = Adw.AlertDialog(
            heading="Export with notes",
            body=("Notes pages carry only what isn't already on the page "
                  "(callouts, text boxes and empty anchors are drawn in place). "
                  "Grouping packs short notes from several pages together, each "
                  "labelled with the page it came from."),
            extra_child=box,
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("export", "Choose file…")
        dlg.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("export")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._export_options_response, group, empty)
        dlg.present(self)

    def _export_options_response(self, dlg, response, group, empty):
        if response != "export":
            return
        include_empty = empty.get_active()
        do_group = group.get_active()
        file_dlg = Gtk.FileDialog.new()
        file_dlg.set_title("Export PDF as…")
        base = os.path.splitext(os.path.basename(self._path))[0]
        file_dlg.set_initial_name(base + "-annotated.pdf")
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        file_dlg.set_filters(store)
        file_dlg.save(self, None,
                      lambda d, r: self._export_file_done(d, r, include_empty, do_group))

    def _export_file_done(self, dialog, result, include_empty, do_group):
        try:
            gfile = dialog.save_finish(result)
            if not gfile:
                return
            path = gfile.get_path()
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
        except Exception:
            return

        toast = Adw.Toast.new("Exporting…")
        toast.set_timeout(0)
        self.toast_overlay.add_toast(toast)

        accent = self.canvas.zoom_accent

        def run():
            try:
                _export_pdf_with_notes(self._path, path, self.notes_model,
                                       include_empty, accent, group=do_group)
                GLib.idle_add(lambda: (
                    toast.dismiss(),
                    self.toast_overlay.add_toast(
                        Adw.Toast.new(f"Exported: {os.path.basename(path)}")
                    )) and None)
            except Exception as e:
                msg = str(e)
                tb = traceback.format_exc()
                GLib.idle_add(lambda: (
                    toast.dismiss(),
                    self._show_error("Export failed", msg, tb)) and None)

        threading.Thread(target=run, daemon=True).start()

    # Render each converted PDF page this wide (px) before embedding it as a
    # slide picture — comfortably crisp on a 1080p projector without bloating
    # the .smdeck (a 16:9 page lands ~1600×900).
    PPTX_IMPORT_WIDTH = 1600

    def _convert_pptx_then_open(self, pptx_path):
        """Import a PowerPoint file as an editable Sidemark Deck: LibreOffice
        renders it to PDF, each page is rasterized to a full-bleed slide picture,
        and the .pptx's speaker notes ride along in the notes panel. The slides
        are images (original text isn't editable as text — a deliberate MVP; see
        ideas.csv), but the deck is otherwise fully real: reorder, annotate with
        ink/text boxes, present with F5, export back to PDF."""
        toast = Adw.Toast.new(f"Importing {os.path.basename(pptx_path)}…")
        toast.set_timeout(0)
        self.toast_overlay.add_toast(toast)
        out_dir = tempfile.mkdtemp(prefix="sidemark-")
        base = os.path.splitext(os.path.basename(pptx_path))[0]

        def run():
            try:
                subprocess.run(
                    ["libreoffice", "--headless", "--convert-to", "pdf",
                     "--outdir", out_dir, pptx_path],
                    check=True, capture_output=True,
                )
                pdf_path = os.path.join(out_dir, base + ".pdf")
                # Slide notes live in the .pptx (the rendered PDF doesn't carry
                # them); pull them so they can ride into the deck's slides.
                slide_notes = _extract_pptx_notes(pptx_path)
                images = self._rasterize_pdf_slides(pdf_path, slide_notes)
                title = base + " (imported)"
                GLib.idle_add(lambda: (toast.dismiss(),
                                       self._open_imported_deck(images, title)) and None)
            except FileNotFoundError:
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("Import failed",
                        "LibreOffice not found. Install it with:\n  pacman -S libreoffice-still")) and None)
            except subprocess.CalledProcessError as e:
                msg = e.stderr.decode(errors="replace") if e.stderr else str(e)
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("Import failed", msg)) and None)
            except Exception as e:
                logger.exception("pptx import failed")
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("Import failed", str(e))) and None)
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

        threading.Thread(target=run, daemon=True).start()

    def _rasterize_pdf_slides(self, pdf_path, slide_notes):
        """Render every page of the converted PDF to a PNG for `deck_from_images`,
        returning [(png_bytes, w, h, notes)] in page order. Runs off the main
        thread; touches only PyMuPDF, so it needs no GTK."""
        # LibreOffice writes a tagged (accessibility) structure tree that MuPDF
        # dislikes — rendering each page prints a harmless "No common ancestor in
        # structure tree" to stderr per page. The pixmaps are correct, so mute
        # MuPDF's stderr chatter for the duration and restore it afterwards.
        prev = fitz.TOOLS.mupdf_display_errors()   # read (setter returns the new value)
        fitz.TOOLS.mupdf_display_errors(False)
        images = []
        try:
            with fitz.open(pdf_path) as doc:
                for i, page in enumerate(doc):
                    zoom = self.PPTX_IMPORT_WIDTH / max(page.rect.width, 1)
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                          alpha=False)
                    images.append((pix.tobytes("png"), pix.width, pix.height,
                                   slide_notes.get(i, "")))
        finally:
            fitz.TOOLS.mupdf_display_errors(prev)
        return images

    def _open_imported_deck(self, images, title):
        """Mount an in-memory deck built from imported slide pictures as a fresh,
        untitled deck (saved as .smdeck on first Ctrl+S)."""
        deck_mod = _deck_module()
        model = deck_mod.deck_from_images(images)
        self._open_deck_model(model, title)
        n = len(model.slides)
        self._toast(f"Imported {n} slide{'s' if n != 1 else ''}")

    # ── OCR (optional: needs the external 'ocrmypdf' tool) ──────────────────────
    def _maybe_offer_ocr(self, path):
        """If a just-opened PDF looks scanned, offer to add a searchable text layer."""
        if path in self._ocr_seen or not _pdf_needs_ocr(path):
            return
        self._ocr_seen.add(path)
        if shutil.which("ocrmypdf"):
            toast = Adw.Toast.new("This document looks scanned — no searchable text.")
            toast.set_button_label("Add text layer")
            toast.set_timeout(8)
            toast.connect("button-clicked", lambda _t: self._ocr_document(path))
            self.toast_overlay.add_toast(toast)
        elif not self._ocr_hint_shown:
            self._ocr_hint_shown = True
            self.toast_overlay.add_toast(Adw.Toast.new(
                "Scanned document — install ‘ocrmypdf’ to make it searchable."))

    def _ocr_current(self):
        """OCR the document in the current tab (menu action)."""
        if not self._path or self._is_untitled or not self._path.lower().endswith(".pdf"):
            self._toast("Open a PDF first to add a text layer.")
            return
        self._ocr_document(self._path)

    def _ocr_document(self, path):
        if not shutil.which("ocrmypdf"):
            self._show_error("OCR unavailable",
                "The 'ocrmypdf' tool is not installed.\n\n"
                "Install it with:\n  pacman -S ocrmypdf")
            return
        s = self._active_session
        if s.canvas.document is None:
            self._toast("Open a PDF first to add a text layer.")
            return
        toast = Adw.Toast.new(f"Running OCR on {os.path.basename(path)}…")
        toast.set_timeout(0)
        self.toast_overlay.add_toast(toast)
        out_dir = tempfile.mkdtemp(prefix="sidemark-ocr-")
        # OCR the document's *current* state (any strokes/page edits baked in),
        # not the on-disk original, so nothing drawn so far is lost.
        in_path = os.path.join(out_dir, "input.pdf")
        out_path = os.path.join(out_dir, os.path.basename(path))
        try:
            s.canvas.save_copy(in_path)
        except Exception as e:
            toast.dismiss()
            self._show_error("OCR failed", str(e))
            return

        def run():
            try:
                subprocess.run(
                    ["ocrmypdf", "--skip-text", in_path, out_path],
                    check=True, capture_output=True,
                )
                GLib.idle_add(lambda: (
                    toast.dismiss(),
                    self._apply_ocr_result(s, path, out_path)) and None)
            except FileNotFoundError:
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("OCR failed",
                        "ocrmypdf not found. Install it with:\n  pacman -S ocrmypdf")) and None)
            except subprocess.CalledProcessError as e:
                msg = e.stderr.decode(errors="replace") if e.stderr else str(e)
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("OCR failed", msg)) and None)

        threading.Thread(target=run, daemon=True).start()

    def _apply_ocr_result(self, s, original_path, out_path):
        """Swap the searchable PDF into the document that was OCR'd while keeping
        its identity: the same notes sidecar and the same save target. OCR only
        adds a text layer, so the notes must NOT be reloaded from a new path."""
        if s not in self._sessions:
            return   # the tab was closed while OCR ran in the background
        # the canvas callbacks act on the active session, so focus the OCR'd one
        if s is not self._active_session and s._tab_page is not None:
            self._tab_view.set_selected_page(s._tab_page)
        self._commit_note()
        self.canvas.load(out_path)        # in-memory doc is now searchable
        self._path = original_path        # but we still save to the original file
        self._ocr_seen.add(original_path)
        self._populate_toc()
        self._restore_note()              # notes are untouched — re-show them
        self._mark_dirty()                # save writes the text layer to disk
        self._toast("Added a searchable text layer — save to keep it.")

    # ── share to phone (LAN HTTP + QR, #62) ─────────────────────────────────────
    def _on_share_to_phone(self):
        if self.canvas.document is None:
            self._toast("Open a PDF first to share it.")
            return
        self._commit_note()
        # Show the dialog *immediately* with a spinner; the slow parts
        # (baking the PDF, starting the server, rendering QR codes) run in a
        # worker thread so the button feels responsive.
        self._show_share_dialog()

    @staticmethod
    def _share_prepare(out_dir, server, name):
        """Worker-thread body: start the (already-built) server and render the QR
        PNGs. Returns (server, entries) or raises. Each entry is a dict with a
        caption and either a ready QR path + url, or a hint string. Nothing is
        baked up front — the live server renders/bakes on demand."""
        server.start()

        entries = []
        lan_url = server.url_for(server.ip)
        lan_qr = os.path.join(out_dir, "qr-lan.png")
        entries.append({"caption": "Same Wi-Fi", "url": lan_url,
                        "qr": lan_qr if _make_qr_png(lan_url, lan_qr) else None})

        ts = _tailscale_ip()
        if ts and ts != server.ip:
            ts_url = server.url_for(ts)
            ts_qr = os.path.join(out_dir, "qr-ts.png")
            entries.append({"caption": "Over Tailscale", "url": ts_url,
                            "qr": ts_qr if _make_qr_png(ts_url, ts_qr) else None})
        else:
            if shutil.which("tailscale"):
                hint = ("Tailscale is installed but not connected. Run "
                        "“tailscale up” here and add the Tailscale app to your "
                        "phone to share securely from any network.")
            else:
                hint = ("Set up Tailscale on both devices to share securely "
                        "from anywhere — even when the Wi-Fi blocks the direct "
                        "link (repeaters, guest networks, AP isolation).")
            entries.append({"caption": "Over Tailscale", "hint": hint})
        return server, entries

    def _share_entry(self, entry):
        """One column of the share dialog: a QR (if available) plus the link as
        selectable text, or an explanatory hint, under a caption."""
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        col.set_hexpand(True)
        head = Gtk.Label(label=entry["caption"])
        head.add_css_class("heading")
        col.append(head)
        if "url" in entry:
            if entry.get("qr"):
                pic = Gtk.Picture.new_for_filename(entry["qr"])
                pic.set_can_shrink(False)
                pic.set_size_request(200, 200)
                col.append(pic)
            else:
                hint = Gtk.Label(label="Install ‘qrencode’ for a scannable code.")
                hint.add_css_class("dim-label")
                hint.set_wrap(True)
                col.append(hint)
            link = Gtk.Label(label=entry["url"])
            link.set_selectable(True)
            link.set_wrap(True)
            link.set_max_width_chars(28)
            link.add_css_class("monospace")
            link.add_css_class("caption")
            col.append(link)
        else:
            spacer = Gtk.Box()
            spacer.set_size_request(200, 200)
            icon = Gtk.Image.new_from_icon_name("network-vpn-symbolic")
            icon.set_pixel_size(48)
            icon.set_vexpand(True)
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("dim-label")
            spacer.append(icon)
            col.append(spacer)
            hint = Gtk.Label(label=entry["hint"])
            hint.add_css_class("dim-label")
            hint.set_wrap(True)
            hint.set_max_width_chars(28)
            hint.set_justify(Gtk.Justification.CENTER)
            col.append(hint)
        return col

    def _render_share_page(self, canvas, notes_model, accent, path):
        """Render the document's current page to a PNG for the live phone view —
        with ink, text boxes, callouts and anchor circles drawn in, exactly like
        the export. Runs on the main thread (fitz objects belong to the UI) and
        works on a one-page *copy* so the live document is never modified."""
        canvas._write_ink_annotations()          # sync live strokes into the page
        idx = canvas.current_page_idx
        out = fitz.open()
        try:
            out.insert_pdf(canvas.document, from_page=idx, to_page=idx)
            page = out[0]
            _draw_page_marks(page, _symbolize(notes_model.get(idx)), accent)
            zoom = 1500.0 / max(page.rect.width, 1)   # ~1500px is crisp on phones
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(path)
        finally:
            out.close()

    def _show_share_dialog(self):
        out_dir = tempfile.mkdtemp(prefix="sidemark-share-")
        # Bind to the document that's active *now*, so the live view keeps
        # following it even if the user switches tabs while sharing.
        canvas = self.canvas
        save_copy = canvas.save_copy
        notes_model = self.notes_model
        accent = canvas.zoom_accent
        name = (os.path.basename(self._path)
                if (self._path and not self._is_untitled) else "document.pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"

        def bake(out_path):
            # The full annotated export (ink + grouped notes/anchors/callouts/
            # text boxes) served behind the Download button.
            ink = out_path + ".ink.pdf"
            save_copy(ink)
            try:
                _export_pdf_with_notes(ink, out_path, notes_model,
                                       include_empty=False, accent=accent,
                                       group=True)
            finally:
                try:
                    os.remove(ink)
                except OSError:
                    pass

        providers = {
            "title": name,
            # cheap int reads — safe to call straight from the server thread
            "state": lambda: (self._share_revision,
                              canvas.current_page_idx, canvas.n_pages),
            # these touch the document, so hop to the main thread and block
            "render": lambda p: _run_on_main(
                lambda: self._render_share_page(canvas, notes_model, accent, p)),
            "pdf": lambda p: _run_on_main(lambda: bake(p)),
        }
        server = _ShareServer(providers=providers)

        # A *non-modal* companion window (not an AlertDialog) so you can keep
        # drawing on / flipping through the PDF while the phone follows along —
        # the whole point of the live view. Set it aside and work behind it.
        win = Adw.Window()
        self._share_window = win   # kept for testing / single-instance reuse
        win.set_title("Sharing to phone")
        win.set_transient_for(self)
        win.set_modal(False)
        win.set_default_size(520, 420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)
        toolbar.set_content(body)
        win.set_content(toolbar)

        def _clear(box):
            child = box.get_first_child()
            while child is not None:
                box.remove(child)
                child = box.get_first_child()

        def _show_loading():
            _clear(body)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_vexpand(True)
            box.set_valign(Gtk.Align.CENTER)
            box.set_halign(Gtk.Align.CENTER)
            spinner = Gtk.Spinner()
            spinner.set_size_request(48, 48)
            spinner.start()
            box.append(spinner)
            msg = Gtk.Label(label="Preparing a link for your phone…")
            msg.add_css_class("dim-label")
            box.append(msg)
            body.append(box)
        _show_loading()

        stopped = {"v": False}

        def _cleanup(*_a):
            if stopped["v"]:
                return False
            stopped["v"] = True
            server.stop()      # safe even if it never started; also rmtree's tmp
            shutil.rmtree(out_dir, ignore_errors=True)
            return False
        win.connect("close-request", _cleanup)
        win.present()

        def _ready(srv, entries):
            _clear(body)
            intro = Gtk.Label(
                label="Scan to open a <b>live view</b> on your phone — it follows "
                      "along as you draw and flip pages. Tap <b>Download</b> there "
                      "for the full annotated PDF. Keep this window open while you "
                      "work; closing it stops sharing.")
            intro.set_use_markup(True)
            intro.set_wrap(True)
            intro.set_xalign(0)
            body.append(intro)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
            row.set_margin_top(6)
            row.set_homogeneous(True)
            for entry in entries:
                row.append(self._share_entry(entry))
            body.append(row)
            live = Gtk.Label(label="●  Live — your phone mirrors this document")
            live.add_css_class("dim-label")
            live.add_css_class("caption")
            live.set_margin_top(4)
            body.append(live)
            # safety net: stop serving after 10 minutes even if left open
            GLib.timeout_add_seconds(
                600, lambda: (_cleanup(), win.close(), False)[2])
            return False

        def _failed(message):
            _clear(body)
            shutil.rmtree(out_dir, ignore_errors=True)
            lbl = Gtk.Label(label=f"Could not prepare the share:\n{message}")
            lbl.set_wrap(True)
            lbl.set_xalign(0)
            body.append(lbl)
            return False

        def _worker():
            try:
                srv, entries = self._share_prepare(out_dir, server, name)
            except Exception as e:                          # noqa: BLE001
                GLib.idle_add(_failed, str(e))
                return
            GLib.idle_add(_ready, srv, entries)

        threading.Thread(target=_worker, daemon=True).start()

    def _current_dir_gfile(self):
        """The folder of the currently open file, so file dialogs start there."""
        cur = (self._path if (self._path and not self._is_untitled)
               else self._notes_path or self._deck_path)
        if cur:
            folder = os.path.dirname(os.path.abspath(cur))
            if os.path.isdir(folder):
                return Gio.File.new_for_path(folder)
        return None

    def _on_open(self, _btn):
        dialog = Gtk.FileDialog.new()
        docs = Gtk.FileFilter()
        docs.set_name("Documents (PDF, PPTX, Markdown, text, Deck)")
        for pat in ("*.pdf", "*.pptx", "*.md", "*.markdown", "*.txt",
                    "*.smdeck"):
            docs.add_pattern(pat)
        any_file = Gtk.FileFilter()
        any_file.set_name("All files")
        any_file.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(docs)
        filters.append(any_file)
        dialog.set_filters(filters)
        dialog.set_default_filter(docs)
        folder = self._current_dir_gfile()
        if folder:
            dialog.set_initial_folder(folder)
        dialog.open(self, None, self._open_done)

    def _open_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error as e:
            # Escape / cancel raises gtk-dialog-error-quark DISMISSED — not an error
            if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                self._show_error("Could not open file", e.message)
            return
        if not file:
            return
        try:
            self.open_file_in_tab(file.get_path())
        except Exception as e:
            self._show_error("Could not open file", str(e))

    def _on_save(self, _btn=None, after=None):
        """Save; if `after` is given it runs only on a successful save
        (for untitled files that means after the save-as dialog completed)."""
        if self._deck_mode:
            self._commit_note()   # speaker notes live inside the .smdeck
            if self._is_untitled or not self._deck_path:
                self._on_save_as_deck(after=after)
                return
            try:
                self._save_deck()
                self._clear_dirty()
                toast = Adw.Toast.new("Saved")
                toast.set_timeout(2)
                self.toast_overlay.add_toast(toast)
            except OSError as e:
                self._show_error("Save failed", str(e))
                return
            _discard_autosave(self._deck_path)   # changes are on disk now
            if after:
                after()
            return
        if self._is_untitled:
            if self._text_mode:
                self._on_save_as_md(after=after)
            else:
                self._on_save_as(after=after)
            return
        notes_file = self._current_notes_path()
        if not self._path and not notes_file:
            return
        try:
            self._commit_note()
            if self._path:
                self.canvas.save(self._path)
            # lazy-create: don't bring an empty sidecar into existence just
            # because the PDF was saved — only write notes once there's content
            # (or the file already exists, so edits/clears still persist)
            if self._text_mode:
                # text-first page: the .md is the document, written verbatim —
                # no page markers, so it stays pure Markdown
                if notes_file:
                    tmp_file = notes_file + ".tmp"
                    with open(tmp_file, "w", encoding="utf-8") as f:
                        f.write(self.notes_model.get(0))
                    os.replace(tmp_file, notes_file)
            elif notes_file and (self.notes_model.has_content()
                                 or os.path.exists(notes_file)):
                self.notes_model.save(notes_file)
            self._save_text_ink()
            self._clear_dirty()
            toast = Adw.Toast.new("Saved")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
        except Exception as e:
            self._show_error("Save failed", str(e))
            return
        if self._path:
            _discard_autosave(self._path)   # changes are on disk now
        if after:
            after()

    def _reload(self):
        if not self._path:
            return
        page = self.canvas.current_page_idx
        def do_reload():
            # Spawn a *standalone* process (SIDEMARK_STANDALONE bypasses the
            # single instance) so the reload actually re-reads the code from
            # disk instead of forwarding to this still-running process.
            env = dict(os.environ, SIDEMARK_STANDALONE="1")
            subprocess.Popen([sys.executable, os.path.abspath(__file__),
                              self._path, "--page", str(page)], env=env)
            self.destroy()
        if self._dirty:
            self._ask_save_then(do_reload)
        else:
            do_reload()

    def _on_key(self, ctrl, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_r:
                self._reload()
                return True
            if keyval == Gdk.KEY_f:
                self._show_search()
                return True
            if keyval == Gdk.KEY_h:
                self._toggle_highlighter()
                return True
            if keyval == Gdk.KEY_m:
                self._toggle_select_mode()
                return True
            if keyval == Gdk.KEY_e:
                self._on_export()
                return True
            if keyval == Gdk.KEY_s:
                self._on_save()
                return True
            if keyval == Gdk.KEY_o:
                self._on_open(None)
                return True
            if keyval == Gdk.KEY_n and not (state & Gdk.ModifierType.SHIFT_MASK):
                if state & Gdk.ModifierType.ALT_MASK:
                    self._on_new_text_page()
                else:
                    self._on_new_pdf(None)
                return True
            if keyval == Gdk.KEY_p and state & Gdk.ModifierType.ALT_MASK:
                self._on_new_presentation()
                return True
            if keyval == Gdk.KEY_c:
                if self.canvas._selected_words and not self._notes_view.has_focus():
                    self.canvas.copy_selection()
                    return True
            if keyval == Gdk.KEY_z:
                self._global_undo()
                return True
            if keyval == Gdk.KEY_t:
                # without a TOC the toggle handler bounces and shows a toast
                self._toc_btn.set_active(not self._toc_btn.get_active())
                return True
            if (state & Gdk.ModifierType.SHIFT_MASK) and keyval == Gdk.KEY_S:
                self._on_save_as()
                return True
            if (state & Gdk.ModifierType.SHIFT_MASK) and keyval == Gdk.KEY_N:
                self._add_blank_page()
                return True
            if (state & Gdk.ModifierType.SHIFT_MASK) and keyval == Gdk.KEY_Delete:
                self._delete_current_page()
                return True
        if keyval == Gdk.KEY_F5:
            self._present_btn.set_active(not self._present_btn.get_active())
            return True
        # lasso selection: Delete removes it, Escape drops it (only when the notes
        # editor isn't focused, so typing in notes is never affected)
        if (self.canvas.has_lasso_selection()
                and not self._notes_view.has_focus()):
            if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
                self.canvas.delete_selected_strokes()
                return True
            if keyval == Gdk.KEY_Escape:
                self.canvas.clear_lasso_selection()
                return True
        # PageUp/PageDown and Ctrl+\ are handled in _on_global_key (capture phase)
        # so they work even when the notes editor has focus.
        return False


    # ── search ────────────────────────────────────────────────────────────────

    def _show_search(self):
        self._search_revealer.set_reveal_child(True)
        self._search_entry.grab_focus()

    def _hide_search(self):
        self._search_revealer.set_reveal_child(False)
        self._search_entry.set_text("")
        self._search_hits = {}
        self._note_hits = {}
        self._search_matches = []
        self._search_current = -1
        self._search_label.set_label("")
        self.canvas.search_rects = []
        self.canvas.search_current_rect = None
        self.canvas.queue_draw()

    def _on_search_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Up:
            self._search_prev()
            return True
        if keyval == Gdk.KEY_Down:
            self._search_next()
            return True
        return False

    def _on_search_changed(self, entry):
        query = entry.get_text()
        self._search_hits = {}
        self._note_hits = {}
        self._search_matches = []
        self._search_current = -1
        if not query:
            self._search_label.set_label("")
            self.canvas.search_rects = []
            self.canvas.search_current_rect = None
            self.canvas.queue_draw()
            return
        # Flush the open page's edits so the notes search sees current text.
        self._commit_note()
        # PDF hits per page
        if self.canvas.document:
            for i in range(self.canvas.n_pages):
                hits = self.canvas.document[i].search_for(query)
                if hits:
                    self._search_hits[i] = hits
        # Notes hits per page (case-insensitive substring on the stored text)
        self._note_hits = self._find_note_matches(query)
        # Unified list, ordered by page; within a page PDF hits then note hits
        for i in sorted(set(self._search_hits) | set(self._note_hits)):
            for j in range(len(self._search_hits.get(i, []))):
                self._search_matches.append(("pdf", i, j))
            for (s, e) in self._note_hits.get(i, []):
                self._search_matches.append(("note", i, s, e))
        if not self._search_matches:
            self._search_label.set_label("0 / 0")
            self._search_entry.add_css_class("error")
            self.canvas.search_rects = []
            self.canvas.search_current_rect = None
            self.canvas.queue_draw()
            return
        self._search_entry.remove_css_class("error")
        # Start from the first match on or after the current page (wraps after)
        cur = self.canvas.current_page_idx
        start = next((k for k, m in enumerate(self._search_matches) if m[1] >= cur), 0)
        self._go_to_match(start)

    def _find_note_matches(self, query):
        """{page_idx: [(start, end), ...]} of query occurrences in the stored
        per-page notes text (case-insensitive)."""
        q = query.lower()
        out = {}
        for page, text in self.notes_model._notes.items():
            low = text.lower()
            spans, pos = [], low.find(q)
            while pos != -1:
                spans.append((pos, pos + len(query)))
                pos = low.find(q, pos + 1)
            if spans:
                out[page] = spans
        return out

    def _search_next(self):
        if not self._search_matches:
            return
        self._go_to_match(self._search_current + 1)

    def _search_prev(self):
        if not self._search_matches:
            return
        self._go_to_match(self._search_current - 1)

    def _go_to_match(self, idx):
        n = len(self._search_matches)
        if n == 0:
            return
        self._search_current = idx % n
        match = self._search_matches[self._search_current]
        page_idx = match[1]
        if page_idx != self.canvas.current_page_idx and self.canvas.document:
            self._commit_note()
            self.canvas.go_to_page(page_idx)  # fires _on_page_changed → _update_search_canvas
        else:
            self._update_search_canvas()
        if match[0] == "note":
            self._select_note_match(match[2], match[3])
        self._search_label.set_label(f"{self._search_current + 1} / {n}")

    def _select_note_match(self, start, end):
        """Select a notes hit. _restore_note loads the raw stored text into the
        buffer synchronously (before rehighlight substitutes \\alpha→α on
        non-cursor lines), so the model offsets map exactly."""
        self._restore_note()
        buf = self._notes_view.get_buffer()
        s = buf.get_iter_at_offset(start)
        e = buf.get_iter_at_offset(end)
        buf.select_range(s, e)   # insert at start → that line becomes the cursor line
        self._notes_view.scroll_to_iter(s, 0.1, False, 0.0, 0.5)

    def _update_search_canvas(self):
        page_idx = self.canvas.current_page_idx
        self.canvas.search_rects = list(self._search_hits.get(page_idx, []))
        cur = (self._search_matches[self._search_current]
               if self._search_current >= 0 and self._search_matches else None)
        if cur and cur[0] == "pdf" and cur[1] == page_idx:
            _, pi, ri = cur
            self.canvas.search_current_rect = self._search_hits[pi][ri]
        else:
            self.canvas.search_current_rect = None
        self.canvas.queue_draw()


class PresenterWindow(Adw.Window):
    """A view-only mirror of the editor canvas for a second screen / projector:
    fullscreen, no header or notes — just the current page with live ink. Shares
    the editor's document and stroke dict by reference (re-pointed on every sync,
    so structural edits are picked up) but keeps its own fit-to-page view, so the
    editor can zoom in to work on a slide while the audience still sees it whole.

    Kept deliberately bare: the presentation timer and the large prev/next
    controls live on the editor window (the presenter's own screen), not here on
    the projected slide. It still pages when focused, though — click / Space /
    arrows / PageUp/Down / mouse side buttons all navigate (clicker-friendly),
    driving the editor via on_nav so both windows stay in step.
    """

    def __init__(self, app, src_canvas, on_nav=None):
        super().__init__(application=app)
        self.set_title("Sidemark — Presenter")
        self._src = src_canvas
        self._on_nav = on_nav   # callback(delta) — drives the editor's _nav_page
        canvas = PDFCanvas(interactive=False)
        canvas.surround_color = (0.0, 0.0, 0.0)   # black surround for projection
        canvas.zoom_accent = src_canvas.zoom_accent
        # draw the editor's in-progress stroke too, so ink appears while it's
        # being laid down (the editor pings us via on_live_draw per motion)
        canvas.live_stroke_src = src_canvas
        src_canvas.on_live_draw = canvas.queue_draw
        self.canvas = canvas
        self.set_content(canvas)
        self.sync_page()

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        # While this window has focus (e.g. a clicker driving the projected
        # screen), it can still page: click advances, the mouse back/forward
        # side buttons flip like in the editor (8 next, 9 prev).
        click = Gtk.GestureClick()
        click.set_button(0)   # 0 = listen to every button
        click.connect("pressed", self._on_click)
        canvas.add_controller(click)

    def _repoint(self):
        """Re-point the shared document/stroke references at the editor's current
        objects (they're swapped out on open / page insert / reorder)."""
        c, src = self.canvas, self._src
        c.document = src.document
        c.all_strokes = src.all_strokes
        c._anchors = src._anchors
        c.n_pages = src.n_pages

    def sync_page(self):
        """Mirror the editor's current page (reloads + refits to our own size)."""
        self._repoint()
        c = self.canvas
        if c.document is None:
            c.page = None
            c.queue_draw()
            return
        # always reload: after a reorder the page at this index has changed
        c._load_page(self._src.current_page_idx)

    def refresh(self):
        """Strokes changed on the current page — just redraw (no reload)."""
        self._repoint()
        self.canvas.queue_draw()

    # presentation-remote style bindings: forward / back / leave
    _NAV_NEXT_KEYS = (Gdk.KEY_space, Gdk.KEY_Right, Gdk.KEY_Down,
                      Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down, Gdk.KEY_Return)
    _NAV_PREV_KEYS = (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_BackSpace,
                      Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up)

    def _nav(self, delta):
        if self._on_nav is not None:
            self._on_nav(delta)

    def detach(self):
        """Unhook from the editor canvas (presenter is closing)."""
        if self._src.on_live_draw == self.canvas.queue_draw:
            self._src.on_live_draw = None

    def _on_key(self, _ctrl, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in self._NAV_NEXT_KEYS:
            self._nav(1)
            return True
        if keyval in self._NAV_PREV_KEYS:
            self._nav(-1)
            return True
        return False

    def _on_click(self, gesture, _n_press, _x, _y):
        button = gesture.get_current_button()
        if button in (Gdk.BUTTON_PRIMARY, 8):   # click / mouse-forward → next
            self._nav(1)
        elif button in (Gdk.BUTTON_SECONDARY, 9):   # right / mouse-back → prev
            self._nav(-1)


class PDFEditorApp(Adw.Application):
    def __init__(self):
        # Single instance: every launch is routed to this one process, so all
        # windows share it and a tab can be dragged between any of them.
        # HANDLES_COMMAND_LINE lets the primary instance parse each launch's
        # arguments (a file, --page) and open them in a fresh window.
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        # Ctrl+R reload sets SIDEMARK_STANDALONE so its fresh-code process runs
        # independently instead of forwarding to the (old-code) instance.
        if os.environ.get("SIDEMARK_STANDALONE"):
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id="de.hspitz.sidemark", flags=flags)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Ctrl+C from the launching terminal should stop the app cleanly rather
        # than surface a KeyboardInterrupt traceback through the GLib main loop.
        # GLibUnix.signal_add is the current API; fall back to the (deprecated)
        # GLib.unix_signal_add on older PyGObject (Ubuntu/Fedora CI runners).
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except (ValueError, ImportError):
            add_signal = GLib.unix_signal_add
        add_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_sigint)

    def _on_sigint(self):
        logger.info("Interrupted (Ctrl+C) — shutting down")
        # destroy() tears windows down without firing close-request, so we don't
        # block on a save prompt — the periodic autosave already covers the work.
        for win in list(self.get_windows()):
            win.destroy()
        self.quit()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _parse_open_args(args):
        """Pull a file path, --page N and --presentation out of one launch's
        arguments (argv without the program name). Unknown flags are ignored."""
        path, page, deck = None, 0, False
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--page" and i + 1 < len(args):
                try:
                    page = max(0, int(args[i + 1]))
                except ValueError:
                    pass
                i += 2
                continue
            if a in ("--presentation", "--deck"):
                deck = True
                i += 1
                continue
            if a in ("-v", "--verbose"):
                i += 1
                continue
            if not a.startswith("-") and path is None:
                path = a
            i += 1
        return path, page, deck

    def do_command_line(self, command_line):
        """Every launch (this process or a forwarded one from a second
        invocation) lands here in the single primary instance; open the file it
        names in a new window."""
        args = command_line.get_arguments()
        path, page, deck = self._parse_open_args(args[1:])
        if path and not os.path.isabs(path):
            cwd = command_line.get_cwd()
            if cwd:
                path = os.path.join(cwd, path)
        # A second launch forwards here and exits immediately; tell its shell
        # why, so it doesn't just look like the command silently did nothing.
        if command_line.get_is_remote():
            what = (f"‘{os.path.basename(path)}’" if path
                    else "a new presentation" if deck else "a new window")
            command_line.print_literal(
                f"Sidemark is already running — opened {what} in it.\n")
        self.open_new_window(path, page, deck)
        return 0

    def do_activate(self):
        # bare activation (e.g. via D-Bus) with no command line → empty window
        self.open_new_window()

    def open_new_window(self, path=None, page=0, deck=False):
        logger.info("Opening new window: %s",
                    path or ("(new presentation)" if deck else "(scratchpad)"))
        win = PDFEditorWindow(self)
        win.present()
        if path and os.path.isfile(path):
            win.open_file(path)
            if page > 0:
                win._go_to_page(page)
        elif deck:
            GLib.idle_add(win._on_new_presentation)
        else:
            if path:
                logger.warning("File not found: %s", path)
            GLib.idle_add(win._open_scratchpad)
        return win


def main():
    args = sys.argv[1:]
    verbose = "--verbose" in args or "-v" in args
    _setup_logging(verbose=verbose)
    try:
        _prune_autosaves()
    except Exception:
        logger.error("autosave pruning failed:\n" + traceback.format_exc())
    # Fail fast on a named file that doesn't exist, before we hand the launch
    # off to the (possibly already-running) primary instance. Checked here so
    # the error surfaces on the launching terminal with its own cwd.
    path, _page, _deck = PDFEditorApp._parse_open_args(args)
    if path and not os.path.isfile(path):
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    # Pass the full argv through: a single-instance app forwards it to the
    # primary, whose do_command_line opens the file in a new window.
    app = PDFEditorApp()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()

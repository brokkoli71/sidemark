#!/usr/bin/env /usr/bin/python3
import sys
import os
import math
import re
import subprocess
import threading
import tempfile
import logging
import atexit
import traceback
import hashlib
import json
import shutil
import time

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


# Fast path for launcher integrations (walker/elephant menus, rofi, …):
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
from gi.repository import Gtk, Adw, Gdk, GLib, Gio, GtkSource, Pango, PangoCairo
import cairo
import fitz          # PyMuPDF
import numpy as np


class PDFCanvas(Gtk.DrawingArea):
    SCROLL_FLIP_THRESHOLD = 3.0   # scroll notches past the page edge before flipping

    def __init__(self):
        super().__init__()
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

        # undo: ("draw", page, stroke) | ("erase", page, idx, stroke, group);
        # erase ops of one drag gesture share a group and undo together.
        # redo holds lists of ops exactly as undo_last popped them.
        self._undo_stack = []
        self._redo_stack = []
        self._erase_group = 0

        self.pen_color = (0.05, 0.05, 0.8)   # RGB — stroke alpha lives in "opacity"
        self.pen_width = 2.0
        # highlighter mode: wide translucent strokes (PDF CA key via annot.set_opacity)
        self.highlighter = False
        self.hl_color = (1.0, 0.85, 0.0)
        self.hl_width = 12.0
        self.hl_opacity = 0.40
        self.surround_color = (0.910, 0.867, 0.824)  # overridden by window with theme color
        self.zoom_accent = (0.52, 0.70, 0.30)        # overridden with theme accent

        self.on_page_changed = None    # callback(current_idx, n_pages)
        self.on_page_will_change = None  # callback() before leaving the page (commit notes)
        self.on_nav_button = None     # callback(delta: int) for back/forward buttons
        self.on_change = None         # callback() whenever strokes are modified
        self.on_anchor_placed = None   # callback(page_idx, pdf_x, pdf_y)
        self.on_anchor_clicked = None  # callback(anchor_index)
        self.on_callout_placed = None  # callback(pdf_x, pdf_y) — for the last placed anchor
        self.on_user_action = None     # callback() once per completed draw/erase gesture

        # {page_idx: [anchor dict from _parse_anchors, ...]}
        self._anchors = {}
        self._active_anchors = set()  # indices highlighted on current page

        # Ctrl+Alt+drag: anchor placed at press (GestureClick), callout box
        # placed at release when the drag travelled far enough
        self._callout_dragging = False
        self._callout_start = None    # screen (x, y)
        self._callout_cur = None

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

        self._panning = False
        self._pan_start_offset = (0.0, 0.0)

        self._ignoring = False  # True while a button-8/9 drag sequence is active

        self._thumb_panning = False
        self._thumb_origin = (0.0, 0.0)
        self._thumb_start_offset = (0.0, 0.0)

        # word-level text selection (Alt+drag) and link opening (Alt+click)
        self._text_selecting = False
        self._alt_start = (0.0, 0.0)
        self._selected_words = []   # fitz word tuples currently highlighted
        self._page_words = []       # cached for current page
        self.on_text_copied = None  # callback(text_or_None)

        # link hover hint
        self._alt_held = False
        self._hover_x = 0.0
        self._hover_y = 0.0
        self._hovered_link_rect = None


        self.set_draw_func(self._draw)
        self.set_focusable(True)
        self.set_can_focus(True)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave",  self._on_motion_leave)
        self.add_controller(motion)
        self._mouse_x = 0.0
        self._mouse_y = 0.0

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES |
            Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        drag = Gtk.GestureDrag.new()
        drag.set_button(0)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        thumb = Gtk.GestureSingle()
        thumb.set_button(10)
        thumb.set_exclusive(True)
        thumb.connect("begin", self._on_thumb_begin)
        thumb.connect("end", self._on_thumb_end)
        self.add_controller(thumb)

        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect("pressed", self._on_click_pressed)
        self.add_controller(click)



        key = Gtk.EventControllerKey.new()
        key.connect("key-pressed",  self._on_alt_key, True)
        key.connect("key-released", self._on_alt_key, False)
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
        self.current_page_idx = idx
        self.page = self.document[idx]
        self.page_width  = self.page.rect.width
        self.page_height = self.page.rect.height
        self._page_surface = None
        self._surface_scale = 0.0
        self._scroll_past = 0.0
        if self._rerender_id is not None:
            GLib.source_remove(self._rerender_id)
            self._rerender_id = None
        if keep_view:
            self._needs_fit = False   # caller keeps zoom and positions the view
        else:
            self._needs_fit = True    # re-fit on first draw with real canvas dimensions
        self._page_words = self.page.get_text("words")   # cache for text selection
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
        """(color, width, opacity) of the active drawing tool."""
        if self.highlighter:
            return self.hl_color, self.hl_width, self.hl_opacity
        return self.pen_color, self.pen_width, 1.0

    # ── layout ───────────────────────────────────────────────────────────────

    def _fit_page(self, w=None, h=None):
        w = w or self.get_width() or 800
        h = h or self.get_height() or 600
        if self.page_width and self.page_height:
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

    def _rerender_now(self):
        if not self.page:
            return
        sf = self.get_scale_factor()
        logical_scale = min(max(self.scale, 0.5), 4.0)
        device_scale  = logical_scale * sf
        pix = self.page.get_pixmap(matrix=fitz.Matrix(device_scale, device_scale), alpha=True, annots=False)
        w, h = pix.width, pix.height
        # fitz RGBA → cairo ARGB32 (BGRA in memory on little-endian): swap R and B channels
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, 4).copy()
        arr[:, :, [0, 2]] = arr[:, :, [2, 0]]
        surf = cairo.ImageSurface.create_for_data(arr, cairo.FORMAT_ARGB32, w, h)
        surf.set_device_scale(sf, sf)
        self._page_surface = surf
        self._surface_scale = logical_scale

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

        # anchor markers
        anchors = self._anchors.get(self.current_page_idx, [])
        if anchors:
            # callout boxes go under the circles so an anchor inside a box stays visible
            for a in anchors:
                if a.get("callout") and a.get("text"):
                    self._draw_callout(ctx, a)
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

    def _draw_callout(self, ctx, a):
        """Wrapped note text in a box at the callout position, with an arrow
        from the anchor circle to the box. Drawn in screen space for crisp
        text; all dimensions scale with zoom."""
        ax, ay = self._pdf_to_screen(a["x"], a["y"])
        cx, cy = self._pdf_to_screen(*a["callout"])
        pad = max(3.0, 5.0 * self.scale)

        layout = PangoCairo.create_layout(ctx)
        desc = Pango.FontDescription("Sans")
        desc.set_absolute_size(max(6.0, 8.5 * self.scale) * Pango.SCALE)
        layout.set_font_description(desc)
        layout.set_width(int(170 * self.scale * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_text(a["text"])
        tw, th = layout.get_pixel_size()
        bx, by = cx, cy
        bw, bh = tw + 2 * pad, th + 2 * pad

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

    # ── input handlers ────────────────────────────────────────────────────────

    def _on_thumb_begin(self, gesture, sequence):
        if self._thumb_panning:
            pass
            self._thumb_panning = False
        else:
            logger.debug(f"thumb pan start ({self._mouse_x:.0f},{self._mouse_y:.0f})")
            self._thumb_panning = True
            self._is_fitted = False
            self._thumb_origin = (self._mouse_x, self._mouse_y)
            self._thumb_start_offset = (self.offset_x, self.offset_y)

    def _on_thumb_end(self, gesture, sequence):
        pass  # ignored — toggle mode, only begin matters

    def _on_motion(self, _ctrl, x, y):
        if self._thumb_panning:
            self.offset_x = self._thumb_start_offset[0] + (x - self._thumb_origin[0])
            self.offset_y = self._thumb_start_offset[1] + (y - self._thumb_origin[1])
            self.queue_draw()
        self._mouse_x = x
        self._mouse_y = y
        self._hover_x, self._hover_y = x, y
        self._update_link_hover()

    def _on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            if self._handle_boundary_flip(dx, dy):
                return True
            self._scroll_past = 0.0
            self.offset_x -= dx * 30
            self.offset_y -= dy * 30
            self._is_fitted = False
            self.queue_draw()
            return True
        factor = 0.9 if dy > 0 else 1.1
        mx, my = self._mouse_x, self._mouse_y
        pdf_x = (mx - self.offset_x) / self.scale
        pdf_y = (my - self.offset_y) / self.scale
        self.scale = max(0.1, min(20.0, self.scale * factor))
        self._is_fitted = False
        self.offset_x = mx - pdf_x * self.scale
        self.offset_y = my - pdf_y * self.scale
        self._schedule_rerender()
        self.queue_draw()
        return True

    def _handle_boundary_flip(self, dx, dy):
        """Scrolling further while the page edge is already visible flips the
        page (after a small resistance threshold). Returns True when the
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
        if self._scroll_past >= self.SCROLL_FLIP_THRESHOLD:
            self._flip_page(1)
        elif self._scroll_past <= -self.SCROLL_FLIP_THRESHOLD:
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
        if delta > 0:
            self.offset_y = 8.0
        else:
            self.offset_y = ch - self.page_height * self.scale - 8.0
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

    def _on_motion_leave(self, _ctrl):
        if self._hovered_link_rect is not None:
            self._hovered_link_rect = None
            self.set_cursor(None)
            self.queue_draw()

    def _on_alt_key(self, _ctrl, keyval, _keycode, _state, pressed):
        if keyval in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R):
            self._alt_held = pressed
            self._update_link_hover()

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
        state = gesture.get_current_event_state()
        if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.ALT_MASK):
            if self.page is None:
                return
            px, py = self._screen_to_pdf(x, y)
            if self.on_anchor_placed:
                self.on_anchor_placed(self.current_page_idx, round(px), round(py))

    def _on_drag_begin(self, gesture, start_x, start_y):
        if gesture.get_current_button() == 3:
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
            self._ignoring = True
            if self.on_nav_button:
                self.on_nav_button(1 if btn == 8 else -1)
            return
        if btn == 10:
            self._ignoring = True  # GestureSingle owns this sequence
            return
        self._ignoring = False
        self._erasing = False
        state = gesture.get_current_event_state()
        if (state & Gdk.ModifierType.CONTROL_MASK) and (state & Gdk.ModifierType.ALT_MASK):
            # anchor already placed at press by GestureClick; dragging on
            # places a callout box at the release point
            self._callout_dragging = True
            self._callout_start = (start_x, start_y)
            self._callout_cur = None
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
            hit = self._anchor_hit_test(start_x, start_y)
            if hit is not None:
                self._ignoring = True
                if self.on_anchor_clicked:
                    self.on_anchor_clicked(hit)
                return
            self.current_stroke = [self._screen_to_pdf(start_x, start_y)]

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if self._ignoring:
            return
        logger.debug(f"drag update offset=({offset_x:.0f},{offset_y:.0f})")
        sx, sy = gesture.get_start_point()[1], gesture.get_start_point()[2]
        if self._callout_dragging:
            self._callout_cur = (sx + offset_x, sy + offset_y)
            self.queue_draw()
            return
        if self._erasing:
            self._erase_at(sx + offset_x, sy + offset_y)
            return
        if self._panning:
            self.offset_x = self._pan_start_offset[0] + offset_x
            self.offset_y = self._pan_start_offset[1] + offset_y
            self.queue_draw()
            return
        if self._text_selecting:
            px0, py0 = self._screen_to_pdf(sx, sy)
            px1, py1 = self._screen_to_pdf(sx + offset_x, sy + offset_y)
            self._selected_words = self._words_in_rect(px0, py0, px1, py1)
            self.queue_draw()
            return
        if self._zoom_selecting:
            self._zoom_end = self._constrain_zoom_end(sx, sy, sx + offset_x, sy + offset_y)
        else:
            self.current_stroke.append(self._screen_to_pdf(sx + offset_x, sy + offset_y))
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        logger.debug(f"drag end offset=({offset_x:.0f},{offset_y:.0f})")
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
        if self._panning:
            self._panning = False
            return
        if self._text_selecting:
            self._text_selecting = False
            if abs(offset_x) < 8 and abs(offset_y) < 8:
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
                color, width, opacity = self._pen_attrs()
                stroke = {
                    "pts": self.current_stroke,
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
        self.queue_draw()

    def _words_in_rect(self, px0, py0, px1, py1):
        """Return fitz word tuples whose bounding boxes overlap the given PDF rect."""
        rx0, rx1 = min(px0, px1), max(px0, px1)
        ry0, ry1 = min(py0, py1), max(py0, py1)
        return [w for w in self._page_words
                if w[0] < rx1 and w[2] > rx0 and w[1] < ry1 and w[3] > ry0]

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
            return
        px, py = self._screen_to_pdf(sx, sy)
        for link in self.page.get_links():
            r = link["from"]
            if r.x0 <= px <= r.x1 and r.y0 <= py <= r.y1:
                kind = link.get("kind", 0)
                if kind == fitz.LINK_URI:
                    uri = link.get("uri", "")
                    if uri:
                        try:
                            Gio.AppInfo.launch_default_for_uri(uri, None)
                        except Exception:
                            pass
                elif kind == fitz.LINK_GOTO:
                    page_no = link.get("page", -1)
                    if page_no >= 0:
                        self.go_to_page(page_no)
                break

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

    def undo_last(self):
        """Undo the last draw or erase operation (an erase drag counts as one)."""
        if not self._undo_stack:
            return
        op = self._undo_stack.pop()
        popped = [op]
        page = op[1]
        strokes = self.all_strokes.setdefault(page, [])
        if op[0] == "draw":
            for i, s in enumerate(strokes):
                if s is op[2]:
                    del strokes[i]
                    break
        else:
            strokes.insert(min(op[2], len(strokes)), op[3])
            group = op[4]
            while (self._undo_stack and self._undo_stack[-1][0] == "erase"
                   and self._undo_stack[-1][4] == group):
                op = self._undo_stack.pop()
                popped.append(op)
                strokes.insert(min(op[2], len(strokes)), op[3])
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
            strokes.append(ops[0][2])
        else:
            # re-remove in the gesture's chronological order (reverse of pop order)
            for op in reversed(ops):
                for i, s in enumerate(strokes):
                    if s is op[3]:
                        del strokes[i]
                        break
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


def notes_path_for(pdf_path):
    return os.path.splitext(pdf_path)[0] + "-notes.md"


# ── autosave snapshots ────────────────────────────────────────────────────────
# Unsaved changes are snapshotted here periodically; the original file is
# never touched until an explicit save. XDG_STATE_HOME, not cache — cache
# cleaners must not eat unsaved lecture notes.

AUTOSAVE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "sidemark", "autosave")


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
_MD_STRIP = [
    (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    (re.compile(r'\*([^*\n]+?)\*'), r'\1'),
    (re.compile(r'`([^`\n]+?)`'), r'\1'),
]


def _strip_markers(text):
    text = _ANCHOR_RE.sub('', text)
    text = _CALLOUT_RE.sub('', text)
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


def _export_pdf_with_notes(src_path, out_path, notes_model, include_empty, accent):
    src_doc = fitz.open(src_path)
    out_doc = fitz.open()
    r, g, b = accent
    anchor_color = (r, g, b)

    for page_idx in range(len(src_doc)):
        notes_text = notes_model.get(page_idx)
        anchors = _parse_anchors(notes_text)
        has_notes = bool(notes_text.strip())

        # Copy source page
        out_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
        out_page = out_doc[-1]

        # Callout boxes under the anchor circles
        for a in anchors:
            if a["callout"] and a["text"]:
                _draw_export_callout(out_page, a, anchor_color)
        # Draw numbered anchor markers on top of the page
        for i, a in enumerate(anchors):
            _draw_export_anchor(out_page, a["x"], a["y"], i + 1, anchor_color)

        # Notes page
        if has_notes or include_empty:
            w, h = out_page.rect.width, out_page.rect.height
            notes_page = out_doc.new_page(width=w, height=h)
            _render_export_notes(notes_page, page_idx, notes_text, anchor_color)

    out_doc.save(out_path, garbage=4, deflate=True)
    out_doc.close()
    src_doc.close()


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


def _render_export_notes(page, page_idx, notes_text, anchor_color):
    margin = 40
    w, h = page.rect.width, page.rect.height
    r, g, b = anchor_color

    # Header
    page.draw_line((margin, 30), (w - margin, 30), color=(0.7, 0.7, 0.7))
    page.insert_text((margin, 24), f"Notes — Page {page_idx + 1}",
                     fontsize=11, color=(0.3, 0.3, 0.3), fontname="hebo")

    # Process notes text: replace anchors, strip markdown
    counter = [0]

    def _replace_anchor(m):
        counter[0] += 1
        return f"[{counter[0]}]"

    text = _ANCHOR_RE.sub(_replace_anchor, notes_text)
    text = _CALLOUT_RE.sub('', text)
    for pattern, repl in _MD_STRIP:
        text = pattern.sub(repl, text)
    text = text.strip()

    if text:
        rect = fitz.Rect(margin, 45, w - margin, h - margin)
        page.insert_textbox(rect, text, fontsize=10, color=(0, 0, 0),
                            fontname="helv", align=0)


class NotesModel:
    """Per-page markdown notes, backed by a sidecar .md file."""

    def __init__(self):
        self._notes = {}
        self.pdf_name = None  # written as ![[name.pdf]] at top of the file

    def get(self, idx):
        return self._notes.get(idx, "")

    def set(self, idx, text):
        self._notes[idx] = text

    def load(self, path):
        self._notes = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            return
        # Strip leading embed line (![[name.pdf]]) before parsing
        raw = re.sub(r'^\s*!\[\[.*?\]\]\n+', '', raw)
        # Format: <!-- page:N --> delimiters (invisible in markdown viewers)
        parts = re.split(r'<!--\s*page:(\d+)\s*-->', raw)
        for i in range(1, len(parts), 2):
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                self._notes[int(parts[i])] = content

    def shift_for_insert(self, idx):
        """Re-key notes after a page was inserted at idx."""
        self._notes = {
            (k + 1 if k >= idx else k): v
            for k, v in self._notes.items()
        }

    def shift_for_delete(self, idx):
        """Drop the note of deleted page idx; re-key later pages."""
        self._notes = {
            (k - 1 if k > idx else k): v
            for k, v in self._notes.items()
            if k != idx
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

    # Combined regex — bold must come before italic so ** is consumed first.
    # Italic uses [^*\n] to prevent matching across ** markers or newlines.
    _INLINE = re.compile(r'\*\*(.+?)\*\*|\*([^*\n]+?)\*|`([^`\n]+?)`')

    # Super/subscript: ^{content} or ^x  /  _{content} or _x
    _SCRIPT_RE = re.compile(r'(\^|_)(?:\{([^}]*)\}|(\S+))')

    # Symbol substitution table
    _SYMBOLS = {
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
        r'\div': '÷', r'\cdot': '·', r'\to': '→', r'\gets': '←',
        r'\in': '∈', r'\notin': '∉', r'\subset': '⊂', r'\supset': '⊃',
        r'\cup': '∪', r'\cap': '∩', r'\emptyset': '∅',
        r'\forall': '∀', r'\exists': '∃',
        r'\partial': '∂', r'\nabla': '∇',
    }
    _SYMBOL_RE = re.compile(r'\\([A-Za-z]+)')

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

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    # ── formatting shortcuts ──────────────────────────────────────────────────

    def _on_key(self, ctrl, keyval, keycode, state):
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return False
        if keyval == Gdk.KEY_b:
            self._wrap_selection("**")
            return True
        if keyval == Gdk.KEY_i:
            self._wrap_selection("*")
            return True
        if keyval == Gdk.KEY_e:
            self._wrap_selection("`")
            return True
        return False

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

        text = buf.get_text(s, e, False)
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
        def _repl(m):
            return self._SYMBOLS.get('\\' + m.group(1), m.group(0))
        return self._SYMBOL_RE.sub(_repl, text)

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
        if buf.get_text(ls, le, False) != original:
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


class PDFEditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Sidemark")
        self.set_default_size(1280, 800)
        self._path = None
        self._notes_path = None   # set when a .md file is opened without an associated PDF
        self._is_untitled = False  # True when working on an auto-created blank (no saved path yet)
        self._dirty = False
        self._suppress_dirty = False
        self._syncing_pen = False   # guard while pen popover mirrors tool state
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
        self._search_hits = {}      # {page_idx: [fitz.Rect, ...]}
        self._search_matches = []   # [(page_idx, rect_idx), ...] flat list
        self._search_current = -1   # index into _search_matches

        theme = _load_theme()
        bg = _hex_to_rgb(theme["background"])
        fg = _hex_to_rgb(theme["foreground"])
        acc = _hex_to_rgb(theme["accent"])
        surround = tuple(b + 0.12 * (f - b) for b, f in zip(bg, fg))
        _lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        _src_scheme = "Adwaita-dark" if _lum < 0.5 else "Adwaita"
        self._swatch_presets = [
            ("Accent", acc),
            ("Red",    _hex_to_rgb(theme["color1"])),
            ("Black",  _hex_to_rgb(theme["foreground"])),
            ("Brown",  _hex_to_rgb(theme["color3"])),
            ("Teal",   _hex_to_rgb(theme["color6"])),
            ("Gray",   _hex_to_rgb(theme["color8"])),
        ]

        self.canvas = PDFCanvas()
        self.canvas.surround_color = surround
        self.canvas.zoom_accent = acc
        self.canvas.set_vexpand(True)
        self.canvas.set_hexpand(True)
        self.canvas.on_page_changed = self._on_page_changed
        self.canvas.on_change = self._mark_dirty
        self.canvas.on_text_copied = self._on_text_copied
        self.canvas.on_nav_button = lambda d: self._go_to_page(self.canvas.current_page_idx + d)
        # commit the current note before any canvas-initiated page change
        # (scroll flip, link jump, undo on another page)
        self.canvas.on_page_will_change = self._commit_note

        GLib.timeout_add_seconds(60, self._autosave_tick)
        self.canvas.on_anchor_placed = self._on_anchor_placed
        self.canvas.on_anchor_clicked = self._on_anchor_clicked
        self.canvas.on_callout_placed = self._on_callout_placed
        self.canvas.on_user_action = self._on_canvas_action
        self._last_anchor_mark = None   # TextMark right after the last placed anchor

        # ── CSS ───────────────────────────────────────────────────────────────
        acc_hex = "#{:02x}{:02x}{:02x}".format(*(int(c * 255) for c in acc))
        fg_hex  = theme["foreground"]
        bg_hex  = theme["background"]
        css = f"""
            .save-button {{
                background: {acc_hex};
                color: {fg_hex};
                font-weight: bold;
            }}
            .save-button:hover {{ background: shade({acc_hex}, 1.1); }}
            .notes-view {{
                font-family: monospace;
                font-size: 13px;
                background-color: {bg_hex};
                color: {fg_hex};
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
        """
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

        # ── header bar ────────────────────────────────────────────────────────
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        open_btn = Gtk.Button(label="Open")
        open_btn.connect("clicked", self._on_open)
        header.pack_start(open_btn)

        self._recent_popover = Gtk.Popover()
        self._recent_popover.connect("show", self._rebuild_recent_menu)
        recent_btn = Gtk.MenuButton()
        recent_btn.set_icon_name("document-open-recent-symbolic")
        recent_btn.set_tooltip_text("Open recent")
        recent_btn.set_popover(self._recent_popover)
        header.pack_start(recent_btn)

        new_btn = Gtk.Button(label="New")
        new_btn.set_tooltip_text("Create a new blank A4 PDF")
        new_btn.connect("clicked", self._on_new_pdf)
        header.pack_start(new_btn)

        # stays sensitive even without a TOC — insensitive widgets get no
        # tooltip in GTK4, and the tooltip is how we explain the situation
        self._has_toc = False
        self._toc_thumbs = False
        self._thumb_idle_id = None
        self._toc_btn = Gtk.ToggleButton()
        self._toc_btn.set_icon_name("view-list-symbolic")
        self._toc_btn.set_tooltip_text("No document open")
        self._toc_btn.connect("toggled", self._on_toc_toggled)
        header.pack_start(self._toc_btn)

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

        add_page_btn = Gtk.Button()
        add_page_btn.set_icon_name("list-add-symbolic")
        add_page_btn.set_tooltip_text("Add blank page after this one (Ctrl+Shift+N)")
        add_page_btn.connect("clicked", lambda _: self._add_blank_page())

        del_page_btn = Gtk.Button()
        del_page_btn.set_icon_name("list-remove-symbolic")
        del_page_btn.set_tooltip_text("Delete current page (Ctrl+Shift+Delete)")
        del_page_btn.connect("clicked", lambda _: self._delete_current_page())

        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav_box.add_css_class("linked")
        nav_box.append(prev_btn)
        nav_box.append(self._page_label)
        nav_box.append(next_btn)
        nav_box.append(add_page_btn)
        nav_box.append(del_page_btn)

        self._file_label = Gtk.Label(label="")
        self._file_label.add_css_class("dim-label")
        self._file_label.add_css_class("caption")
        self._file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._file_label.set_max_width_chars(28)
        self._file_label.set_valign(Gtk.Align.CENTER)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_box.set_valign(Gtk.Align.CENTER)
        title_box.append(nav_box)
        title_box.append(self._file_label)
        header.set_title_widget(title_box)

        redo_btn = Gtk.Button()
        redo_btn.set_icon_name("edit-redo-symbolic")
        redo_btn.set_tooltip_text("Redo (Ctrl+Y / Ctrl+Shift+Z)")
        redo_btn.connect("clicked", lambda _: self._global_redo())
        header.pack_end(redo_btn)

        undo_btn = Gtk.Button()
        undo_btn.set_icon_name("edit-undo-symbolic")
        undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        undo_btn.connect("clicked", lambda _: self._global_undo())
        header.pack_end(undo_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("save-button")
        save_btn.set_tooltip_text("Save (Ctrl+S)")
        save_btn.connect("clicked", self._on_save)
        header.pack_end(save_btn)

        export_btn = Gtk.Button()
        export_btn.set_icon_name("document-send-symbolic")
        export_btn.set_tooltip_text("Export with notes (Ctrl+E)")
        export_btn.connect("clicked", lambda _: self._on_export())
        header.pack_end(export_btn)

        # pen settings popover
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        popover_box.set_margin_start(16)
        popover_box.set_margin_end(16)
        popover_box.set_margin_top(12)
        popover_box.set_margin_bottom(12)

        # tool switcher — the width/color controls below edit the active tool
        tool_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        tool_box.add_css_class("linked")
        tool_box.set_homogeneous(True)
        self._pen_seg = Gtk.ToggleButton(label="Pen")
        self._pen_seg.set_active(True)
        self._hl_toggle = Gtk.ToggleButton(label="Highlighter")
        self._hl_toggle.set_tooltip_text("Wide translucent strokes (Ctrl+H)")
        self._hl_toggle.set_group(self._pen_seg)
        self._hl_toggle.connect("toggled", self._on_highlighter_toggled)
        tool_box.append(self._pen_seg)
        tool_box.append(self._hl_toggle)
        popover_box.append(tool_box)

        width_label = Gtk.Label(label="Width", xalign=0)
        width_label.add_css_class("dim-label")
        width_label.set_margin_top(6)
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

        popover = Gtk.Popover()
        popover.set_child(popover_box)

        self._pen_btn = Gtk.MenuButton()
        self._pen_btn.set_icon_name("document-edit-symbolic")
        self._pen_btn.set_tooltip_text("Pen settings")
        self._pen_btn.set_popover(popover)
        header.pack_end(self._pen_btn)

        # While the highlighter is active the pen button shows a mini preview
        # of the actual highlight stroke instead of the pencil icon — Adwaita
        # ships no marker icon, and this doubles as a color hint.
        self._hl_icon = Gtk.DrawingArea()
        self._hl_icon.set_content_width(18)
        self._hl_icon.set_content_height(18)

        def _draw_hl_icon(_area, ctx, w, h):
            r, g, b = self.canvas.hl_color
            ctx.set_source_rgba(r, g, b, max(self.canvas.hl_opacity, 0.55))
            ctx.set_line_width(7)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.move_to(4, h - 5)
            ctx.line_to(w - 4, 5)
            ctx.stroke()
        self._hl_icon.set_draw_func(_draw_hl_icon)

        # notes toggle
        self._notes_toggle = Gtk.ToggleButton()
        self._notes_toggle.set_icon_name("view-sidebar-symbolic")
        self._notes_toggle.set_tooltip_text("Toggle notes (Ctrl+\\)")
        self._notes_toggle.set_active(True)
        self._notes_toggle.connect("toggled", self._on_notes_toggled)
        header.pack_end(self._notes_toggle)

        # shortcuts help
        help_btn = Gtk.MenuButton()
        help_btn.set_label("?")
        help_btn.set_tooltip_text("Keyboard shortcuts")
        help_btn.set_popover(self._build_shortcuts_popover())
        header.pack_end(help_btn)

        # ── notes panel ───────────────────────────────────────────────────────
        self._notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        notes_header = Gtk.Label(label="Notes")
        notes_header.add_css_class("dim-label")
        notes_header.set_xalign(0)
        notes_header.set_margin_start(10)
        notes_header.set_margin_top(6)
        notes_header.set_margin_bottom(4)
        self._notes_box.append(notes_header)

        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_vexpand(True)
        notes_scroll.set_hexpand(True)
        self._notes_view = MarkdownNotesView(_src_scheme)
        self._notes_view.get_buffer().connect("changed", self._on_notes_changed)
        self._notes_view.get_buffer().connect("notify::cursor-position", self._on_notes_cursor_moved)
        notes_scroll.set_child(self._notes_view)
        self._notes_box.append(notes_scroll)

        # ── search bar ────────────────────────────────────────────────────────
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.set_margin_start(6)
        search_box.set_margin_end(6)
        search_box.set_margin_top(4)
        search_box.set_margin_bottom(4)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_hexpand(True)
        self._search_entry.set_placeholder_text("Search in PDF…")
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("stop-search", lambda _: self._hide_search())
        self._search_entry.connect("activate", lambda _: self._search_next())

        search_key = Gtk.EventControllerKey()
        search_key.connect("key-pressed", self._on_search_key)
        self._search_entry.add_controller(search_key)

        search_prev_btn = Gtk.Button()
        search_prev_btn.set_icon_name("go-up-symbolic")
        search_prev_btn.set_tooltip_text("Previous match")
        search_prev_btn.connect("clicked", lambda _: self._search_prev())

        search_next_btn = Gtk.Button()
        search_next_btn.set_icon_name("go-down-symbolic")
        search_next_btn.set_tooltip_text("Next match")
        search_next_btn.connect("clicked", lambda _: self._search_next())

        self._search_label = Gtk.Label(label="")
        self._search_label.add_css_class("dim-label")
        self._search_label.set_width_chars(7)

        search_close_btn = Gtk.Button()
        search_close_btn.set_icon_name("window-close-symbolic")
        search_close_btn.connect("clicked", lambda _: self._hide_search())

        search_box.append(self._search_entry)
        search_box.append(search_prev_btn)
        search_box.append(search_next_btn)
        search_box.append(self._search_label)
        search_box.append(search_close_btn)

        self._search_revealer = Gtk.Revealer()
        self._search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._search_revealer.set_child(search_box)
        self._search_revealer.set_reveal_child(False)

        canvas_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        canvas_box.append(self._search_revealer)
        canvas_box.append(self.canvas)

        # ── split pane ────────────────────────────────────────────────────────
        self._saved_pane_pos = 800
        self._pane_anim = None   # running notes show/hide animation
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_start_child(canvas_box)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)
        self._paned.set_end_child(self._notes_box)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(True)
        self._paned.set_hexpand(True)
        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

        # ── outline (TOC) sidebar ─────────────────────────────────────────────
        self._toc_list = Gtk.ListBox()
        self._toc_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._toc_list.connect("row-activated", self._on_toc_row_activated)
        self._toc_scroll = Gtk.ScrolledWindow()
        self._toc_scroll.set_child(self._toc_list)
        self._toc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._toc_scroll.set_size_request(230, -1)
        self._toc_revealer = Gtk.Revealer()
        self._toc_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self._toc_revealer.set_child(self._toc_scroll)
        self._toc_revealer.set_reveal_child(False)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(self._toc_revealer)
        content.append(self._paned)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(content)
        self.set_child(self.toast_overlay)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

        # Ctrl+Z must work globally; the notes TextView consumes it before the
        # bubble-phase controller above, so intercept it in the capture phase.
        undo_ctrl = Gtk.EventControllerKey()
        undo_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        undo_ctrl.connect("key-pressed", self._on_undo_key)
        self.add_controller(undo_ctrl)

    # ── shortcuts popover ─────────────────────────────────────────────────────

    def _build_shortcuts_popover(self):
        shortcuts = [
            ("Draw",          None),
            ("Left-drag",     "Draw stroke"),
            ("Right-drag",    "Erase stroke"),
            ("Ctrl+H",        "Toggle highlighter"),
            ("Ctrl+Z",        "Undo last action (draw, erase, typing)"),
            ("Ctrl+Y",        "Redo (also Ctrl+Shift+Z)"),
            ("Text",          None),
            ("Alt+Drag",      "Select text (word-level)"),
            ("Ctrl+C",        "Copy selected text"),
            ("Alt+Click",     "Open link under cursor"),
            ("Ctrl+Alt+Click","Place anchor marker in notes"),
            ("Ctrl+Alt+Drag","Place anchor + callout box at drag end"),
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
            ("Ctrl+F",        "Search text in PDF"),
            ("Ctrl+S",        "Save"),
            ("Ctrl+Shift+S",  "Save as…"),
            ("Ctrl+E",        "Export PDF with notes"),
            ("Ctrl+R",        "Reload (new instance)"),
            ("Ctrl+\\",       "Toggle notes"),
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
        popover = Gtk.Popover()
        popover.set_child(scroll)
        return popover

    # ── page & notes handshake ────────────────────────────────────────────────

    def _set_file_title(self, subtitle, full_path=None):
        self._file_label.set_label(subtitle)
        self._file_label.set_tooltip_text(full_path or subtitle)
        self.set_title(f"Sidemark — {subtitle}")

    def _on_realize(self, _widget):
        GLib.idle_add(self._init_pane_position)

    def _init_pane_position(self):
        width = self.get_allocated_width()
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

    # ── dirty tracking ────────────────────────────────────────────────────────

    def _mark_dirty(self, *_):
        if not self._suppress_dirty:
            self._dirty = True

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
        if self._dirty and self._path:
            try:
                self._write_autosave()
            except Exception:
                logger.error("autosave failed:\n" + traceback.format_exc())
        return True   # keep the timer running

    def _write_autosave(self):
        d = _autosave_dir_for(self._path)
        os.makedirs(d, exist_ok=True)
        self.canvas.save_copy(os.path.join(d, "doc.pdf"))
        self._commit_note()
        self.notes_model.save(os.path.join(d, "notes.md"))
        meta = {"path": os.path.abspath(self._path), "saved_at": time.time()}
        tmp = os.path.join(d, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, os.path.join(d, "meta.json"))
        logger.info(f"autosave: snapshot written for {self._path}")

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
        if not self._dirty:
            return False   # allow close
        self._ask_save_then(self.destroy)
        return True        # block default close; destroy() called from dialog

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

    def _go_to_page(self, idx):
        self._commit_note()
        self.canvas.go_to_page(idx)

    def _commit_note(self):
        if not self._path and not self._notes_path and not self._is_untitled:
            return
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self.notes_model.set(self.canvas.current_page_idx, text)

    def _restore_note(self):
        self._suppress_dirty = True
        text = self.notes_model.get(self.canvas.current_page_idx)
        self._last_anchor_mark = None   # set_text would strand the mark at offset 0
        buf = self._notes_view.get_buffer()
        # Programmatic page loads must not enter the undo history — otherwise
        # Ctrl+Z in the notes view could resurrect another page's text here
        buf.begin_irreversible_action()
        buf.set_text(text)
        buf.end_irreversible_action()
        self._suppress_dirty = False
        # a page switch ends any typing burst; future bursts diff against this text
        self._notes_burst_open = False
        self._burst_base = text
        self._update_canvas_anchors()

    # ── global undo ───────────────────────────────────────────────────────────

    def _on_canvas_action(self):
        """A draw/erase gesture finished: record it and end any typing burst."""
        self._notes_burst_open = False
        buf = self._notes_view.get_buffer()
        self._burst_base = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self._undo_timeline.append(("canvas",))
        self._redo_timeline.clear()   # canvas already cleared its own redo

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
        self._suppress_dirty = False
        self._notes_burst_open = False
        self._burst_base = text
        self.notes_model.set(page, text)
        self._mark_dirty()
        self._update_canvas_anchors()

    def _global_undo(self):
        """Undo the most recent user action — a stroke, an erase gesture, or a
        typing burst — in chronological order across canvas and notes."""
        if not self._undo_timeline:
            return
        op = self._undo_timeline.pop()
        if op[0] == "canvas":
            self.canvas.undo_last()
            self._redo_timeline.append(("canvas",))
            return
        _, page, before = op
        if page != self.canvas.current_page_idx:
            self._go_to_page(page)
        buf = self._notes_view.get_buffer()
        after = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self._redo_timeline.append(("notes", page, before, after))
        self._set_notes_text(page, before)

    def _global_redo(self):
        """Re-apply the most recently undone action (Ctrl+Y / Ctrl+Shift+Z)."""
        if not self._redo_timeline:
            return
        op = self._redo_timeline.pop()
        if op[0] == "canvas":
            self.canvas.redo_last()
            self._undo_timeline.append(("canvas",))
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
        if btn.get_active() and not self.canvas.document:
            btn.set_active(False)   # bounce; re-fires toggled with False
            toast = Adw.Toast.new("No document open")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
            return
        self._toc_revealer.set_reveal_child(btn.get_active())
        if btn.get_active() and self._toc_thumbs:
            self._select_thumb(self.canvas.current_page_idx)

    def _on_toc_row_activated(self, _list, row):
        page = getattr(row, "toc_page", None)
        if page is not None:
            self._go_to_page(page)
            self.canvas.grab_focus()

    def _populate_toc(self):
        if self._thumb_idle_id is not None:
            GLib.source_remove(self._thumb_idle_id)
            self._thumb_idle_id = None
        while (child := self._toc_list.get_first_child()) is not None:
            self._toc_list.remove(child)
        toc = []
        if self.canvas.document:
            try:
                toc = self.canvas.document.get_toc(simple=True)
            except Exception:
                toc = []
        self._has_toc = bool(toc)
        self._toc_thumbs = not toc and self.canvas.document is not None
        if toc:
            self._toc_list.set_selection_mode(Gtk.SelectionMode.NONE)
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
                self._toc_list.append(row)
            self._toc_btn.set_tooltip_text("Toggle outline (Ctrl+T)")
        elif self._toc_thumbs:
            self._populate_thumbnails()
            if self._toc_revealer.get_reveal_child():
                self._select_thumb(self.canvas.current_page_idx)
            self._toc_btn.set_tooltip_text(
                "Toggle page thumbnails (Ctrl+T) — no outline in this document")
        else:
            self._toc_btn.set_active(False)   # also hides the revealer
            self._toc_btn.set_tooltip_text("No document open")

    # ── page thumbnails (outline fallback) ────────────────────────────────────

    THUMB_WIDTH = 160

    def _populate_thumbnails(self):
        """Fill the outline sidebar with page thumbnails, rendered lazily so
        opening a large document stays instant."""
        doc = self.canvas.document
        self._toc_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        pictures = []
        for i in range(len(doc)):
            rect = doc[i].rect
            pic = Gtk.Picture()
            scale = self.THUMB_WIDTH / rect.width if rect.width else 0.2
            pic.set_size_request(self.THUMB_WIDTH, int(rect.height * scale))
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
            self._toc_list.append(row)
            pictures.append(pic)

        queue = list(enumerate(pictures))

        def render_next():
            # a new document invalidates the queue
            if not queue or self.canvas.document is not doc:
                self._thumb_idle_id = None
                return False
            i, pic = queue.pop(0)
            try:
                page = doc[i]
                s = self.THUMB_WIDTH / page.rect.width
                pix = page.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
                tex = Gdk.MemoryTexture.new(
                    pix.width, pix.height, Gdk.MemoryFormat.R8G8B8,
                    GLib.Bytes.new(pix.samples), pix.stride)
                pic.set_paintable(tex)
            except Exception:
                logger.error("thumbnail render failed:\n" + traceback.format_exc())
            if not queue:
                self._thumb_idle_id = None
                return False
            return True

        self._thumb_idle_id = GLib.idle_add(render_next)

    def _select_thumb(self, idx):
        """Highlight the current page's thumbnail and scroll it into view."""
        row = self._toc_list.get_row_at_index(idx)
        if row is None:
            return
        self._toc_list.select_row(row)
        ok, bounds = row.compute_bounds(self._toc_list)
        if ok:
            adj = self._toc_scroll.get_vadjustment()
            target = bounds.get_y() + bounds.get_height() / 2 - adj.get_page_size() / 2
            adj.set_value(max(0.0, min(target, adj.get_upper() - adj.get_page_size())))

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

    def _on_color_changed(self, btn, _param=None):
        if self._syncing_pen:
            return
        rgba = btn.get_rgba()
        rgb = (rgba.red, rgba.green, rgba.blue)
        if self.canvas.highlighter:
            self.canvas.hl_color = rgb
            self._hl_icon.queue_draw()
        else:
            self.canvas.pen_color = rgb

    def _toggle_highlighter(self):
        """Ctrl+H: flip the Pen/Highlighter segment pair."""
        if self._hl_toggle.get_active():
            self._pen_seg.set_active(True)
        else:
            self._hl_toggle.set_active(True)

    def _on_highlighter_toggled(self, btn):
        self.canvas.highlighter = btn.get_active()
        if self.canvas.highlighter:
            self._pen_btn.set_child(self._hl_icon)
            self._hl_icon.queue_draw()
            self._pen_btn.set_tooltip_text("Pen settings — highlighter active (Ctrl+H)")
        else:
            self._pen_btn.set_child(None)
            self._pen_btn.set_icon_name("document-edit-symbolic")
            self._pen_btn.set_tooltip_text("Pen settings")
        self._sync_pen_popover()

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
        if path == os.path.join(os.path.expanduser("~"), ".local", "share",
                                "sidemark", "scratchpad.pdf"):
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
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
                    self._recent_popover.popdown()
                    self.open_file(p)
                return _on_click
            row.connect("clicked", _make_open(path))
            box.append(row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(420)
        scroller.set_propagate_natural_height(True)
        scroller.set_propagate_natural_width(True)
        scroller.set_child(box)
        self._recent_popover.set_child(scroller)

    def open_file(self, path):
        if self._dirty:
            self._ask_save_then(lambda: self._do_open_file(path))
        else:
            self._do_open_file(path)

    def _do_open_file(self, path):
        if path.lower().endswith(".pptx"):
            self._convert_pptx_then_open(path)
            return
        if path.lower().endswith(".md"):
            self._open_markdown(path)
            return
        self._path = path
        self._notes_path = None
        self._is_untitled = False
        self._set_file_title(os.path.basename(path), path)
        self.notes_model = NotesModel()
        self.notes_model.pdf_name = os.path.basename(path)
        self.notes_model.load(notes_path_for(path))
        self._hide_search()
        self.canvas.load(path)  # fires on_page_changed → _restore_note for page 0
        self._populate_toc()
        self._clear_dirty()
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._notes_burst_open = False
        self._remember_recent(path)
        self._maybe_offer_recovery(path)

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
        # Notes-only mode: no PDF, load markdown directly into notes panel.
        self._path = None
        self._notes_path = md_path
        self._set_file_title(os.path.basename(md_path), md_path)
        self.notes_model = NotesModel()
        self.notes_model.load(md_path)
        self._page_label.set_label("—")
        # Show page 0 notes; canvas stays in "no PDF" placeholder state.
        buf = self._notes_view.get_buffer()
        buf.begin_irreversible_action()
        buf.set_text(self.notes_model.get(0))
        buf.end_irreversible_action()
        self._undo_timeline.clear()
        self._redo_timeline.clear()
        self._notes_burst_open = False
        self._burst_base = self.notes_model.get(0)
        self._remember_recent(md_path)

    def _on_new_pdf(self, _btn):
        if self._dirty:
            self._ask_save_then(self._create_blank)
        else:
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

    def _open_scratchpad(self):
        """Open (or create) the persistent scratchpad at ~/.local/share/sidemark/scratchpad.pdf."""
        data_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "sidemark")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "scratchpad.pdf")
        if not os.path.exists(path):
            surf = cairo.PDFSurface(path, 595, 842)
            cairo.Context(surf).show_page()
            surf.finish()
        self._do_open_file(path)
        self._set_file_title("Scratchpad", path)
        self._clear_dirty()

    def _on_save_as(self, after=None):
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
        check = Gtk.CheckButton(label="Include pages with no notes")
        check.set_active(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.append(check)

        dlg = Adw.AlertDialog(
            heading="Export with notes",
            body="Each page will be followed by its notes page.",
            extra_child=box,
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("export", "Choose file…")
        dlg.set_response_appearance("export", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("export")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._export_options_response, check)
        dlg.present(self)

    def _export_options_response(self, dlg, response, check):
        if response != "export":
            return
        include_empty = check.get_active()
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
        file_dlg.save(self, None, lambda d, r: self._export_file_done(d, r, include_empty))

    def _export_file_done(self, dialog, result, include_empty):
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
                                       include_empty, accent)
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

    def _convert_pptx_then_open(self, pptx_path):
        toast = Adw.Toast.new(f"Converting {os.path.basename(pptx_path)}…")
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
                GLib.idle_add(lambda: (toast.dismiss(), self.open_file(pdf_path)) and None)
            except FileNotFoundError:
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("Conversion failed",
                        "LibreOffice not found. Install it with:\n  pacman -S libreoffice-still")) and None)
            except subprocess.CalledProcessError as e:
                msg = e.stderr.decode(errors="replace") if e.stderr else str(e)
                GLib.idle_add(lambda: (toast.dismiss(),
                    self._show_error("Conversion failed", msg)) and None)

        threading.Thread(target=run, daemon=True).start()

    def _on_open(self, _btn):
        dialog = Gtk.FileDialog.new()
        f = Gtk.FileFilter()
        f.set_name("PDF / PPTX files")
        f.add_pattern("*.pdf")
        f.add_pattern("*.pptx")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)
        dialog.open(self, None, self._open_done)

    def _open_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self.open_file(file.get_path())
        except Exception as e:
            self._show_error("Could not open file", str(e))

    def _on_save(self, _btn=None, after=None):
        """Save; if `after` is given it runs only on a successful save
        (for untitled files that means after the save-as dialog completed)."""
        if self._is_untitled:
            self._on_save_as(after=after)
            return
        notes_file = notes_path_for(self._path) if self._path else self._notes_path
        if not self._path and not notes_file:
            return
        try:
            self._commit_note()
            if self._path:
                self.canvas.save(self._path)
            if notes_file:
                self.notes_model.save(notes_file)
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
            subprocess.Popen([sys.executable, os.path.abspath(__file__),
                              self._path, "--page", str(page)])
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
                self._on_new_pdf(None)
                return True
            if keyval == Gdk.KEY_c:
                if self.canvas._selected_words and not self._notes_view.has_focus():
                    self.canvas.copy_selection()
                    return True
            if keyval == Gdk.KEY_z:
                self._global_undo()
                return True
            if keyval == Gdk.KEY_backslash:
                self._notes_toggle.set_active(not self._notes_toggle.get_active())
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
        if keyval == Gdk.KEY_Page_Down:
            self._go_to_page(self.canvas.current_page_idx + 1)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self._go_to_page(self.canvas.current_page_idx - 1)
            return True
        return False


    # ── search ────────────────────────────────────────────────────────────────

    def _show_search(self):
        self._search_revealer.set_reveal_child(True)
        self._search_entry.grab_focus()

    def _hide_search(self):
        self._search_revealer.set_reveal_child(False)
        self._search_entry.set_text("")
        self._search_hits = {}
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
        self._search_matches = []
        self._search_current = -1
        if not query or not self.canvas.document:
            self._search_label.set_label("")
            self.canvas.search_rects = []
            self.canvas.search_current_rect = None
            self.canvas.queue_draw()
            return
        for i in range(self.canvas.n_pages):
            hits = self.canvas.document[i].search_for(query)
            if hits:
                self._search_hits[i] = hits
                for j in range(len(hits)):
                    self._search_matches.append((i, j))
        if not self._search_matches:
            self._search_label.set_label("0 / 0")
            self._search_entry.add_css_class("error")
            self.canvas.search_rects = []
            self.canvas.search_current_rect = None
            self.canvas.queue_draw()
            return
        self._search_entry.remove_css_class("error")
        # Start from the first match on or after the current page
        cur = self.canvas.current_page_idx
        start = next((k for k, (pi, _) in enumerate(self._search_matches) if pi >= cur), 0)
        self._go_to_match(start)

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
        page_idx, _ = self._search_matches[self._search_current]
        if page_idx != self.canvas.current_page_idx:
            self._commit_note()
            self.canvas.go_to_page(page_idx)  # fires _on_page_changed → _update_search_canvas
        else:
            self._update_search_canvas()
        self._search_label.set_label(f"{self._search_current + 1} / {n}")

    def _update_search_canvas(self):
        page_idx = self.canvas.current_page_idx
        self.canvas.search_rects = list(self._search_hits.get(page_idx, []))
        if (self._search_current >= 0 and self._search_matches
                and self._search_matches[self._search_current][0] == page_idx):
            pi, ri = self._search_matches[self._search_current]
            self.canvas.search_current_rect = self._search_hits[pi][ri]
        else:
            self.canvas.search_current_rect = None
        self.canvas.queue_draw()


class PDFEditorApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="de.hspitz.sidemark",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._initial_file = None
        self._initial_page = 0

    def do_activate(self):
        win = PDFEditorWindow(self)
        win.present()
        if self._initial_file:
            win.open_file(self._initial_file)
            if self._initial_page > 0:
                win._go_to_page(self._initial_page)
        else:
            GLib.idle_add(win._open_scratchpad)

    def run_with_file(self, path, page=0):
        self._initial_file = path
        self._initial_page = page
        return self.run([])


def main():
    args = sys.argv[1:]
    verbose = "--verbose" in args or "-v" in args
    args = [a for a in args if a not in ("--verbose", "-v")]
    _setup_logging(verbose=verbose)
    try:
        _prune_autosaves()
    except Exception:
        logger.error("autosave pruning failed:\n" + traceback.format_exc())
    initial_page = 0
    if "--page" in args:
        i = args.index("--page")
        try:
            initial_page = max(0, int(args[i + 1]))
            args = args[:i] + args[i + 2:]
        except (IndexError, ValueError):
            args = args[:i] + args[i + 1:]
    app = PDFEditorApp()
    if args:
        path = args[0]
        if not os.path.isfile(path):
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        sys.exit(app.run_with_file(path, page=initial_page))
    else:
        sys.exit(app.run([]))


if __name__ == "__main__":
    main()

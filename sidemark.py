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

LOG_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "sidemark", "logs")
_log_path = None
logger = logging.getLogger(__name__)


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
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.info("session started" + (" (verbose)" if verbose else ""))
    atexit.register(_cleanup_log)


def _cleanup_log():
    logger.info("session ended cleanly")
    logging.shutdown()
    try:
        if _log_path:
            os.remove(_log_path)
    except OSError:
        pass


import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Gtk, Adw, Gdk, GLib, Gio, GtkSource, Pango
import cairo
import fitz          # PyMuPDF
import numpy as np


class PDFCanvas(Gtk.DrawingArea):
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

        self.pen_color = (0.05, 0.05, 0.8)   # RGB — PDF ink annotations have no alpha
        self.pen_width = 2.0
        self.surround_color = (0.910, 0.867, 0.824)  # overridden by window with theme color
        self.zoom_accent = (0.52, 0.70, 0.30)        # overridden with theme accent

        self.on_page_changed = None    # callback(current_idx, n_pages)
        self.on_nav_button = None     # callback(delta: int) for back/forward buttons
        self.on_change = None         # callback() whenever strokes are modified
        self.on_anchor_placed = None   # callback(page_idx, pdf_x, pdf_y)
        self.on_anchor_clicked = None  # callback(anchor_index)

        self._anchors = {}         # {page_idx: [(x, y), ...]}
        self._active_anchors = set()  # indices highlighted on current page

        self.search_rects = []          # fitz.Rect hits for current page
        self.search_current_rect = None # the active match rect

        # zoom-to-region state
        self._zoom_stack = []          # [(scale, offset_x, offset_y), ...]
        self._zoom_selecting = False
        self._zoom_start = None        # screen (x, y)
        self._zoom_end = None          # screen (x, y), constrained

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
        total_annots = 0
        for i in range(self.n_pages):
            page = self.document[i]   # keep reference alive while reading annotations
            for annot in page.annots(types=[fitz.PDF_ANNOT_INK]):
                color = tuple(annot.colors.get("stroke", (0.05, 0.05, 0.8)))
                width = annot.border.get("width", 2.0)
                for polyline in annot.vertices:
                    if polyline:
                        self.all_strokes.setdefault(i, []).append({
                            "pts":   [tuple(pt) for pt in polyline],
                            "color": color,
                            "width": width,
                        })
                        total_annots += 1
        logger.info(f"load: {path} — {self.n_pages} pages, {total_annots} strokes loaded")
        self._load_page(0)

    def _load_page(self, idx):
        self.current_page_idx = idx
        self.page = self.document[idx]
        self.page_width  = self.page.rect.width
        self.page_height = self.page.rect.height
        self._page_surface = None
        self._surface_scale = 0.0
        if self._rerender_id is not None:
            GLib.source_remove(self._rerender_id)
            self._rerender_id = None
        self._needs_fit = True   # re-fit on first draw with real canvas dimensions
        self._page_words = self.page.get_text("words")   # cache for text selection
        self._selected_words = []
        self.queue_draw()
        if self.on_page_changed:
            self.on_page_changed(idx, self.n_pages)

    def go_to_page(self, idx):
        if not self.document:
            return
        idx = max(0, min(self.n_pages - 1, idx))
        if idx != self.current_page_idx:
            self._load_page(idx)

    @property
    def strokes(self):
        return self.all_strokes.setdefault(self.current_page_idx, [])

    # ── layout ───────────────────────────────────────────────────────────────

    def _fit_page(self):
        w = self.get_width() or 800
        h = self.get_height() or 600
        if self.page_width and self.page_height:
            self.scale = min(w / self.page_width, h / self.page_height) * 0.95
            self.offset_x = (w - self.page_width * self.scale) / 2
            self.offset_y = (h - self.page_height * self.scale) / 2

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
            to_draw.append({"pts": self.current_stroke,
                             "color": self.pen_color,
                             "width": self.pen_width})

        ctx.save()
        ctx.translate(self.offset_x, self.offset_y)
        ctx.scale(self.scale, self.scale)
        for stroke in to_draw:
            pts = stroke["pts"]
            r, g, b = stroke["color"]
            ctx.set_source_rgb(r, g, b)
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
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            r, g, b = self.zoom_accent
            radius = 8.0 / self.scale
            for i, (ax, ay) in enumerate(anchors):
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

    # ── input handlers ────────────────────────────────────────────────────────

    def _on_thumb_begin(self, gesture, sequence):
        if self._thumb_panning:
            pass
            self._thumb_panning = False
        else:
            logger.debug(f"thumb pan start ({self._mouse_x:.0f},{self._mouse_y:.0f})")
            self._thumb_panning = True
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
            self.offset_x -= dx * 30
            self.offset_y -= dy * 30
            self.queue_draw()
            return True
        factor = 0.9 if dy > 0 else 1.1
        mx, my = self._mouse_x, self._mouse_y
        pdf_x = (mx - self.offset_x) / self.scale
        pdf_y = (my - self.offset_y) / self.scale
        self.scale = max(0.1, min(20.0, self.scale * factor))
        self.offset_x = mx - pdf_x * self.scale
        self.offset_y = my - pdf_y * self.scale
        self._schedule_rerender()
        self.queue_draw()
        return True

    def _anchor_hit_test(self, sx, sy):
        """Return index of anchor circle under screen point, or None."""
        anchors = self._anchors.get(self.current_page_idx, [])
        for i, (ax, ay) in enumerate(anchors):
            scx, scy = self._pdf_to_screen(ax, ay)
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
            self._ignoring = True  # anchor placed by GestureClick, suppress drag
            return
        if state & Gdk.ModifierType.CONTROL_MASK:
            self._panning = True
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
        if self._erasing:
            self._erasing = False
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
                self.strokes.append({
                    "pts": self.current_stroke,
                    "color": self.pen_color,
                    "width": self.pen_width,
                })
                if self.on_change:
                    self.on_change()
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
        before = len(self.strokes)
        logger.debug(f"erase at pdf=({px:.1f},{py:.1f}) strokes={before}")
        self.all_strokes[self.current_page_idx] = [
            s for s in self.strokes
            if not self._stroke_hits(s["pts"], px, py, s["width"] / 2 + 3.0)
        ]
        if len(self.strokes) != before:
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
        self.offset_x = (cw - pdf_w * new_scale) / 2 - px1 * new_scale
        self.offset_y = (ch - pdf_h * new_scale) / 2 - py1 * new_scale
        self._schedule_rerender()

    def zoom_back(self):
        if self._zoom_stack:
            self.scale, self.offset_x, self.offset_y = self._zoom_stack.pop()
            self._schedule_rerender()
            self.queue_draw()

    def zoom_to_fit(self):
        """Reset to fit-page view, clearing the entire zoom history."""
        self._zoom_stack.clear()
        self._fit_page()
        self._schedule_rerender()
        self.queue_draw()

    def undo_last(self):
        if self.strokes:
            self.strokes.pop()
            if self.on_change:
                self.on_change()
            self.queue_draw()

    # ── save ──────────────────────────────────────────────────────────────────

    def save(self, path):
        """Save via self.document so structural changes (inserted pages) are preserved."""
        tmp = path + ".tmp"
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
                annot.update()
                total_written += 1
        logger.info(f"save: {path} — wrote {total_written} ink annotation(s)")
        self.document.save(tmp, garbage=4, deflate=True)
        os.replace(tmp, path)
        # Reopen so self.document reflects the saved state cleanly
        self.document = fitz.open(path)

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


_ANCHOR_RE = re.compile(r'<!--\s*anchor:(\d+):(\d+)\s*-->')
_MD_STRIP = [
    (re.compile(r'^#{1,6}\s+', re.MULTILINE), ''),
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    (re.compile(r'\*([^*\n]+?)\*'), r'\1'),
    (re.compile(r'`([^`\n]+?)`'), r'\1'),
]


def _export_pdf_with_notes(src_path, out_path, notes_model, include_empty, accent):
    src_doc = fitz.open(src_path)
    out_doc = fitz.open()
    r, g, b = accent
    anchor_color = (r, g, b)

    for page_idx in range(len(src_doc)):
        notes_text = notes_model.get(page_idx)
        anchor_matches = list(_ANCHOR_RE.finditer(notes_text))
        anchors = [(int(m.group(1)), int(m.group(2))) for m in anchor_matches]
        has_notes = bool(notes_text.strip())

        # Copy source page
        out_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
        out_page = out_doc[-1]

        # Draw numbered anchor markers on top of the page
        for i, (px, py) in enumerate(anchors):
            _draw_export_anchor(out_page, px, py, i + 1, anchor_color)

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
        self.canvas.on_anchor_placed = self._on_anchor_placed
        self.canvas.on_anchor_clicked = self._on_anchor_clicked

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

        new_btn = Gtk.Button(label="New")
        new_btn.set_tooltip_text("Create a new blank A4 PDF")
        new_btn.connect("clicked", self._on_new_pdf)
        header.pack_start(new_btn)

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

        undo_btn = Gtk.Button()
        undo_btn.set_icon_name("edit-undo-symbolic")
        undo_btn.set_tooltip_text("Undo (Ctrl+Z)")
        undo_btn.connect("clicked", lambda _: self.canvas.undo_last())
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
                    self._color_btn.set_rgba(rgba)
                    self.canvas.pen_color = (r, g, b)
                return _on_click

            swatch.connect("clicked", _make_handler(*rgb))
            swatches_box.append(swatch)
        popover_box.append(swatches_box)

        popover = Gtk.Popover()
        popover.set_child(popover_box)

        pen_btn = Gtk.MenuButton()
        pen_btn.set_icon_name("document-edit-symbolic")
        pen_btn.set_tooltip_text("Pen settings")
        pen_btn.set_popover(popover)
        header.pack_end(pen_btn)

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
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_start_child(canvas_box)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)
        self._paned.set_end_child(self._notes_box)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(True)
        self.connect("realize", self._on_realize)
        self.connect("close-request", self._on_close_request)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self._paned)
        self.set_child(self.toast_overlay)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

    # ── shortcuts popover ─────────────────────────────────────────────────────

    def _build_shortcuts_popover(self):
        shortcuts = [
            ("Draw",          None),
            ("Left-drag",     "Draw stroke"),
            ("Right-drag",    "Erase stroke"),
            ("Ctrl+Z",        "Undo last stroke"),
            ("Text",          None),

            ("Text",          None),
            ("Alt+Drag",      "Select text (word-level)"),
            ("Ctrl+C",        "Copy selected text"),
            ("Alt+Click",     "Open link under cursor"),
            ("Ctrl+Alt+Click","Place anchor marker in notes"),
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

        popover = Gtk.Popover()
        popover.set_child(grid)
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
        self.notes_model.shift_for_insert(self.canvas.current_page_idx + 1)
        self.canvas.add_blank_page()
        self._mark_dirty()

    def _delete_current_page(self):
        if not self.canvas.document:
            return
        if self.canvas.n_pages <= 1:
            toast = Adw.Toast.new("Cannot delete the only page")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
            return
        self.notes_model.shift_for_delete(self.canvas.current_page_idx)
        self.canvas.delete_current_page()
        self._mark_dirty()

    # ── dirty tracking ────────────────────────────────────────────────────────

    def _mark_dirty(self, *_):
        if not self._suppress_dirty:
            self._dirty = True

    def _on_notes_changed(self, _buf):
        self._mark_dirty()
        self._update_canvas_anchors()

    def _clear_dirty(self):
        self._dirty = False

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
                self._on_save()
                callback()
            elif r == "discard":
                callback()
            # cancel: do nothing
        dlg.connect("response", on_response)
        dlg.present(self)

    # ── page & notes handshake ────────────────────────────────────────────────

    def _on_page_changed(self, idx, n):
        self._page_label.set_label(f"{idx + 1} / {n}")
        self._restore_note()
        self._update_search_canvas()

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
        self._notes_view.get_buffer().set_text(text)
        self._suppress_dirty = False
        self._update_canvas_anchors()

    def _update_canvas_anchors(self):
        page_idx = self.canvas.current_page_idx
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        matches = list(re.finditer(r'<!--\s*anchor:(\d+):(\d+)\s*-->', text))
        self.canvas._anchors[page_idx] = [(int(m.group(1)), int(m.group(2))) for m in matches]
        self._anchor_line_nos = [text[:m.start()].count('\n') for m in matches]
        lines = text.split('\n')
        n_lines = len(lines)
        self._anchor_para_ends = []
        for ln in self._anchor_line_nos:
            end = n_lines - 1
            for j in range(ln + 1, n_lines):
                if not lines[j].strip():
                    end = j - 1
                    break
            self._anchor_para_ends.append(end)
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
        self._notes_view.grab_focus()
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

    def _on_notes_toggled(self, btn):
        if btn.get_active():
            self._notes_box.set_visible(True)
            w = self.get_width() or 1280
            pos = self._saved_pane_pos
            if pos > w - 150 or pos < 100:
                pos = int(w * 0.62)
                self._saved_pane_pos = pos
            self._paned.set_position(pos)
        else:
            pos = self._paned.get_position()
            w = self.get_width() or 1280
            if 100 < pos < w - 50:
                self._saved_pane_pos = pos
            self._notes_box.set_visible(False)

    # ── standard helpers ──────────────────────────────────────────────────────

    def _show_error(self, title, detail, tb=None):
        print(f"ERROR: {title}: {detail}", file=sys.stderr)
        if tb:
            print(tb, file=sys.stderr)
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
        self.canvas.pen_width = scale.get_value()

    def _on_color_changed(self, btn, _param=None):
        rgba = btn.get_rgba()
        self.canvas.pen_color = (rgba.red, rgba.green, rgba.blue)

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
        self._clear_dirty()

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
        self._notes_view.get_buffer().set_text(self.notes_model.get(0))

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

    def _on_save_as(self):
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
        dialog.save(self, None, self._save_as_done)

    def _save_as_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
            if not file:
                return
            path = file.get_path()
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            import shutil
            shutil.copy2(self._path, path)   # copy blank/temp as starting point
            old_tmp = self._path if self._is_untitled else None
            self._path = path
            self._is_untitled = False
            self._set_file_title(os.path.basename(path), path)
            self._on_save()
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

    def _on_save(self, _btn=None):
        if self._is_untitled:
            self._on_save_as()
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
            if keyval == Gdk.KEY_e:
                self._on_export()
                return True
            if keyval == Gdk.KEY_s:
                self._on_save()
                return True
            if keyval == Gdk.KEY_c:
                if self.canvas._selected_words and not self._notes_view.has_focus():
                    self.canvas.copy_selection()
                    return True
            if keyval == Gdk.KEY_z:
                self.canvas.undo_last()
                return True
            if keyval == Gdk.KEY_backslash:
                self._notes_toggle.set_active(not self._notes_toggle.get_active())
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

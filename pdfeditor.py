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
import urllib.parse

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_log_path = None
logger = logging.getLogger(__name__)


def _setup_logging():
    global _log_path
    os.makedirs(LOG_DIR, exist_ok=True)
    _log_path = os.path.join(LOG_DIR, f"session_{os.getpid()}.log")
    handler = logging.FileHandler(_log_path)
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.info("session started")
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
gi.require_version("Poppler", "0.18")
gi.require_version("GtkSource", "5")
from gi.repository import Gtk, Adw, Gdk, Poppler, GLib, Gio, GtkSource
import cairo


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

        self.pen_color = (0.05, 0.05, 0.8, 0.9)
        self.pen_width = 2.0
        self.surround_color = (0.910, 0.867, 0.824)  # overridden by window with theme color
        self.zoom_accent = (0.52, 0.70, 0.30)        # overridden with theme accent

        self.on_page_changed = None  # callback(current_idx, n_pages)
        self.on_nav_button = None    # callback(delta: int) for back/forward buttons

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


        self.set_draw_func(self._draw)
        self.set_focusable(True)
        self.set_can_focus(True)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
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


    # ── page management ──────────────────────────────────────────────────────

    def load(self, path):
        uri = GLib.filename_to_uri(os.path.abspath(path), None)
        self.document = Poppler.Document.new_from_file(uri, None)
        self.n_pages = self.document.get_n_pages()
        self.all_strokes = {}
        self.current_stroke = []
        self._load_page(0)

    def _load_page(self, idx):
        self.current_page_idx = idx
        self.page = self.document.get_page(idx)
        self.page_width, self.page_height = self.page.get_size()
        self._page_surface = None
        self._surface_scale = 0.0
        if self._rerender_id is not None:
            GLib.source_remove(self._rerender_id)
            self._rerender_id = None
        self._needs_fit = True   # re-fit on first draw with real canvas dimensions
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
        sf = self.get_scale_factor()          # 2 on HiDPI, 1 otherwise
        logical_scale = min(max(self.scale, 0.5), 4.0)
        device_scale = logical_scale * sf     # device pixels per PDF point
        w = max(1, int(self.page_width  * device_scale))
        h = max(1, int(self.page_height * device_scale))
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        surf.set_device_scale(sf, sf)         # expose surface in logical pixels
        sctx = cairo.Context(surf)
        sctx.scale(logical_scale, logical_scale)
        self.page.render(sctx)
        self._page_surface = surf
        self._surface_scale = logical_scale   # logical pixels per PDF point

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

        for stroke in to_draw:
            pts = stroke["pts"]
            r, g, b, a = stroke["color"]
            ctx.set_source_rgba(r, g, b, a)
            ctx.set_line_width(stroke["width"])
            if len(pts) < 2:
                if pts:
                    sx, sy = self._pdf_to_screen(*pts[0])
                    ctx.arc(sx, sy, stroke["width"] / 2, 0, 2 * math.pi)
                    ctx.fill()
                continue
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            ctx.move_to(*pts[0])
            for pt in pts[1:]:
                ctx.line_to(*pt)
            ctx.restore()
            ctx.stroke()

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
            logger.debug("thumb pan stop")
            self._thumb_panning = False
        else:
            logger.debug(f"thumb pan start at ({self._mouse_x:.0f},{self._mouse_y:.0f})")
            self._thumb_panning = True
            self._thumb_origin = (self._mouse_x, self._mouse_y)
            self._thumb_start_offset = (self.offset_x, self.offset_y)

    def _on_thumb_end(self, gesture, sequence):
        pass  # ignored — toggle mode, only begin matters

    def _on_motion(self, ctrl, x, y):
        if self._thumb_panning:
            self.offset_x = self._thumb_start_offset[0] + (x - self._thumb_origin[0])
            self.offset_y = self._thumb_start_offset[1] + (y - self._thumb_origin[1])
            self.queue_draw()
        self._mouse_x = x
        self._mouse_y = y

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

    def _on_drag_begin(self, gesture, start_x, start_y):
        if gesture.get_current_button() == 3:
            self._erasing = True
            self._panning = False
            self._text_selecting = False
            self._zoom_selecting = False
            self._erase_at(start_x, start_y)
            return
        btn = gesture.get_current_button()
        logger.debug(f"drag-begin button={btn}")
        if btn in (8, 9):
            self._ignoring = True
            if self.on_nav_button:
                self.on_nav_button(-1 if btn == 8 else 1)
            return
        if btn == 10:
            self._ignoring = True  # GestureSingle owns this sequence
            return
        self._ignoring = False
        self._erasing = False
        state = gesture.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self._panning = True
            self._pan_start_offset = (self.offset_x, self.offset_y)
            self._zoom_selecting = False
        elif state & Gdk.ModifierType.SHIFT_MASK:
            self._zoom_selecting = True
            self._zoom_start = (start_x, start_y)
            self._zoom_end = (start_x, start_y)
            self._panning = False
        else:
            self._zoom_selecting = False
            self._panning = False
            self.current_stroke = [self._screen_to_pdf(start_x, start_y)]

    def _on_drag_update(self, gesture, offset_x, offset_y):
        if self._ignoring:
            return
        logger.debug(f"drag-update btn={gesture.get_current_button()} panning={self._panning} offset=({offset_x:.0f},{offset_y:.0f})")
        sx, sy = gesture.get_start_point()[1], gesture.get_start_point()[2]
        if self._erasing:
            self._erase_at(sx + offset_x, sy + offset_y)
            return
        if self._panning:
            self.offset_x = self._pan_start_offset[0] + offset_x
            self.offset_y = self._pan_start_offset[1] + offset_y
            self.queue_draw()
            return
        if self._zoom_selecting:
            self._zoom_end = self._constrain_zoom_end(sx, sy, sx + offset_x, sy + offset_y)
        else:
            self.current_stroke.append(self._screen_to_pdf(sx + offset_x, sy + offset_y))
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        logger.debug(f"drag-end btn={gesture.get_current_button() if gesture else '?'} offset=({offset_x:.0f},{offset_y:.0f})")
        if self._ignoring:
            self._ignoring = False
            return
        if self._erasing:
            self._erasing = False
            return
        if self._panning:
            self._panning = False
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
            self.current_stroke = []
        self.queue_draw()


    def _erase_at(self, sx, sy):
        px, py = self._screen_to_pdf(sx, sy)
        before = len(self.strokes)
        self.all_strokes[self.current_page_idx] = [
            s for s in self.strokes
            if not self._stroke_hits(s["pts"], px, py, s["width"] / 2 + 3.0)
        ]
        if len(self.strokes) != before:
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
            self.queue_draw()

    # ── save ──────────────────────────────────────────────────────────────────

    def save(self, path):
        tmp = path + ".tmp"
        first = self.document.get_page(0)
        fw, fh = first.get_size()
        surface = cairo.PDFSurface(tmp, fw, fh)
        ctx = cairo.Context(surface)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)

        for i in range(self.n_pages):
            pg = self.document.get_page(i)
            pw, ph = pg.get_size()
            surface.set_size(pw, ph)
            pg.render(ctx)
            for stroke in self.all_strokes.get(i, []):
                pts = stroke["pts"]
                r, g, b, a = stroke["color"]
                ctx.set_source_rgba(r, g, b, a)
                ctx.set_line_width(stroke["width"])
                if len(pts) < 2:
                    if pts:
                        ctx.arc(pts[0][0], pts[0][1], stroke["width"] / 2, 0, 2 * math.pi)
                        ctx.fill()
                    continue
                ctx.move_to(*pts[0])
                for pt in pts[1:]:
                    ctx.line_to(*pt)
                ctx.stroke()
            ctx.show_page()

        surface.finish()
        os.replace(tmp, path)


def _load_theme():
    """Read background/foreground/accent from the current omarchy theme."""
    defaults = {
        "background": "#fdf6ee", "foreground": "#22211d", "accent": "#85b34c",
        "color1": "#df2b0d", "color3": "#8a6c3e", "color6": "#3d6b52", "color8": "#a09080",
    }
    path = os.path.expanduser("~/.config/omarchy/current/theme/colors.toml")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if " = " in line and not line.startswith("#"):
                    k, v = line.split(" = ", 1)
                    k = k.strip()
                    if k in defaults:
                        defaults[k] = v.strip().strip('"')
    except OSError:
        pass
    return defaults


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


def notes_path_for(pdf_path):
    return os.path.splitext(pdf_path)[0] + "-notes.md"


class NotesModel:
    """Per-page markdown notes, backed by a sidecar .md file."""

    def __init__(self):
        self._notes = {}

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
        # Format: <!-- page:N --> delimiters (invisible in markdown viewers)
        parts = re.split(r'<!--\s*page:(\d+)\s*-->', raw)
        for i in range(1, len(parts), 2):
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                self._notes[int(parts[i])] = content

    def save(self, path):
        sections = [
            f"<!-- page:{idx} -->\n\n{self._notes[idx].strip()}"
            for idx in sorted(self._notes)
            if self._notes[idx].strip()
        ]
        content = "\n\n".join(sections) + "\n" if sections else ""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
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
            "h1":     tag("h1",     weight=700, scale=1.5),
            "h2":     tag("h2",     weight=700, scale=1.25),
            "h3":     tag("h3",     weight=600, scale=1.1),
            "bold":   tag("bold",   weight=700),
            "italic": tag("italic", style=2),   # Pango.Style.ITALIC
            "code":   tag("code",   family="monospace",
                          background="#2d2d2d" if is_dark else "#f0f0f0",
                          foreground="#e06c75" if is_dark else "#c0392b"),
            "hide":   tag("hide",   invisible=True),
        }

        self._cursor_line = 0
        self._rehighlight_id = None
        buf.connect("notify::cursor-position", self._on_cursor_moved)
        buf.connect("changed", self._on_changed)

    # ── signal handlers ───────────────────────────────────────────────────────

    def _on_cursor_moved(self, buf, _):
        line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        if line != self._cursor_line:
            self._cursor_line = line
            self._schedule()

    def _on_changed(self, _buf):
        self._schedule()

    def _schedule(self):
        if self._rehighlight_id is not None:
            GLib.source_remove(self._rehighlight_id)
        self._rehighlight_id = GLib.timeout_add(30, self._rehighlight)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _rehighlight(self):
        self._rehighlight_id = None
        buf = self.get_buffer()
        s, e = buf.get_start_iter(), buf.get_end_iter()
        for tg in self._t.values():
            buf.remove_tag(tg, s, e)
        self._cursor_line = buf.get_iter_at_mark(buf.get_insert()).get_line()
        for ln in range(buf.get_line_count()):
            ls = buf.get_iter_at_line(ln)[1]
            le = ls.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            self._highlight_line(buf, ls, ln, buf.get_text(ls, le, False))
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


class PDFEditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PDF Editor")
        self.set_default_size(1280, 800)
        self._path = None
        self._notes_path = None   # set when a .md file is opened without an associated PDF
        self.notes_model = NotesModel()

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

        self.canvas.on_nav_button = lambda d: self._go_to_page(self.canvas.current_page_idx + d)

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

        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav_box.add_css_class("linked")
        nav_box.append(prev_btn)
        nav_box.append(self._page_label)
        nav_box.append(next_btn)
        header.set_title_widget(nav_box)

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
        self.canvas.pen_color = (*acc, 1.0)
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
                    self.canvas.pen_color = (r, g, b, 1.0)
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

        # open notes in Obsidian
        self._obsidian_btn = Gtk.Button()
        self._obsidian_btn.set_icon_name("text-editor-symbolic")
        self._obsidian_btn.set_tooltip_text("Open notes in Obsidian")
        self._obsidian_btn.set_sensitive(False)
        self._obsidian_btn.connect("clicked", self._on_open_obsidian)
        header.pack_end(self._obsidian_btn)

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
        notes_scroll.set_child(self._notes_view)
        self._notes_box.append(notes_scroll)

        # ── split pane ────────────────────────────────────────────────────────
        self._saved_pane_pos = 800
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_start_child(self.canvas)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)
        self._paned.set_end_child(self._notes_box)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(True)
        self.connect("realize", self._on_realize)

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

            ("Navigate",      None),
            ("PageDown",      "Next page"),
            ("PageUp",        "Previous page"),
            ("Zoom & Pan",    None),
            ("Ctrl+Scroll",   "Zoom in / out"),
            ("Scroll",        "Pan"),
            ("Ctrl+Drag",     "Pan"),
            ("Shift+Drag",    "Zoom to region"),
            ("Shift+Click",   "Fit page"),
            ("File",          None),
            ("Ctrl+S",        "Save"),
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

    def _on_realize(self, _widget):
        self._pane_init_tries = 0
        GLib.timeout_add(50, self._try_init_pane)

    def _try_init_pane(self):
        w = self.get_width()
        self._pane_init_tries += 1
        if w < 200 and self._pane_init_tries < 20:
            return True  # retry until window has real width
        pos = int(max(w, 1280) * 0.62)
        self._saved_pane_pos = pos
        self._paned.set_position(pos)
        return False

    def _on_page_changed(self, idx, n):
        self._page_label.set_label(f"{idx + 1} / {n}")
        self._restore_note()

    def _go_to_page(self, idx):
        self._commit_note()
        self.canvas.go_to_page(idx)

    def _commit_note(self):
        if not self._path and not self._notes_path:
            return
        buf = self._notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self.notes_model.set(self.canvas.current_page_idx, text)

    def _restore_note(self):
        text = self.notes_model.get(self.canvas.current_page_idx)
        self._notes_view.get_buffer().set_text(text)

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

    def _show_error(self, title, detail):
        dlg = Adw.AlertDialog.new(title, detail)
        dlg.add_response("close", "Close")
        dlg.add_response("copy", "Copy Error")
        dlg.set_default_response("close")
        def on_response(d, r):
            if r == "copy":
                Gdk.Display.get_default().get_clipboard().set_text(detail, -1)
        dlg.connect("response", on_response)
        dlg.present(self)

    def _on_width_changed(self, scale):
        self.canvas.pen_width = scale.get_value()

    def _on_color_changed(self, btn, _param=None):
        rgba = btn.get_rgba()
        self.canvas.pen_color = (rgba.red, rgba.green, rgba.blue, rgba.alpha)

    def open_file(self, path):
        if path.lower().endswith(".pptx"):
            self._convert_pptx_then_open(path)
            return
        if path.lower().endswith(".md"):
            self._open_markdown(path)
            return
        self._path = path
        self._notes_path = None
        self.set_title(f"PDF Editor — {os.path.basename(path)}")
        self.notes_model = NotesModel()
        self.notes_model.load(notes_path_for(path))
        self.canvas.load(path)  # fires on_page_changed → _restore_note for page 0
        self._obsidian_btn.set_sensitive(True)

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
        self.set_title(f"PDF Editor — {os.path.basename(md_path)}")
        self.notes_model = NotesModel()
        self.notes_model.load(md_path)
        self._page_label.set_label("—")
        self._obsidian_btn.set_sensitive(True)
        # Show page 0 notes; canvas stays in "no PDF" placeholder state.
        self._notes_view.get_buffer().set_text(self.notes_model.get(0))

    def _on_new_pdf(self, _btn):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Create new blank PDF")
        dialog.set_initial_name("notes.pdf")
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        dialog.save(self, None, self._new_pdf_done)

    def _new_pdf_done(self, dialog, result):
        try:
            file = dialog.save_finish(result)
            if not file:
                return
            path = file.get_path()
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            surf = cairo.PDFSurface(path, 595, 842)  # A4 in PDF points (72 dpi)
            cairo.Context(surf).show_page()
            surf.finish()
            self.open_file(path)
        except Exception as e:
            self._show_error("Could not create PDF", str(e))

    def _on_open_obsidian(self, _btn):
        notes = notes_path_for(self._path) if self._path else self._notes_path
        if not notes:
            return
        if not os.path.exists(notes):
            self._commit_note()
            self.notes_model.save(notes)
        uri = "obsidian://open?path=" + urllib.parse.quote(os.path.abspath(notes))
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as e:
            self._show_error("Could not open Obsidian", str(e))

    def _convert_pptx_then_open(self, pptx_path):
        toast = Adw.Toast.new(f"Converting {os.path.basename(pptx_path)}…")
        toast.set_timeout(0)
        self.toast_overlay.add_toast(toast)
        out_dir = tempfile.mkdtemp(prefix="pdfeditor-")
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
        notes_file = notes_path_for(self._path) if self._path else self._notes_path
        if not self._path and not notes_file:
            return
        try:
            self._commit_note()
            if self._path:
                self.canvas.save(self._path)
            if notes_file:
                self.notes_model.save(notes_file)
            toast = Adw.Toast.new("Saved")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
        except Exception as e:
            self._show_error("Save failed", str(e))

    def _on_key(self, ctrl, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_s:
                self._on_save()
                return True
            if keyval == Gdk.KEY_z:
                self.canvas.undo_last()
                return True
            if keyval == Gdk.KEY_backslash:
                self._notes_toggle.set_active(not self._notes_toggle.get_active())
                return True
        if keyval == Gdk.KEY_Page_Down:
            self._go_to_page(self.canvas.current_page_idx + 1)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self._go_to_page(self.canvas.current_page_idx - 1)
            return True
        return False


class PDFEditorApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="de.hspitz.pdfeditor",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._initial_file = None

    def do_activate(self):
        win = PDFEditorWindow(self)
        win.present()
        if self._initial_file:
            win.open_file(self._initial_file)

    def run_with_file(self, path):
        self._initial_file = path
        return self.run([])


def main():
    _setup_logging()
    app = PDFEditorApp()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not os.path.isfile(path):
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        sys.exit(app.run_with_file(path))
    else:
        sys.exit(app.run([]))


if __name__ == "__main__":
    main()

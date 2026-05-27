#!/usr/bin/env /usr/bin/python3
import sys
import os
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gtk, Gdk, Poppler, GLib, Gio
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

        self.on_page_changed = None  # callback(current_idx, n_pages)

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
        self._fit_page()
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

    def _screen_to_pdf(self, sx, sy):
        return (sx - self.offset_x) / self.scale, (sy - self.offset_y) / self.scale

    def _pdf_to_screen(self, px, py):
        return px * self.scale + self.offset_x, py * self.scale + self.offset_y

    # ── drawing ───────────────────────────────────────────────────────────────

    def _draw(self, area, ctx, width, height):
        ctx.set_source_rgb(0.45, 0.45, 0.45)
        ctx.paint()

        if self.page is None:
            return

        if self.offset_x == 0 and self.offset_y == 0 and self.scale == 1.0:
            self._fit_page()

        ctx.set_source_rgb(1, 1, 1)
        ctx.rectangle(self.offset_x, self.offset_y,
                      self.page_width * self.scale, self.page_height * self.scale)
        ctx.fill()

        ctx.save()
        ctx.translate(self.offset_x, self.offset_y)
        ctx.scale(self.scale, self.scale)
        self.page.render(ctx)
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

    # ── input handlers ────────────────────────────────────────────────────────

    def _on_motion(self, ctrl, x, y):
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
        self.queue_draw()
        return True

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.current_stroke = [self._screen_to_pdf(start_x, start_y)]

    def _on_drag_update(self, gesture, offset_x, offset_y):
        sx, sy = gesture.get_start_point()[1], gesture.get_start_point()[2]
        self.current_stroke.append(self._screen_to_pdf(sx + offset_x, sy + offset_y))
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        if self.current_stroke:
            self.strokes.append({
                "pts": self.current_stroke,
                "color": self.pen_color,
                "width": self.pen_width,
            })
        self.current_stroke = []
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


class PDFEditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PDF Editor")
        self.set_default_size(960, 780)
        self._path = None

        # ── header bar ────────────────────────────────────────────────────────
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        open_btn = Gtk.Button(label="Open")
        open_btn.connect("clicked", self._on_open)
        header.pack_start(open_btn)

        # page navigation
        prev_btn = Gtk.Button(label="◀")
        prev_btn.connect("clicked", lambda _: self.canvas.go_to_page(self.canvas.current_page_idx - 1))
        self._page_label = Gtk.Label(label="—")
        self._page_label.set_width_chars(7)
        next_btn = Gtk.Button(label="▶")
        next_btn.connect("clicked", lambda _: self.canvas.go_to_page(self.canvas.current_page_idx + 1))

        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        nav_box.append(prev_btn)
        nav_box.append(self._page_label)
        nav_box.append(next_btn)
        header.set_title_widget(nav_box)

        undo_btn = Gtk.Button(label="Undo")
        undo_btn.connect("clicked", lambda _: self.canvas.undo_last())
        header.pack_end(undo_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self._on_save)
        header.pack_end(save_btn)

        # ── pen toolbar ───────────────────────────────────────────────────────
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(6)

        toolbar.append(Gtk.Label(label="Width:"))
        self._width_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 20, 0.5)
        self._width_scale.set_value(2.0)
        self._width_scale.set_size_request(140, -1)
        self._width_scale.set_draw_value(True)
        self._width_scale.connect("value-changed", self._on_width_changed)
        toolbar.append(self._width_scale)

        toolbar.append(Gtk.Label(label="Color:"))
        self._color_btn = Gtk.ColorButton()
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = 0.05, 0.05, 0.8, 0.9
        self._color_btn.set_rgba(rgba)
        self._color_btn.connect("color-set", self._on_color_changed)
        toolbar.append(self._color_btn)

        # ── canvas + toast overlay ────────────────────────────────────────────
        self.canvas = PDFCanvas()
        self.canvas.set_vexpand(True)
        self.canvas.set_hexpand(True)
        self.canvas.on_page_changed = self._update_page_label

        self.toast_overlay = Gtk.ToastOverlay()
        self.toast_overlay.set_child(self.canvas)
        self.toast_overlay.set_vexpand(True)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(self.toast_overlay)
        self.set_child(root)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_page_label(self, idx, n):
        self._page_label.set_label(f"{idx + 1} / {n}")

    def _on_width_changed(self, scale):
        self.canvas.pen_width = scale.get_value()

    def _on_color_changed(self, btn):
        rgba = btn.get_rgba()
        self.canvas.pen_color = (rgba.red, rgba.green, rgba.blue, rgba.alpha)

    def open_file(self, path):
        self._path = path
        self.set_title(f"PDF Editor — {os.path.basename(path)}")
        self.canvas.load(path)

    def _on_open(self, _btn):
        dialog = Gtk.FileDialog.new()
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)
        dialog.open(self, None, self._open_done)

    def _open_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self.open_file(file.get_path())
        except Exception:
            pass

    def _on_save(self, _btn=None):
        if not self._path:
            return
        try:
            self.canvas.save(self._path)
            toast = Gtk.Toast.new("Saved successfully")
            toast.set_timeout(2)
            self.toast_overlay.add_toast(toast)
        except Exception as e:
            toast = Gtk.Toast.new(f"Save failed: {e}")
            toast.set_timeout(4)
            self.toast_overlay.add_toast(toast)

    def _on_key(self, ctrl, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_s:
                self._on_save()
                return True
            if keyval == Gdk.KEY_z:
                self.canvas.undo_last()
                return True
        if keyval == Gdk.KEY_Page_Down:
            self.canvas.go_to_page(self.canvas.current_page_idx + 1)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self.canvas.go_to_page(self.canvas.current_page_idx - 1)
            return True
        return False


class PDFEditorApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="de.hspitz.pdfeditor")
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

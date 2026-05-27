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
        self.page = None
        self.page_width = 0
        self.page_height = 0

        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

        self.strokes = []        # finished strokes: list of list of (pdf_x, pdf_y)
        self.current_stroke = [] # points being drawn right now

        self.set_draw_func(self._draw)
        self.set_focusable(True)
        self.set_can_focus(True)

        # Track mouse position for zoom-to-cursor
        self._mouse_x = 0.0
        self._mouse_y = 0.0
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        # Ctrl+scroll → zoom
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.BOTH_AXES |
            Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        # Drag → draw strokes (handles both mouse and touch)
        drag = Gtk.GestureDrag.new()
        drag.set_button(0)  # all buttons
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def load(self, path):
        uri = GLib.filename_to_uri(os.path.abspath(path), None)
        self.document = Poppler.Document.new_from_file(uri, None)
        self.page = self.document.get_page(0)
        self.page_width, self.page_height = self.page.get_size()
        self.strokes = []
        self.current_stroke = []
        # Fit page to initial window size; defer to first draw if allocation not ready
        self._fit_page()
        self.queue_draw()

    def _fit_page(self):
        w = self.get_width() or 800
        h = self.get_height() or 600
        if self.page_width and self.page_height:
            self.scale = min(w / self.page_width, h / self.page_height) * 0.95
            self.offset_x = (w - self.page_width * self.scale) / 2
            self.offset_y = (h - self.page_height * self.scale) / 2

    def _draw(self, area, ctx, width, height):
        # White background
        ctx.set_source_rgb(0.5, 0.5, 0.5)
        ctx.paint()

        if self.page is None:
            return

        # Fit on first draw if offsets are still 0 and scale is 1
        if self.offset_x == 0 and self.offset_y == 0 and self.scale == 1.0:
            self._fit_page()

        # Draw page background (white)
        px = self.offset_x
        py = self.offset_y
        pw = self.page_width * self.scale
        ph = self.page_height * self.scale
        ctx.set_source_rgb(1, 1, 1)
        ctx.rectangle(px, py, pw, ph)
        ctx.fill()

        # Render PDF page
        ctx.save()
        ctx.translate(self.offset_x, self.offset_y)
        ctx.scale(self.scale, self.scale)
        self.page.render(ctx)
        ctx.restore()

        # Draw stored strokes
        ctx.set_source_rgba(0.05, 0.05, 0.8, 0.9)
        ctx.set_line_width(2.0 / self.scale * self.scale)  # constant visual width
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)

        for stroke in self.strokes + ([self.current_stroke] if self.current_stroke else []):
            if len(stroke) < 2:
                continue
            ctx.save()
            ctx.translate(self.offset_x, self.offset_y)
            ctx.scale(self.scale, self.scale)
            ctx.move_to(*stroke[0])
            for pt in stroke[1:]:
                ctx.line_to(*pt)
            ctx.restore()
            ctx.stroke()

        # Draw single-point dots
        for stroke in self.strokes + ([self.current_stroke] if self.current_stroke else []):
            if len(stroke) == 1:
                sx, sy = self._pdf_to_screen(*stroke[0])
                ctx.arc(sx, sy, 1.5, 0, 2 * math.pi)
                ctx.fill()

    def _screen_to_pdf(self, sx, sy):
        return (sx - self.offset_x) / self.scale, (sy - self.offset_y) / self.scale

    def _pdf_to_screen(self, px, py):
        return px * self.scale + self.offset_x, py * self.scale + self.offset_y

    def _on_motion(self, ctrl, x, y):
        self._mouse_x = x
        self._mouse_y = y

    def _on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            # Pan without ctrl
            self.offset_x -= dx * 30
            self.offset_y -= dy * 30
            self.queue_draw()
            return True

        factor = 0.9 if dy > 0 else 1.1
        mx, my = self._mouse_x, self._mouse_y
        # Keep the PDF point under mouse fixed
        pdf_x = (mx - self.offset_x) / self.scale
        pdf_y = (my - self.offset_y) / self.scale
        self.scale = max(0.1, min(20.0, self.scale * factor))
        self.offset_x = mx - pdf_x * self.scale
        self.offset_y = my - pdf_y * self.scale
        self.queue_draw()
        return True

    def _on_drag_begin(self, gesture, start_x, start_y):
        pdf_pt = self._screen_to_pdf(start_x, start_y)
        self.current_stroke = [pdf_pt]

    def _on_drag_update(self, gesture, offset_x, offset_y):
        start_x, start_y = gesture.get_start_point()[1], gesture.get_start_point()[2]
        sx = start_x + offset_x
        sy = start_y + offset_y
        self.current_stroke.append(self._screen_to_pdf(sx, sy))
        self.queue_draw()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        if self.current_stroke:
            self.strokes.append(self.current_stroke)
        self.current_stroke = []
        self.queue_draw()

    def save(self, path):
        tmp = path + ".tmp"
        surface = cairo.PDFSurface(tmp, self.page_width, self.page_height)
        ctx = cairo.Context(surface)
        self.page.render(ctx)

        ctx.set_source_rgba(0.05, 0.05, 0.8, 0.9)
        ctx.set_line_width(2.0)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_join(cairo.LINE_JOIN_ROUND)
        for stroke in self.strokes:
            if len(stroke) < 2:
                if stroke:
                    ctx.arc(stroke[0][0], stroke[0][1], 1.5, 0, 2 * math.pi)
                    ctx.fill()
                continue
            ctx.move_to(*stroke[0])
            for pt in stroke[1:]:
                ctx.line_to(*pt)
            ctx.stroke()

        surface.finish()
        os.replace(tmp, path)

    def undo_last(self):
        if self.strokes:
            self.strokes.pop()
            self.queue_draw()


class PDFEditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PDF Editor")
        self.set_default_size(900, 700)
        self._path = None

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        open_btn = Gtk.Button(label="Open")
        open_btn.connect("clicked", self._on_open)
        header.pack_start(open_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.connect("clicked", self._on_save)
        header.pack_end(save_btn)

        undo_btn = Gtk.Button(label="Undo")
        undo_btn.connect("clicked", lambda _: self.canvas.undo_last())
        header.pack_end(undo_btn)

        self.canvas = PDFCanvas()
        self.canvas.set_vexpand(True)
        self.canvas.set_hexpand(True)
        self.set_child(self.canvas)

        # Keyboard shortcut: Ctrl+S → save, Ctrl+Z → undo
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key)
        self.add_controller(key_ctrl)

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
        if self._path:
            self.canvas.save(self._path)

    def _on_key(self, ctrl, keyval, keycode, state):
        if state & Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_s:
                self._on_save()
                return True
            if keyval == Gdk.KEY_z:
                self.canvas.undo_last()
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

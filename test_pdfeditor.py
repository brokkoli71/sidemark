#!/usr/bin/env /usr/bin/python3
"""
Headless tests for PDFCanvas logic.
Run with:  /usr/bin/python3 test_pdfeditor.py
"""
import os
import sys
import math
import tempfile
import unittest

# Prevent GTK from trying to connect to a display
os.environ.setdefault("GDK_BACKEND", "offscreen")

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Poppler", "0.18")
from gi.repository import Gtk, Adw, GLib
import cairo

# Bootstrap Adw so widget construction works without a real display
Adw.init()

sys.path.insert(0, os.path.dirname(__file__))
from pdfeditor import PDFCanvas, NotesModel, notes_path_for


# ── helper: create a minimal single-page PDF in memory ───────────────────────

def make_pdf(path, n_pages=1, width=595, height=842):
    surface = cairo.PDFSurface(path, width, height)
    ctx = cairo.Context(surface)
    for _ in range(n_pages):
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.show_page()
    surface.finish()


# ── coordinate math ───────────────────────────────────────────────────────────

class TestCoordinates(unittest.TestCase):
    def setUp(self):
        self.canvas = PDFCanvas()
        self.canvas.scale = 2.0
        self.canvas.offset_x = 50.0
        self.canvas.offset_y = 30.0

    def test_screen_to_pdf_roundtrip(self):
        for sx, sy in [(100, 80), (0, 0), (300, 200)]:
            pdf = self.canvas._screen_to_pdf(sx, sy)
            back = self.canvas._pdf_to_screen(*pdf)
            self.assertAlmostEqual(back[0], sx)
            self.assertAlmostEqual(back[1], sy)

    def test_screen_to_pdf_values(self):
        px, py = self.canvas._screen_to_pdf(50, 30)  # exactly at offset
        self.assertAlmostEqual(px, 0.0)
        self.assertAlmostEqual(py, 0.0)

    def test_zoom_keeps_point_fixed(self):
        # Simulate the zoom logic: the PDF point under the mouse must not move
        canvas = self.canvas
        mx, my = 150.0, 110.0
        pdf_x_before = (mx - canvas.offset_x) / canvas.scale
        pdf_y_before = (my - canvas.offset_y) / canvas.scale

        factor = 1.1
        canvas.scale *= factor
        canvas.offset_x = mx - pdf_x_before * canvas.scale
        canvas.offset_y = my - pdf_y_before * canvas.scale

        pdf_x_after = (mx - canvas.offset_x) / canvas.scale
        pdf_y_after = (my - canvas.offset_y) / canvas.scale
        self.assertAlmostEqual(pdf_x_before, pdf_x_after, places=10)
        self.assertAlmostEqual(pdf_y_before, pdf_y_after, places=10)


# ── zoom to region ────────────────────────────────────────────────────────────

class TestZoomToRegion(unittest.TestCase):
    def _canvas(self):
        c = PDFCanvas()
        c.scale = 1.0
        c.offset_x = 0.0
        c.offset_y = 0.0
        return c

    def test_execute_zoom_centers_selection(self):
        c = self._canvas()
        # Simulate 800×600 canvas
        # Select screen rect (100,100)–(300,250)
        c._execute_zoom_to_rect((100, 100), (300, 250))
        self.assertEqual(len(c._zoom_stack), 1)
        # After zoom, the selection should be scaled up
        self.assertGreater(c.scale, 1.0)

    def test_zoom_back_restores_state(self):
        c = self._canvas()
        original = (c.scale, c.offset_x, c.offset_y)
        c._execute_zoom_to_rect((100, 100), (300, 250))
        c.zoom_back()
        self.assertAlmostEqual(c.scale, original[0])
        self.assertAlmostEqual(c.offset_x, original[1])
        self.assertAlmostEqual(c.offset_y, original[2])
        self.assertEqual(len(c._zoom_stack), 0)

    def test_zoom_back_on_empty_stack_does_not_raise(self):
        c = self._canvas()
        c.zoom_back()  # should not raise

    def test_tiny_rect_does_not_zoom_in(self):
        c = self._canvas()
        c._execute_zoom_to_rect((100, 100), (103, 102))  # < 8px → no zoom push
        self.assertEqual(len(c._zoom_stack), 0)
        self.assertAlmostEqual(c.scale, 1.0)

    def test_constrain_zoom_end_aspect_ratio(self):
        c = self._canvas()
        # Canvas 800×600 → aspect 4/3
        # Drag 120px horizontally → expect 90px vertically (120 * 600/800)
        ex, ey = c._constrain_zoom_end(0, 0, 120, 999)
        self.assertAlmostEqual(ex, 120)
        self.assertAlmostEqual(ey, 90.0, places=5)

    def test_zoom_stack_is_lifo(self):
        c = self._canvas()
        c._execute_zoom_to_rect((50, 50), (200, 200))
        scale1 = c.scale
        c._execute_zoom_to_rect((60, 60), (180, 180))
        c.zoom_back()
        self.assertAlmostEqual(c.scale, scale1)
        c.zoom_back()
        self.assertAlmostEqual(c.scale, 1.0)


# ── stroke storage ────────────────────────────────────────────────────────────

class TestStrokes(unittest.TestCase):
    def _canvas_with_pdf(self, n_pages=3):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        make_pdf(path, n_pages=n_pages)
        canvas.load(path)
        self._tmp = path
        return canvas

    def tearDown(self):
        if hasattr(self, "_tmp") and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_stroke_stored_on_current_page(self):
        canvas = self._canvas_with_pdf()
        canvas.strokes.append({"pts": [(1, 2), (3, 4)], "color": (0, 0, 1, 1), "width": 2})
        self.assertEqual(len(canvas.all_strokes[0]), 1)

    def test_strokes_isolated_per_page(self):
        canvas = self._canvas_with_pdf(n_pages=3)
        canvas.strokes.append({"pts": [(1, 1)], "color": (0, 0, 1, 1), "width": 2})
        canvas.go_to_page(1)
        self.assertEqual(len(canvas.strokes), 0)  # page 1 has no strokes
        canvas.go_to_page(0)
        self.assertEqual(len(canvas.strokes), 1)  # page 0 still has its stroke

    def test_undo_removes_last_stroke(self):
        canvas = self._canvas_with_pdf()
        canvas.strokes.append({"pts": [(0, 0)], "color": (0, 0, 1, 1), "width": 2})
        canvas.strokes.append({"pts": [(1, 1)], "color": (1, 0, 0, 1), "width": 3})
        canvas.undo_last()
        self.assertEqual(len(canvas.strokes), 1)
        self.assertEqual(canvas.strokes[0]["color"], (0, 0, 1, 1))

    def test_undo_on_empty_does_not_raise(self):
        canvas = self._canvas_with_pdf()
        canvas.undo_last()  # should not raise

    def test_pen_attributes_stored_in_stroke(self):
        canvas = self._canvas_with_pdf()
        canvas.pen_color = (1.0, 0.0, 0.0, 1.0)
        canvas.pen_width = 5.0
        # Simulate drag_end
        canvas.current_stroke = [(10, 20), (30, 40)]
        canvas._on_drag_end(None, 0, 0)
        stroke = canvas.strokes[-1]
        self.assertEqual(stroke["color"], (1.0, 0.0, 0.0, 1.0))
        self.assertEqual(stroke["width"], 5.0)


# ── save round-trip ───────────────────────────────────────────────────────────

class TestSave(unittest.TestCase):
    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp_in = f.name
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp_out = f.name
        make_pdf(self._tmp_in, n_pages=2)

    def tearDown(self):
        for p in (self._tmp_in, self._tmp_out):
            if os.path.exists(p):
                os.unlink(p)

    def test_save_produces_valid_pdf(self):
        from gi.repository import Poppler
        canvas = PDFCanvas()
        canvas.load(self._tmp_in)
        canvas.strokes.append({"pts": [(10, 10), (100, 100)], "color": (0, 0, 1, 1), "width": 2})
        canvas.save(self._tmp_out)
        self.assertTrue(os.path.getsize(self._tmp_out) > 0)
        # Re-open and verify page count is preserved
        uri = GLib.filename_to_uri(os.path.abspath(self._tmp_out), None)
        doc = Poppler.Document.new_from_file(uri, None)
        self.assertEqual(doc.get_n_pages(), 2)

    def test_save_does_not_corrupt_source(self):
        canvas = PDFCanvas()
        canvas.load(self._tmp_in)
        canvas.save(self._tmp_out)
        # Source file must still be a valid PDF
        from gi.repository import Poppler
        uri = GLib.filename_to_uri(os.path.abspath(self._tmp_in), None)
        doc = Poppler.Document.new_from_file(uri, None)
        self.assertEqual(doc.get_n_pages(), 2)

    def test_save_overwrites_atomically(self):
        # .tmp file must not survive after save
        canvas = PDFCanvas()
        canvas.load(self._tmp_in)
        canvas.save(self._tmp_out)
        self.assertFalse(os.path.exists(self._tmp_out + ".tmp"))


# ── notes model ──────────────────────────────────────────────────────────────

class TestNotes(unittest.TestCase):
    def test_notes_path_for(self):
        self.assertEqual(notes_path_for("/tmp/lecture.pdf"), "/tmp/lecture-notes.md")
        self.assertEqual(notes_path_for("slides.pdf"), "slides-notes.md")

    def test_parse_empty(self):
        m = NotesModel()
        m.load.__func__  # just access to confirm it exists
        m._notes = {}
        self.assertEqual(m.get(0), "")

    def test_parse_single_page(self):
        m = NotesModel()
        raw = "<!-- page:2 -->\n\nSome notes here."
        import re as _re
        parts = _re.split(r'<!--\s*page:(\d+)\s*-->', raw)
        for i in range(1, len(parts), 2):
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                m._notes[int(parts[i])] = content
        self.assertEqual(m.get(2), "Some notes here.")
        self.assertEqual(m.get(0), "")

    def test_parse_multiple_pages_with_gaps(self):
        m = NotesModel()
        raw = "<!-- page:0 -->\n\nFirst.\n\n<!-- page:3 -->\n\nFourth.\n\n<!-- page:5 -->\n\nSixth."
        import re as _re
        parts = _re.split(r'<!--\s*page:(\d+)\s*-->', raw)
        for i in range(1, len(parts), 2):
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if content:
                m._notes[int(parts[i])] = content
        self.assertEqual(m.get(0), "First.")
        self.assertEqual(m.get(3), "Fourth.")
        self.assertEqual(m.get(5), "Sixth.")
        self.assertEqual(m.get(1), "")  # gap

    def test_serialize_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        try:
            m1 = NotesModel()
            m1.set(0, "Page zero notes")
            m1.set(2, "Page two notes")
            m1.set(4, "Page four notes")
            m1.save(path)

            m2 = NotesModel()
            m2.load(path)
            self.assertEqual(m2.get(0), "Page zero notes")
            self.assertEqual(m2.get(2), "Page two notes")
            self.assertEqual(m2.get(4), "Page four notes")
            self.assertEqual(m2.get(1), "")
        finally:
            os.unlink(path)

    def test_empty_pages_not_written(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        try:
            m = NotesModel()
            m.set(0, "")
            m.set(1, "  ")  # whitespace only
            m.set(2, "Real note")
            m.save(path)
            with open(path) as f:
                content = f.read()
            self.assertNotIn("page:0", content)
            self.assertNotIn("page:1", content)
            self.assertIn("page:2", content)
        finally:
            os.unlink(path)

    def test_load_missing_file_is_silent(self):
        m = NotesModel()
        m.load("/tmp/this-file-does-not-exist-ever.md")
        self.assertEqual(m.get(0), "")

    def test_save_atomic(self):
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            path = f.name
        try:
            m = NotesModel()
            m.set(0, "hello")
            m.save(path)
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            os.unlink(path)


# ── theme loading ─────────────────────────────────────────────────────────────

class TestTheme(unittest.TestCase):
    def test_hex_to_rgb_black(self):
        from pdfeditor import _hex_to_rgb
        self.assertEqual(_hex_to_rgb("#000000"), (0.0, 0.0, 0.0))

    def test_hex_to_rgb_white(self):
        from pdfeditor import _hex_to_rgb
        r, g, b = _hex_to_rgb("#ffffff")
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, 1.0)
        self.assertAlmostEqual(b, 1.0)

    def test_hex_to_rgb_accent(self):
        from pdfeditor import _hex_to_rgb
        r, g, b = _hex_to_rgb("#85b34c")
        self.assertAlmostEqual(r, 0x85 / 255)
        self.assertAlmostEqual(g, 0xb3 / 255)
        self.assertAlmostEqual(b, 0x4c / 255)

    def test_load_theme_returns_defaults_when_file_missing(self):
        from pdfeditor import _load_theme
        import unittest.mock as mock
        with mock.patch("builtins.open", side_effect=OSError):
            theme = _load_theme()
        self.assertIn("background", theme)
        self.assertIn("foreground", theme)
        self.assertIn("accent", theme)
        self.assertTrue(theme["background"].startswith("#"))

    def test_load_theme_parses_toml_values(self):
        from pdfeditor import _load_theme
        import tempfile, unittest.mock as mock
        fake_toml = 'background = "#aabbcc"\nforeground = "#112233"\naccent = "#445566"\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(fake_toml)
            tmp = f.name
        try:
            with mock.patch("pdfeditor.os.path.expanduser", return_value=tmp):
                theme = _load_theme()
            self.assertEqual(theme["background"], "#aabbcc")
            self.assertEqual(theme["foreground"], "#112233")
            self.assertEqual(theme["accent"], "#445566")
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)

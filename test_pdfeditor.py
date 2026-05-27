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
from pdfeditor import PDFCanvas


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

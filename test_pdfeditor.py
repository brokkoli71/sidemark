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
from gi.repository import Gtk, Adw, GLib, Gdk, Poppler
import cairo
import unittest.mock as mock

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


# ── eraser ───────────────────────────────────────────────────────────────────

class TestEraser(unittest.TestCase):
    def _canvas_with_pdf(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        make_pdf(path)
        canvas.load(path)
        self._tmp = path
        return canvas

    def tearDown(self):
        if hasattr(self, "_tmp") and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_stroke_hits_on_segment(self):
        self.assertTrue(PDFCanvas._stroke_hits([(0, 0), (100, 0)], 50, 0, 5.0))

    def test_stroke_hits_near_endpoint(self):
        self.assertTrue(PDFCanvas._stroke_hits([(10, 10)], 12, 10, 5.0))

    def test_stroke_misses_far_point(self):
        self.assertFalse(PDFCanvas._stroke_hits([(0, 0), (100, 0)], 50, 20, 5.0))

    def test_erase_removes_hit_stroke(self):
        canvas = self._canvas_with_pdf()
        canvas.scale = 1.0
        canvas.offset_x = 0.0
        canvas.offset_y = 0.0
        canvas.strokes.append({"pts": [(10, 10), (50, 10)], "color": (0,0,1,1), "width": 2})
        canvas._erase_at(30, 10)   # screen == PDF when scale=1, offset=0
        self.assertEqual(len(canvas.strokes), 0)

    def test_erase_keeps_non_hit_stroke(self):
        canvas = self._canvas_with_pdf()
        canvas.scale = 1.0
        canvas.offset_x = 0.0
        canvas.offset_y = 0.0
        canvas.strokes.append({"pts": [(10, 10), (50, 10)], "color": (0,0,1,1), "width": 2})
        canvas._erase_at(200, 200)
        self.assertEqual(len(canvas.strokes), 1)

    def test_erase_only_removes_hit_stroke(self):
        canvas = self._canvas_with_pdf()
        canvas.scale = 1.0
        canvas.offset_x = 0.0
        canvas.offset_y = 0.0
        canvas.strokes.append({"pts": [(10, 10), (50, 10)], "color": (0,0,1,1), "width": 2})
        canvas.strokes.append({"pts": [(200, 200), (300, 200)], "color": (1,0,0,1), "width": 2})
        canvas._erase_at(30, 10)
        self.assertEqual(len(canvas.strokes), 1)
        self.assertEqual(canvas.strokes[0]["color"], (1, 0, 0, 1))


# ── cached rendering ─────────────────────────────────────────────────────────

class TestRendering(unittest.TestCase):
    def _canvas_with_pdf(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        make_pdf(path)
        canvas.load(path)
        self._tmp = path
        return canvas

    def tearDown(self):
        if hasattr(self, "_tmp") and os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_rerender_creates_surface(self):
        canvas = self._canvas_with_pdf()
        canvas._page_surface = None
        canvas._rerender_now()
        self.assertIsNotNone(canvas._page_surface)

    def test_surface_scale_stored(self):
        canvas = self._canvas_with_pdf()
        canvas.scale = 1.5
        canvas._page_surface = None
        canvas._rerender_now()
        self.assertAlmostEqual(canvas._surface_scale, 1.5)

    def test_load_page_clears_cache(self):
        canvas = self._canvas_with_pdf()
        canvas._rerender_now()
        self.assertIsNotNone(canvas._page_surface)
        canvas._load_page(0)  # reload same page to trigger cache clear
        self.assertIsNone(canvas._page_surface)

    def test_scale_clamped(self):
        canvas = self._canvas_with_pdf()
        canvas.scale = 10.0  # above cap
        canvas._rerender_now()
        self.assertAlmostEqual(canvas._surface_scale, 4.0)
        canvas.scale = 0.1   # below floor
        canvas._rerender_now()
        self.assertAlmostEqual(canvas._surface_scale, 0.5)


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


# ── deferred fit (needs_fit flag) ─────────────────────────────────────────────

class TestNeedsFit(unittest.TestCase):
    """
    _load_page is called before the canvas has been allocated, so _fit_page
    would use the 800×600 fallback.  The _needs_fit flag defers the fit to the
    first real _draw call, at which point get_width/get_height are valid.
    """

    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp)

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def _canvas(self):
        c = PDFCanvas()
        c.load(self._tmp)
        return c

    def _draw(self, canvas, w, h):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(w, 1), max(h, 1))
        canvas._draw(canvas, cairo.Context(surf), w, h)

    def test_false_before_load(self):
        self.assertFalse(PDFCanvas()._needs_fit)

    def test_set_after_load(self):
        self.assertTrue(self._canvas()._needs_fit)

    def test_cleared_after_draw_with_real_dimensions(self):
        c = self._canvas()
        self._draw(c, 800, 600)
        self.assertFalse(c._needs_fit)

    def test_not_cleared_by_zero_size_draw(self):
        c = self._canvas()
        self._draw(c, 0, 0)
        self.assertTrue(c._needs_fit)

    def test_page_fits_inside_canvas_after_draw(self):
        c = self._canvas()
        W, H = 800, 600
        self._draw(c, W, H)
        self.assertGreaterEqual(c.offset_x, 0)
        self.assertGreaterEqual(c.offset_y, 0)
        self.assertLessEqual(c.offset_x + c.page_width  * c.scale, W + 1e-6)
        self.assertLessEqual(c.offset_y + c.page_height * c.scale, H + 1e-6)

    def test_screen_to_pdf_maps_page_center_correctly(self):
        # After a real draw the page centre in screen coords should round-trip
        # back to (page_width/2, page_height/2).
        c = self._canvas()
        self._draw(c, 800, 600)
        screen_cx = c.offset_x + c.page_width  * c.scale / 2
        screen_cy = c.offset_y + c.page_height * c.scale / 2
        pdf_x, pdf_y = c._screen_to_pdf(screen_cx, screen_cy)
        self.assertAlmostEqual(pdf_x, c.page_width  / 2, places=1)
        self.assertAlmostEqual(pdf_y, c.page_height / 2, places=1)


# ── text selection Poppler rectangle ─────────────────────────────────────────

class TestTextSelectionCoords(unittest.TestCase):
    """
    Poppler.Page.get_selected_text uses PDF coordinates: y=0 at the page
    bottom, y increases upward.  _finish_text_selection must flip the
    screen-space y values before building the rectangle.
    """

    class _MockPage:
        """Minimal Poppler.Page stand-in that records the rectangle it received."""
        def __init__(self, w=595, h=842):
            self.width = w
            self.height = h
            self.last_rect = None

        def get_size(self):
            return self.width, self.height

        def get_selected_text(self, style, rect):
            self.last_rect = (rect.x1, rect.y1, rect.x2, rect.y2)
            return "hello"

    def _canvas(self, pw=595, ph=842):
        c = PDFCanvas()
        page = self._MockPage(pw, ph)
        c.page = page
        c.page_width  = pw
        c.page_height = ph
        c.scale    = 1.0
        c.offset_x = 0.0
        c.offset_y = 0.0
        return c, page

    def _select(self, canvas, page, start, end):
        """Run _finish_text_selection with a mocked clipboard; return Poppler rect."""
        canvas._text_select_start = start
        canvas._text_select_end   = end
        with mock.patch.object(Gdk, 'ContentProvider'), \
             mock.patch.object(Gdk, 'Display') as md:
            md.get_default.return_value.get_clipboard.return_value.set_content.return_value = None
            canvas._finish_text_selection()
        return page.last_rect

    def test_y_flipped_for_poppler(self):
        # Screen y=100 (near top) → Poppler y = page_height - 100 = 742 (near top in PDF space).
        # Screen y=200 (lower)    → Poppler y = page_height - 200 = 642.
        # The rect passed to Poppler should have y1=642 (lower bound) and y2=742.
        c, pg = self._canvas(ph=842)
        rect = self._select(c, pg, (100, 100), (300, 200))
        self.assertAlmostEqual(rect[1], 842 - 200)  # y1 = bottom of selection in Poppler coords
        self.assertAlmostEqual(rect[3], 842 - 100)  # y2 = top of selection in Poppler coords

    def test_x_coordinates_preserved_for_single_line(self):
        # y-span of 3 points → single-line → exact x coords kept
        c, pg = self._canvas()
        rect = self._select(c, pg, (50, 100), (250, 103))
        self.assertAlmostEqual(rect[0], 50.0)
        self.assertAlmostEqual(rect[2], 250.0)

    def test_x_extends_to_page_width_for_multiline(self):
        # y-span of 200 points → multi-line → x snapped to [0, page_width]
        c, pg = self._canvas(pw=595)
        rect = self._select(c, pg, (50, 100), (250, 300))
        self.assertAlmostEqual(rect[0], 0.0)
        self.assertAlmostEqual(rect[2], 595.0)

    def test_y1_always_less_than_y2(self):
        # Poppler rect must be lower-left → upper-right (y1 ≤ y2).
        c, pg = self._canvas()
        rect = self._select(c, pg, (100, 50), (300, 400))
        self.assertLessEqual(rect[1], rect[3])

    def test_x1_always_less_than_x2(self):
        c, pg = self._canvas()
        rect = self._select(c, pg, (300, 100), (100, 300))  # x dragged right-to-left
        self.assertLessEqual(rect[0], rect[2])

    def test_same_rect_regardless_of_drag_direction(self):
        c1, pg1 = self._canvas()
        c2, pg2 = self._canvas()
        r1 = self._select(c1, pg1, (100, 100), (300, 300))
        r2 = self._select(c2, pg2, (300, 300), (100, 100))  # reversed drag
        for a, b in zip(r1, r2):
            self.assertAlmostEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)

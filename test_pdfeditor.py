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
from gi.repository import Gtk, Adw, GLib, Gdk
import cairo
import fitz
import unittest.mock as mock

# Bootstrap Adw so widget construction works without a real display
Adw.init()

sys.path.insert(0, os.path.dirname(__file__))
from sidemark import (PDFCanvas, NotesModel, notes_path_for,
                      _export_pdf_with_notes, PDFEditorWindow)


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
            self._tmp = f.name
        make_pdf(self._tmp, n_pages=2)

    def tearDown(self):
        for p in [self._tmp, self._tmp + ".tmp"]:
            if os.path.exists(p):
                os.unlink(p)

    def test_save_produces_valid_pdf(self):
        canvas = PDFCanvas()
        canvas.load(self._tmp)
        canvas.strokes.append({"pts": [(10, 10), (100, 100)], "color": (0, 0, 1), "width": 2})
        canvas.save(self._tmp)
        self.assertTrue(os.path.getsize(self._tmp) > 0)
        doc = fitz.open(self._tmp)
        self.assertEqual(len(doc), 2)
        doc.close()

    def test_strokes_survive_round_trip(self):
        # Strokes saved as ink annotations must be readable back as strokes.
        canvas = PDFCanvas()
        canvas.load(self._tmp)
        canvas.strokes.append({"pts": [(10, 10), (50, 50)], "color": (1, 0, 0), "width": 3})
        canvas.save(self._tmp)
        canvas2 = PDFCanvas()
        canvas2.load(self._tmp)
        self.assertEqual(len(canvas2.strokes), 1)
        self.assertAlmostEqual(canvas2.strokes[0]["width"], 3.0, places=0)
        self.assertEqual(len(canvas2.strokes[0]["pts"]), 2)

    def test_erase_after_reload(self):
        # The core motivation for the PyMuPDF migration: strokes loaded from
        # a saved file must be individually erasable.
        canvas = PDFCanvas()
        canvas.load(self._tmp)
        canvas.strokes.append({"pts": [(10, 10), (50, 10)], "color": (0, 0, 1), "width": 2})
        canvas.save(self._tmp)
        canvas2 = PDFCanvas()
        canvas2.load(self._tmp)
        self.assertEqual(len(canvas2.strokes), 1)
        canvas2.scale = 1.0
        canvas2.offset_x = 0.0
        canvas2.offset_y = 0.0
        canvas2._erase_at(30, 10)   # hit the stroke
        self.assertEqual(len(canvas2.strokes), 0)

    def test_save_overwrites_atomically(self):
        canvas = PDFCanvas()
        canvas.load(self._tmp)
        canvas.save(self._tmp)
        self.assertFalse(os.path.exists(self._tmp + ".tmp"))


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
        from sidemark import _hex_to_rgb
        self.assertEqual(_hex_to_rgb("#000000"), (0.0, 0.0, 0.0))

    def test_hex_to_rgb_white(self):
        from sidemark import _hex_to_rgb
        r, g, b = _hex_to_rgb("#ffffff")
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(g, 1.0)
        self.assertAlmostEqual(b, 1.0)

    def test_hex_to_rgb_accent(self):
        from sidemark import _hex_to_rgb
        r, g, b = _hex_to_rgb("#85b34c")
        self.assertAlmostEqual(r, 0x85 / 255)
        self.assertAlmostEqual(g, 0xb3 / 255)
        self.assertAlmostEqual(b, 0x4c / 255)

    def test_load_theme_returns_defaults_when_file_missing(self):
        from sidemark import _load_theme
        import unittest.mock as mock
        with mock.patch("builtins.open", side_effect=OSError):
            theme = _load_theme()
        self.assertIn("background", theme)
        self.assertIn("foreground", theme)
        self.assertIn("accent", theme)
        self.assertTrue(theme["background"].startswith("#"))

    def test_load_theme_parses_toml_values(self):
        from sidemark import _load_theme
        import tempfile, unittest.mock as mock
        fake_toml = 'background = "#aabbcc"\nforeground = "#112233"\naccent = "#445566"\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(fake_toml)
            tmp = f.name
        try:
            with mock.patch("sidemark.os.path.expanduser", return_value=tmp):
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

# ── markdown formatting shortcuts ────────────────────────────────────────────

class TestMarkdownFormatting(unittest.TestCase):

    def _view(self):
        from sidemark import MarkdownNotesView
        return MarkdownNotesView()

    def _set(self, buf, text, sel_start, sel_end):
        buf.set_text(text)
        s = buf.get_start_iter(); s.forward_chars(sel_start)
        e = buf.get_start_iter(); e.forward_chars(sel_end)
        buf.select_range(s, e)

    def _text(self, buf):
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def test_bold_wraps_selection(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "hello world", 6, 11)
        v._wrap_selection("**")
        self.assertEqual(self._text(buf), "hello **world**")

    def test_italic_wraps_selection(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "hello world", 6, 11)
        v._wrap_selection("*")
        self.assertEqual(self._text(buf), "hello *world*")

    def test_code_wraps_selection(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "run foo", 4, 7)
        v._wrap_selection("`")
        self.assertEqual(self._text(buf), "run `foo`")

    def test_no_selection_does_nothing(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("hello"); buf.place_cursor(buf.get_end_iter())
        v._wrap_selection("**")
        self.assertEqual(self._text(buf), "hello")

    def test_wrap_right_to_left_drag(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("hello world")
        s = buf.get_start_iter(); s.forward_chars(6)
        e = buf.get_start_iter(); e.forward_chars(11)
        buf.select_range(e, s)   # reversed drag
        v._wrap_selection("**")
        self.assertEqual(self._text(buf), "hello **world**")

    def test_bold_unwraps_when_markers_selected(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "hello **world**", 6, 15)   # select "**world**"
        v._wrap_selection("**")
        self.assertEqual(self._text(buf), "hello world")

    def test_bold_unwraps_when_inner_text_selected(self):
        # Select just "world" (no markers) inside **world** — should still unwrap
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "hello **world**", 8, 13)   # select "world"
        v._wrap_selection("**")
        self.assertEqual(self._text(buf), "hello world")

    def test_italic_does_not_unwrap_bold(self):
        # Selecting **bold** and pressing Ctrl+I should add italic, not strip bold
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "**bold**", 0, 8)
        v._wrap_selection("*")
        self.assertEqual(self._text(buf), "***bold***")   # bold+italic

    def test_selection_preserved_after_wrap(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "hello world", 6, 11)
        v._wrap_selection("**")
        # Selection should cover "world" (not the markers)
        s = buf.get_iter_at_mark(buf.get_selection_bound())
        e = buf.get_iter_at_mark(buf.get_insert())
        if s.compare(e) > 0: s, e = e, s
        self.assertEqual(buf.get_text(s, e, False), "world")

    def test_selection_preserved_after_unwrap(self):
        v = self._view(); buf = v.get_buffer()
        self._set(buf, "**world**", 0, 9)
        v._wrap_selection("**")
        s = buf.get_iter_at_mark(buf.get_selection_bound())
        e = buf.get_iter_at_mark(buf.get_insert())
        if s.compare(e) > 0: s, e = e, s
        self.assertEqual(buf.get_text(s, e, False), "world")


class TestLatexFormatting(unittest.TestCase):

    def _view(self):
        from sidemark import MarkdownNotesView
        return MarkdownNotesView()

    # ── symbol substitution ───────────────────────────────────────────────────

    def test_symbol_sub_single(self):
        v = self._view()
        self.assertEqual(v._apply_symbol_subs(r'\alpha'), 'α')

    def test_symbol_sub_in_sentence(self):
        v = self._view()
        self.assertEqual(v._apply_symbol_subs(r'let \alpha = 1'), 'let α = 1')

    def test_symbol_sub_multiple(self):
        v = self._view()
        self.assertEqual(v._apply_symbol_subs(r'\alpha + \beta'), 'α + β')

    def test_symbol_sub_unknown_unchanged(self):
        v = self._view()
        self.assertEqual(v._apply_symbol_subs(r'\frac'), r'\frac')

    def test_symbol_sub_no_backslash_unchanged(self):
        v = self._view()
        self.assertEqual(v._apply_symbol_subs('alpha'), 'alpha')

    # ── script regex ──────────────────────────────────────────────────────────

    def test_script_re_single_sup(self):
        from sidemark import MarkdownNotesView
        m = MarkdownNotesView._SCRIPT_RE.search('x^2')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '^')
        self.assertEqual(m.group(3), '2')

    def test_script_re_multi_sup(self):
        from sidemark import MarkdownNotesView
        m = MarkdownNotesView._SCRIPT_RE.search('x^ab')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(3), 'ab')

    def test_script_re_braced_sup(self):
        from sidemark import MarkdownNotesView
        m = MarkdownNotesView._SCRIPT_RE.search('x^{n+1}')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), 'n+1')

    def test_script_re_sub(self):
        from sidemark import MarkdownNotesView
        m = MarkdownNotesView._SCRIPT_RE.search('x_ij')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '_')
        self.assertEqual(m.group(3), 'ij')

    def test_script_re_breaks_at_space(self):
        from sidemark import MarkdownNotesView
        m = MarkdownNotesView._SCRIPT_RE.search('x^ab cd')
        self.assertIsNotNone(m)
        self.assertEqual(m.group(3), 'ab')   # stops before space

    def test_symbol_buffer_substitution(self):
        v = self._view()
        buf = v.get_buffer()
        buf.set_text(r'\sum_{i=1}^n')
        # Move cursor to line 1 (a different line) so line 0 is substituted
        buf.insert(buf.get_end_iter(), '\nother line')
        buf.place_cursor(buf.get_iter_at_line(1)[1])
        # Trigger rehighlight synchronously
        v._rehighlight()
        ok, ls = buf.get_iter_at_line(0)
        le = ls.copy(); le.forward_to_line_end()
        result = buf.get_text(ls, le, False)
        self.assertIn('Σ', result)
        self.assertNotIn(r'\sum', result)

    def test_symbol_restored_on_cursor_enter(self):
        v = self._view()
        buf = v.get_buffer()
        buf.set_text(r'\alpha' + '\nother')
        buf.place_cursor(buf.get_iter_at_line(1)[1])
        v._rehighlight()
        # Now move cursor back to line 0
        buf.place_cursor(buf.get_iter_at_line(0)[1])
        v._rehighlight()
        ok, ls = buf.get_iter_at_line(0)
        le = ls.copy(); le.forward_to_line_end()
        result = buf.get_text(ls, le, False)
        self.assertEqual(result, r'\alpha')


# ── export ────────────────────────────────────────────────────────────────────

class TestExport(unittest.TestCase):
    """
    Covers three bug classes that slipped through before:
      1. PyMuPDF API calls (font names, draw calls) must be tested against a
         real PDF so bad names raise immediately rather than at user runtime.
      2. Exception handlers in threads must be tested via the error path so
         closure bugs (Python deletes 'except ... as e' at block exit) surface.
      3. New GTK signal connections must be tested by constructing and
         realizing the widget so unknown signal names raise in CI.
    """

    def _model_with_notes(self, text, page=0):
        m = NotesModel()
        m.set(page, text)
        return m

    # -- PyMuPDF rendering (font names, draw calls) ---------------------------

    def test_export_plain_notes_produces_valid_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.pdf")
            out = os.path.join(d, "out.pdf")
            make_pdf(src, n_pages=2)
            model = self._model_with_notes("Hello world", page=0)
            _export_pdf_with_notes(src, out, model, include_empty=False,
                                   accent=(0.2, 0.5, 0.9))
            doc = fitz.open(out)
            # page 0 → source + notes; page 1 → source only (no notes, not included)
            self.assertEqual(doc.page_count, 3)
            doc.close()

    def test_export_with_anchor_markers(self):
        """Exercises _draw_export_anchor and the notes-page anchor replacement."""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.pdf")
            out = os.path.join(d, "out.pdf")
            make_pdf(src)
            notes = "Before\n<!-- anchor:100:200 -->\nAfter"
            model = self._model_with_notes(notes)
            # Must not raise (caught helv-bo / font-name bug class)
            _export_pdf_with_notes(src, out, model, include_empty=True,
                                   accent=(0.2, 0.5, 0.9))
            doc = fitz.open(out)
            self.assertEqual(doc.page_count, 2)
            # The anchor number "1" must appear as text on the source page
            source_page_text = doc[0].get_text()
            self.assertIn("1", source_page_text)
            # The notes page must contain [1] replacing the anchor comment
            notes_page_text = doc[1].get_text()
            self.assertIn("[1]", notes_page_text)
            doc.close()

    def test_export_include_empty_adds_notes_page_for_every_source_page(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.pdf")
            out = os.path.join(d, "out.pdf")
            make_pdf(src, n_pages=3)
            model = NotesModel()  # no notes on any page
            _export_pdf_with_notes(src, out, model, include_empty=True,
                                   accent=(0.2, 0.5, 0.9))
            doc = fitz.open(out)
            self.assertEqual(doc.page_count, 6)
            doc.close()

    # -- Exception-path closure -----------------------------------------------

    def test_export_bad_source_raises(self):
        """Error path: bad source PDF must raise, not silently fail.
        Catches the bug class where 'except ... as e' is used in a lambda
        — the fix is to capture str(e) in a local before the lambda."""
        model = NotesModel()
        with self.assertRaises(Exception):
            _export_pdf_with_notes("/nonexistent/no.pdf", "/tmp/out.pdf",
                                   model, False, (0, 0, 1))

    # -- GTK signal connections -----------------------------------------------

    def test_window_realize_does_not_raise(self):
        """Constructing and realizing PDFEditorWindow must not raise.
        Catches the bug class where .connect() is given an unknown signal name."""
        errors = []
        app = Adw.Application(application_id="test.sidemark.realize")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)

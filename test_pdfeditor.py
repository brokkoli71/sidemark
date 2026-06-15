#!/usr/bin/env /usr/bin/python3
"""
Headless tests for PDFCanvas logic.
Run with:  /usr/bin/python3 test_pdfeditor.py
"""
import os
import sys
import math
import tempfile
import time
import unittest

# Keep tests from writing the user's real recently-used.xbel — they run on the
# live session backend (GTK4 dropped the offscreen backend).
os.environ["SIDEMARK_TEST"] = "1"

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio
import cairo
import fitz
import unittest.mock as mock

# Bootstrap Adw so widget construction works without a real display
Adw.init()

sys.path.insert(0, os.path.dirname(__file__))
import sidemark
from sidemark import (PDFCanvas, NotesModel, notes_path_for,
                      _export_pdf_with_notes, _parse_anchors, PDFEditorWindow)

# window tests open files, which records recents — keep that out of the user's
# real ~/.local/share/sidemark/recent.json (TestRecentFiles patches its own)
sidemark.RECENT_PATH = os.path.join(
    tempfile.mkdtemp(prefix="sidemark-test-recents-"), "recent.json")


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


# ── pinch zoom ────────────────────────────────────────────────────────────────

class TestPinchZoom(unittest.TestCase):
    def _canvas(self):
        c = PDFCanvas()
        c.scale = 1.0
        c.offset_x = 0.0
        c.offset_y = 0.0
        c._pinch_start_scale = None
        return c

    def test_zoom_at_keeps_anchor_fixed(self):
        c = self._canvas()
        c.scale = 2.0
        c.offset_x, c.offset_y = 40.0, 25.0
        cx, cy = 150.0, 110.0
        pdf_before = ((cx - c.offset_x) / c.scale, (cy - c.offset_y) / c.scale)
        c._zoom_at(1.5, cx, cy)
        pdf_after = ((cx - c.offset_x) / c.scale, (cy - c.offset_y) / c.scale)
        self.assertAlmostEqual(c.scale, 3.0)
        self.assertAlmostEqual(pdf_before[0], pdf_after[0], places=10)
        self.assertAlmostEqual(pdf_before[1], pdf_after[1], places=10)

    def test_zoom_at_clamps(self):
        c = self._canvas()
        c.scale = 19.0
        c._zoom_at(5.0, 100, 100)
        self.assertLessEqual(c.scale, 20.0)
        c.scale = 0.2
        c._zoom_at(0.01, 100, 100)
        self.assertGreaterEqual(c.scale, 0.1)

    def test_pinch_scales_relative_to_begin(self):
        c = self._canvas()
        c.page = object()  # non-None so handlers run
        c.scale = 2.0
        gesture = mock.Mock()
        gesture.get_bounding_box_center.return_value = (True, 100.0, 100.0)
        c._on_pinch_begin(gesture, None)
        self.assertEqual(c._pinch_start_scale, 2.0)
        # a cumulative delta of 1.5 → target scale 3.0
        c._on_pinch_scale(gesture, 1.5)
        self.assertAlmostEqual(c.scale, 3.0)
        # a later delta of 0.5 → target scale 1.0 (relative to begin, not current)
        c._on_pinch_scale(gesture, 0.5)
        self.assertAlmostEqual(c.scale, 1.0)

    def test_pinch_anchor_point_follows_centroid(self):
        # both fingers stay anchored: the document point under the centroid at
        # begin must remain under the centroid even as the centroid moves
        c = self._canvas()
        c.page = object()
        c.scale = 2.0
        c.offset_x, c.offset_y = 10.0, 5.0
        begin = mock.Mock()
        begin.get_bounding_box_center.return_value = (True, 100.0, 80.0)
        c._on_pinch_begin(begin, None)
        anchor_pdf = ((100 - 10) / 2.0, (80 - 5) / 2.0)
        # centroid moves to (160,140) while pinching out 1.5×
        move = mock.Mock()
        move.get_bounding_box_center.return_value = (True, 160.0, 140.0)
        c._on_pinch_scale(move, 1.5)
        self.assertAlmostEqual(c.scale, 3.0)
        # the anchored document point now sits under the new centroid
        self.assertAlmostEqual((160 - c.offset_x) / c.scale, anchor_pdf[0])
        self.assertAlmostEqual((140 - c.offset_y) / c.scale, anchor_pdf[1])

    def test_pinch_begin_discards_in_progress_stroke(self):
        c = self._canvas()
        c.page = object()
        c.current_stroke = [(10, 10), (11, 12)]  # a dot/stroke from finger 1
        gesture = mock.Mock()
        gesture.get_bounding_box_center.return_value = (True, 50.0, 50.0)
        c._on_pinch_begin(gesture, None)
        self.assertEqual(c.current_stroke, [])
        self.assertTrue(c._ignoring)
        c._on_pinch_end(gesture, None)
        self.assertFalse(c._ignoring)
        self.assertIsNone(c._pinch_start_scale)

    def test_leftover_finger_pans_after_pinch_no_dot(self):
        # release one finger before the other: the remaining finger's live drag
        # must pan the page, not draw a stroke
        c = self._canvas()
        c.page = object()
        c.scale = 2.0
        c.offset_x, c.offset_y = 0.0, 0.0
        zoom = mock.Mock()
        zoom.get_bounding_box_center.return_value = (True, 100.0, 100.0)
        c._on_pinch_begin(zoom, None)
        c._on_pinch_end(zoom, None)         # one finger lifted
        self.assertTrue(c._post_pinch)
        drag = mock.Mock()
        drag.get_start_point.return_value = (True, 100.0, 100.0)
        c._on_drag_update(drag, 30.0, 20.0)  # leftover finger moves
        # first post-pinch update latches the anchor → no movement yet
        self.assertEqual((c.offset_x, c.offset_y), (0.0, 0.0))
        c._on_drag_update(drag, 50.0, 35.0)  # 20px right, 15px down from anchor
        self.assertAlmostEqual(c.offset_x, 20.0)
        self.assertAlmostEqual(c.offset_y, 15.0)
        self.assertEqual(c.current_stroke, [])  # nothing drawn
        c._on_drag_end(drag, 50.0, 35.0)
        self.assertFalse(c._post_pinch)
        self.assertEqual(c.current_stroke, [])

    def test_drag_begin_clears_post_pinch(self):
        c = self._canvas()
        c.page = object()
        c._post_pinch = True
        gesture = mock.Mock()
        gesture.get_current_button.return_value = 1
        gesture.get_current_event_state.return_value = Gdk.ModifierType(0)
        c.select_mode = False
        c._anchor_hit_test = lambda *a: None
        c._on_drag_begin(gesture, 10.0, 10.0)
        self.assertFalse(c._post_pinch)

    def test_pinch_without_page_is_noop(self):
        c = self._canvas()
        c.page = None
        gesture = mock.Mock()
        c._on_pinch_begin(gesture, None)
        self.assertIsNone(c._pinch_start_scale)
        c._on_pinch_scale(gesture, 2.0)  # must not raise
        self.assertEqual(c.scale, 1.0)


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
        canvas.pen_color = (0, 0, 1, 1)
        canvas.current_stroke = [(0, 0)]
        canvas._on_drag_end(None, 0, 0)
        canvas.pen_color = (1, 0, 0, 1)
        canvas.current_stroke = [(1, 1)]
        canvas._on_drag_end(None, 0, 0)
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


# ── straight-line snap (GoodNotes-style hold) ──────────────────────────────────

class TestStraightLineSnap(unittest.TestCase):
    def _drag(self, sx=0.0, sy=0.0):
        g = mock.Mock()
        g.get_start_point.return_value = (True, sx, sy)
        return g

    def _canvas(self):
        c = PDFCanvas()
        c.scale, c.offset_x, c.offset_y = 1.0, 0.0, 0.0
        return c

    def test_snap_collapses_squiggle_to_line(self):
        c = self._canvas()
        c.current_stroke = [(0, 0), (5, 3), (8, 1), (12, 9)]
        c._snap_to_straight()
        self.assertTrue(c._straight_mode)
        self.assertEqual(c.current_stroke, [(0, 0), (12, 9)])

    def test_snap_noop_for_single_point(self):
        c = self._canvas()
        c.current_stroke = [(2, 2)]
        c._snap_to_straight()
        self.assertFalse(c._straight_mode)
        self.assertEqual(c.current_stroke, [(2, 2)])

    def test_endpoint_follows_cursor_in_straight_mode(self):
        c = self._canvas()
        c.current_stroke = [(0, 0), (10, 10)]
        c._straight_mode = True
        c._on_drag_update(self._drag(0, 0), 30, 5)
        self.assertEqual(len(c.current_stroke), 2)   # stays a line
        self.assertEqual(tuple(c.current_stroke[0]), (0, 0))
        self.assertEqual(tuple(c.current_stroke[1]), (30, 5))

    def test_free_motion_appends_and_arms_timer(self):
        c = self._canvas()
        c.current_stroke = [(0, 0)]
        c._on_drag_update(self._drag(0, 0), 5, 5)
        self.assertEqual(len(c.current_stroke), 2)
        self.assertIsNotNone(c._straight_timer)
        c._cancel_straight_timer()
        self.assertIsNone(c._straight_timer)

    def test_drag_end_resets_straight_state(self):
        c = self._canvas()
        c._straight_mode = True
        c._arm_straight_timer()
        c.current_stroke = [(0, 0), (5, 5)]
        c._on_drag_end(None, 5, 5)
        self.assertFalse(c._straight_mode)
        self.assertIsNone(c._straight_timer)


# ── stroke smoothing ───────────────────────────────────────────────────────────

class TestStrokeSmoothing(unittest.TestCase):
    def test_zero_strength_is_identity(self):
        pts = [(0, 0), (1, 5), (2, 0), (3, 5)]
        self.assertEqual(PDFCanvas._smooth_points(pts, 0.0), pts)

    def test_too_few_points_unchanged(self):
        self.assertEqual(PDFCanvas._smooth_points([(0, 0), (4, 4)], 1.0),
                         [(0, 0), (4, 4)])

    def test_endpoints_preserved(self):
        pts = [(0, 0), (1, 9), (2, 0), (3, 9), (4, 0)]
        out = PDFCanvas._smooth_points(pts, 1.0)
        self.assertEqual(out[0], (0.0, 0.0))
        self.assertEqual(out[-1], (4.0, 0.0))
        self.assertEqual(len(out), len(pts))

    def test_smoothing_reduces_jitter(self):
        # a zigzag: interior points should move toward their neighbours' mean,
        # so total deviation from the straight baseline shrinks
        pts = [(0, 0), (1, 10), (2, -10), (3, 10), (4, 0)]
        out = PDFCanvas._smooth_points(pts, 1.0)
        raw_dev = sum(abs(y) for _, y in pts)
        new_dev = sum(abs(y) for _, y in out)
        self.assertLess(new_dev, raw_dev)

    def test_commit_smooths_freehand_stroke(self):
        c = PDFCanvas()
        c.scale, c.offset_x, c.offset_y = 1.0, 0.0, 0.0
        c.smoothing = 1.0
        c.current_stroke = [(0, 0), (1, 10), (2, -10), (3, 10), (4, 0)]
        raw = list(c.current_stroke)
        c._on_drag_end(None, 0, 0)
        committed = c.strokes[-1]["pts"]
        self.assertNotEqual(committed, raw)          # was smoothed
        self.assertEqual(committed[0], (0.0, 0.0))   # endpoints kept

    def test_commit_does_not_smooth_when_disabled(self):
        c = PDFCanvas()
        c.scale, c.offset_x, c.offset_y = 1.0, 0.0, 0.0
        c.smoothing = 0.0
        c.current_stroke = [(0, 0), (1, 10), (2, -10), (3, 10), (4, 0)]
        c._on_drag_end(None, 0, 0)
        self.assertEqual(c.strokes[-1]["pts"], [(0, 0), (1, 10), (2, -10), (3, 10), (4, 0)])

    def test_snapped_line_is_not_smoothed(self):
        c = PDFCanvas()
        c.scale, c.offset_x, c.offset_y = 1.0, 0.0, 0.0
        c.smoothing = 1.0
        c._straight_mode = True
        c.current_stroke = [(0, 0), (10, 4)]
        c._on_drag_end(None, 0, 0)
        self.assertEqual(c.strokes[-1]["pts"], [(0, 0), (10, 4)])


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


# ── view adjustment on canvas resize (sidebar toggle, window resize) ─────────

class TestViewResize(unittest.TestCase):
    def _canvas_with_pdf(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp)   # 595 x 842
        canvas.load(self._tmp)
        return canvas

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def test_fitted_view_refits_on_resize(self):
        canvas = self._canvas_with_pdf()
        canvas._fit_page(800, 600)
        canvas._last_size = (800, 600)
        canvas._on_resize(None, 500, 600)
        self.assertAlmostEqual(canvas.scale, min(500 / 595, 600 / 842) * 0.95)
        self.assertAlmostEqual(canvas.offset_x, (500 - 595 * canvas.scale) / 2)
        self.assertAlmostEqual(canvas.offset_y, (600 - 842 * canvas.scale) / 2)

    def test_zoomed_view_keeps_center_anchored(self):
        canvas = self._canvas_with_pdf()
        canvas._is_fitted = False
        canvas.scale = 2.0
        canvas.offset_x = -200.0   # pdf point at old center (800/2, 600/2):
        canvas.offset_y = -100.0   # ((400+200)/2, (300+100)/2) = (300, 200)
        canvas._last_size = (800, 600)
        canvas._on_resize(None, 600, 600)
        cx_pdf = (600 / 2 - canvas.offset_x) / canvas.scale
        cy_pdf = (600 / 2 - canvas.offset_y) / canvas.scale
        self.assertAlmostEqual(cx_pdf, 300.0)
        self.assertAlmostEqual(cy_pdf, 200.0)
        self.assertAlmostEqual(canvas.scale, 2.0)   # zoom level untouched

    def test_first_resize_only_records_size(self):
        canvas = self._canvas_with_pdf()
        scale, ox, oy = canvas.scale, canvas.offset_x, canvas.offset_y
        canvas._on_resize(None, 800, 600)   # old size unknown (0, 0)
        self.assertEqual(canvas._last_size, (800, 600))
        self.assertEqual((canvas.scale, canvas.offset_x, canvas.offset_y),
                         (scale, ox, oy))

    def test_fit_page_sets_fitted_flag(self):
        canvas = self._canvas_with_pdf()
        canvas._is_fitted = False
        canvas._fit_page(800, 600)
        self.assertTrue(canvas._is_fitted)

    def test_manual_zoom_clears_fitted_flag(self):
        canvas = self._canvas_with_pdf()
        canvas._fit_page(800, 600)
        ctrl = mock.Mock()
        ctrl.get_current_event_state.return_value = Gdk.ModifierType.CONTROL_MASK
        canvas._on_scroll(ctrl, 0, 1)   # Ctrl+scroll zoom
        self.assertFalse(canvas._is_fitted)

    def test_scroll_pan_clears_fitted_flag(self):
        canvas = self._canvas_with_pdf()
        canvas._fit_page(800, 600)
        ctrl = mock.Mock()
        ctrl.get_current_event_state.return_value = Gdk.ModifierType(0)
        canvas._on_scroll(ctrl, 0, 1)
        self.assertFalse(canvas._is_fitted)

    def test_zoom_to_rect_clears_fitted_flag(self):
        canvas = self._canvas_with_pdf()
        canvas._fit_page(800, 600)
        canvas._execute_zoom_to_rect((10, 10), (200, 200))
        self.assertFalse(canvas._is_fitted)

    def test_zoom_to_fit_restores_fitted_flag(self):
        canvas = self._canvas_with_pdf()
        canvas._execute_zoom_to_rect((10, 10), (200, 200))
        canvas.zoom_to_fit()
        self.assertTrue(canvas._is_fitted)


# ── scroll-past-boundary page flip ────────────────────────────────────────────

class TestScrollFlip(unittest.TestCase):
    def _canvas_with_pdf(self, n_pages=3):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp, n_pages=n_pages)
        canvas.load(self._tmp)
        canvas._fit_page(800, 600)   # whole page visible — both edges at boundary
        return canvas

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    @staticmethod
    def _scroll(canvas, dy, times=1):
        ctrl = mock.Mock()
        ctrl.get_current_event_state.return_value = Gdk.ModifierType(0)
        for _ in range(times):
            canvas._on_scroll(ctrl, 0, dy)

    def test_scrolling_past_bottom_flips_to_next_page(self):
        canvas = self._canvas_with_pdf()
        self._scroll(canvas, 1, times=3)
        self.assertEqual(canvas.current_page_idx, 1)

    def test_below_threshold_does_not_flip(self):
        canvas = self._canvas_with_pdf()
        self._scroll(canvas, 1, times=2)
        self.assertEqual(canvas.current_page_idx, 0)

    def test_scrolling_past_top_flips_to_previous_page(self):
        canvas = self._canvas_with_pdf()
        canvas.go_to_page(1)
        canvas._fit_page(800, 600)
        self._scroll(canvas, -1, times=3)
        self.assertEqual(canvas.current_page_idx, 0)

    def test_direction_reversal_resets_resistance(self):
        canvas = self._canvas_with_pdf()
        canvas.go_to_page(1)
        canvas._fit_page(800, 600)
        self._scroll(canvas, 1, times=2)    # 2 notches down …
        self._scroll(canvas, -1, times=1)   # … reversal resets the accumulator
        self._scroll(canvas, 1, times=2)    # 2 more down: still below threshold
        self.assertEqual(canvas.current_page_idx, 1)
        self._scroll(canvas, 1, times=1)
        self.assertEqual(canvas.current_page_idx, 2)

    def test_no_flip_past_last_page(self):
        canvas = self._canvas_with_pdf()
        canvas.go_to_page(2)
        canvas._fit_page(800, 600)
        self._scroll(canvas, 1, times=5)
        self.assertEqual(canvas.current_page_idx, 2)

    def test_zoomed_flip_keeps_zoom_and_aligns_top(self):
        canvas = self._canvas_with_pdf()
        canvas._is_fitted = False
        canvas.scale = 2.0
        canvas.offset_x = -100.0
        canvas.offset_y = 600 - 842 * 2.0   # page bottom exactly at viewport bottom
        self._scroll(canvas, 1, times=3)
        self.assertEqual(canvas.current_page_idx, 1)
        self.assertEqual(canvas.scale, 2.0)
        self.assertEqual(canvas.offset_x, -100.0)
        self.assertEqual(canvas.offset_y, 8.0)   # new page top in view

    def test_mid_page_scroll_pans_normally(self):
        canvas = self._canvas_with_pdf()
        canvas._is_fitted = False
        canvas.scale = 2.0
        canvas.offset_y = -200.0   # neither edge visible
        self._scroll(canvas, 1, times=1)
        self.assertEqual(canvas.current_page_idx, 0)
        self.assertEqual(canvas.offset_y, -230.0)   # panned by 30 px

    def test_page_will_change_fires_before_change(self):
        canvas = self._canvas_with_pdf()
        seen = []
        canvas.on_page_will_change = lambda: seen.append(canvas.current_page_idx)
        canvas.go_to_page(1)
        self.assertEqual(seen, [0])   # fired while the old page was current
        canvas.go_to_page(1)          # no-op: same page
        self.assertEqual(seen, [0])


# ── undo for draw and erase ──────────────────────────────────────────────────

class TestUndoEraser(unittest.TestCase):
    """Ctrl+Z must also undo erasing — erased strokes (including ones loaded
    from a saved file) used to be gone for good."""

    def _canvas_with_pdf(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp)
        canvas.load(self._tmp)
        canvas.scale = 1.0
        canvas.offset_x = 0.0
        canvas.offset_y = 0.0
        return canvas

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    def _draw(self, canvas, pts):
        canvas.current_stroke = list(pts)
        canvas._on_drag_end(None, 0, 0)

    def test_undo_restores_erased_stroke(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        canvas._erase_group += 1   # as set by _on_drag_begin for button 3
        canvas._erase_at(30, 10)
        self.assertEqual(len(canvas.strokes), 0)
        canvas.undo_last()
        self.assertEqual(len(canvas.strokes), 1)
        self.assertEqual(canvas.strokes[0]["pts"], [(10, 10), (50, 10)])

    def test_erase_drag_undoes_as_one_group(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        self._draw(canvas, [(10, 40), (50, 40)])
        canvas._erase_group += 1
        canvas._erase_at(30, 10)   # one drag gesture hits both strokes …
        canvas._erase_at(30, 40)   # … across two motion events
        self.assertEqual(len(canvas.strokes), 0)
        canvas.undo_last()         # a single undo restores the whole drag
        self.assertEqual(len(canvas.strokes), 2)

    def test_separate_erase_drags_undo_separately(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        self._draw(canvas, [(10, 40), (50, 40)])
        canvas._erase_group += 1
        canvas._erase_at(30, 10)
        canvas._erase_group += 1
        canvas._erase_at(30, 40)
        canvas.undo_last()
        self.assertEqual(len(canvas.strokes), 1)
        canvas.undo_last()
        self.assertEqual(len(canvas.strokes), 2)

    def test_erased_stroke_restored_at_original_position(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        self._draw(canvas, [(10, 40), (50, 40)])
        self._draw(canvas, [(10, 70), (50, 70)])
        canvas._erase_group += 1
        canvas._erase_at(30, 40)   # erase the middle stroke
        canvas.undo_last()
        self.assertEqual([s["pts"][0] for s in canvas.strokes],
                         [(10, 10), (10, 40), (10, 70)])

    def test_undo_order_interleaves_draw_and_erase(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        canvas._erase_group += 1
        canvas._erase_at(30, 10)
        self._draw(canvas, [(10, 40), (50, 40)])
        canvas.undo_last()   # removes the second draw
        self.assertEqual(len(canvas.strokes), 0)
        canvas.undo_last()   # restores the erased first stroke
        self.assertEqual(len(canvas.strokes), 1)
        self.assertEqual(canvas.strokes[0]["pts"][0], (10, 10))

    def test_load_clears_undo_stack(self):
        canvas = self._canvas_with_pdf()
        self._draw(canvas, [(10, 10), (50, 10)])
        canvas.load(self._tmp)
        self.assertEqual(canvas._undo_stack, [])
        canvas.undo_last()   # must not raise or remove loaded strokes


# ── page insert / delete keep notes, strokes and anchors aligned ─────────────

class TestPageInsertDelete(unittest.TestCase):
    """Inserting/deleting a page must re-key everything that is keyed by page
    index: strokes, anchors (canvas) and notes (model). A desync here attaches
    notes/ink to the wrong pages — silent data corruption."""

    def _canvas_with_pdf(self, n_pages=3):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp, n_pages=n_pages)
        canvas.load(self._tmp)
        return canvas

    def tearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)

    @staticmethod
    def _stroke(tag):
        return {"pts": [(tag, tag), (tag + 1, tag + 1)], "color": (0, 0, 1), "width": 2}

    def test_insert_shifts_strokes_and_anchors(self):
        canvas = self._canvas_with_pdf()
        canvas.all_strokes = {0: [self._stroke(0)], 1: [self._stroke(1)], 2: [self._stroke(2)]}
        canvas._anchors = {0: [(10, 10)], 1: [(11, 11)], 2: [(12, 12)]}
        canvas.go_to_page(0)
        canvas.add_blank_page()   # inserts at index 1
        self.assertEqual(canvas.n_pages, 4)
        self.assertEqual(canvas.all_strokes[0][0]["pts"][0], (0, 0))
        self.assertNotIn(1, canvas.all_strokes)      # new blank page
        self.assertEqual(canvas.all_strokes[2][0]["pts"][0], (1, 1))
        self.assertEqual(canvas.all_strokes[3][0]["pts"][0], (2, 2))
        self.assertEqual(canvas._anchors, {0: [(10, 10)], 2: [(11, 11)], 3: [(12, 12)]})

    def test_delete_shifts_strokes_and_anchors(self):
        canvas = self._canvas_with_pdf()
        canvas.all_strokes = {0: [self._stroke(0)], 1: [self._stroke(1)], 2: [self._stroke(2)]}
        canvas._anchors = {0: [(10, 10)], 1: [(11, 11)], 2: [(12, 12)]}
        canvas.go_to_page(1)
        self.assertTrue(canvas.delete_current_page())
        self.assertEqual(canvas.n_pages, 2)
        self.assertEqual(canvas.all_strokes[0][0]["pts"][0], (0, 0))
        self.assertEqual(canvas.all_strokes[1][0]["pts"][0], (2, 2))
        self.assertEqual(canvas._anchors, {0: [(10, 10)], 1: [(12, 12)]})

    def test_notes_shift_for_insert(self):
        m = NotesModel()
        m.set(0, "zero")
        m.set(1, "one")
        m.set(2, "two")
        m.shift_for_insert(1)
        self.assertEqual(m.get(0), "zero")
        self.assertEqual(m.get(1), "")      # the inserted page has no note
        self.assertEqual(m.get(2), "one")
        self.assertEqual(m.get(3), "two")

    def test_notes_shift_for_delete(self):
        m = NotesModel()
        m.set(0, "zero")
        m.set(1, "one")
        m.set(2, "two")
        m.shift_for_delete(1)
        self.assertEqual(m.get(0), "zero")
        self.assertEqual(m.get(1), "two")
        self.assertEqual(m.get(2), "")

    def test_insert_then_delete_roundtrip(self):
        m = NotesModel()
        m.set(0, "zero")
        m.set(5, "five")
        m.shift_for_insert(1)
        m.shift_for_delete(1)
        self.assertEqual(m.get(0), "zero")
        self.assertEqual(m.get(5), "five")


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


class TestThemedIcon(unittest.TestCase):
    def test_falls_back_when_first_missing(self):
        from sidemark import _themed_icon
        # A bogus first name forces a fall-through to a real freedesktop icon
        # that every icon theme ships, so the button is never left blank.
        name = _themed_icon("definitely-not-a-real-icon-symbolic",
                            "go-next-symbolic")
        self.assertEqual(name, "go-next-symbolic")

    def test_returns_first_when_all_missing(self):
        from sidemark import _themed_icon
        name = _themed_icon("no-such-icon-aaa-symbolic", "no-such-icon-bbb")
        self.assertEqual(name, "no-such-icon-aaa-symbolic")

    def test_prefers_first_available(self):
        from sidemark import _themed_icon
        name = _themed_icon("go-next-symbolic", "go-previous-symbolic")
        self.assertEqual(name, "go-next-symbolic")


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


class TestMarkdownLineOps(unittest.TestCase):

    def _view(self):
        from sidemark import MarkdownNotesView
        return MarkdownNotesView()

    def _text(self, buf):
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _cursor(self, buf):
        it = buf.get_iter_at_mark(buf.get_insert())
        return it.get_line(), it.get_line_offset()

    def _put_cursor(self, buf, line, col):
        it = buf.get_iter_at_line(line)[1]
        it.forward_chars(col)
        buf.place_cursor(it)

    def _select(self, buf, l0, c0, l1, c1):
        a = buf.get_iter_at_line(l0)[1]; a.forward_chars(c0)
        b = buf.get_iter_at_line(l1)[1]; b.forward_chars(c1)
        buf.select_range(a, b)

    # ── duplicate (Ctrl+D) ──
    def test_duplicate_single_line(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("one\ntwo\nthree")
        self._put_cursor(buf, 1, 2)
        v._duplicate_lines()
        self.assertEqual(self._text(buf), "one\ntwo\ntwo\nthree")
        self.assertEqual(self._cursor(buf), (2, 2))   # cursor lands on the copy

    def test_duplicate_final_line_without_newline(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("a\nb")
        self._put_cursor(buf, 1, 1)
        v._duplicate_lines()
        self.assertEqual(self._text(buf), "a\nb\nb")

    def test_duplicate_multiline_selection(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("a\nb\nc\nd")
        self._select(buf, 1, 0, 2, 1)
        v._duplicate_lines()
        self.assertEqual(self._text(buf), "a\nb\nc\nb\nc\nd")

    def test_duplicate_selection_ending_at_col0_excludes_trailing_line(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("a\nb\nc")
        self._select(buf, 0, 0, 1, 0)   # visually just line "a"
        v._duplicate_lines()
        self.assertEqual(self._text(buf), "a\na\nb\nc")

    # ── move (Alt+↑/↓) ──
    def test_move_line_down(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("one\ntwo\nthree")
        self._put_cursor(buf, 0, 1)
        v._move_lines(1)
        self.assertEqual(self._text(buf), "two\none\nthree")
        self.assertEqual(self._cursor(buf), (1, 1))   # cursor follows the line

    def test_move_line_up(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("one\ntwo\nthree")
        self._put_cursor(buf, 2, 3)
        v._move_lines(-1)
        self.assertEqual(self._text(buf), "one\nthree\ntwo")
        self.assertEqual(self._cursor(buf), (1, 3))

    def test_move_up_at_top_is_noop(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("one\ntwo")
        self._put_cursor(buf, 0, 0)
        v._move_lines(-1)
        self.assertEqual(self._text(buf), "one\ntwo")

    def test_move_down_at_bottom_is_noop(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("one\ntwo")
        self._put_cursor(buf, 1, 0)
        v._move_lines(1)
        self.assertEqual(self._text(buf), "one\ntwo")

    def test_move_final_line_up_keeps_no_trailing_newline(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("a\nb\nc")
        self._put_cursor(buf, 2, 0)
        v._move_lines(-1)
        self.assertEqual(self._text(buf), "a\nc\nb")

    def test_move_selection_down_keeps_selection(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("a\nb\nc\nd")
        self._select(buf, 0, 0, 1, 1)
        v._move_lines(1)
        self.assertEqual(self._text(buf), "c\na\nb\nd")
        self.assertTrue(buf.get_has_selection())


class TestMarkdownSnippets(unittest.TestCase):

    def _view(self):
        from sidemark import MarkdownNotesView
        return MarkdownNotesView()

    def _text(self, buf):
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _put_cursor(self, buf, line, col):
        it = buf.get_iter_at_line(line)[1]
        it.forward_chars(col)
        buf.place_cursor(it)

    def test_date_token_expands(self):
        import datetime
        v = self._view(); buf = v.get_buffer()
        buf.set_text("/date")
        self._put_cursor(buf, 0, 5)        # cursor right after the token
        self.assertTrue(v._expand_snippet())
        self.assertEqual(self._text(buf), datetime.date.today().isoformat())

    def test_date_token_mid_line_expands_only_the_token(self):
        import datetime
        v = self._view(); buf = v.get_buffer()
        buf.set_text("on /date")
        self._put_cursor(buf, 0, 8)
        self.assertTrue(v._expand_snippet())
        self.assertEqual(self._text(buf), "on " + datetime.date.today().isoformat())

    def test_unknown_token_is_left_alone(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("/nope")
        self._put_cursor(buf, 0, 5)
        self.assertFalse(v._expand_snippet())
        self.assertEqual(self._text(buf), "/nope")

    def test_token_glued_to_word_is_not_a_snippet(self):
        v = self._view(); buf = v.get_buffer()
        buf.set_text("foo/date")
        self._put_cursor(buf, 0, 8)
        self.assertFalse(v._expand_snippet())
        self.assertEqual(self._text(buf), "foo/date")

    def test_now_token_expands_with_time(self):
        import datetime
        v = self._view(); buf = v.get_buffer()
        buf.set_text("/now")
        self._put_cursor(buf, 0, 4)
        self.assertTrue(v._expand_snippet())
        # starts with today's date, plus a time component
        self.assertTrue(self._text(buf).startswith(datetime.date.today().isoformat()))
        self.assertRegex(self._text(buf), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")


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

    def test_header_stays_visible_in_fullscreen(self):
        """Regression for #40: the header must live in an Adw.ToolbarView top
        bar, not the titlebar slot — GTK4 hides the titlebar in fullscreen.
        Asserts the structure and that the header stays mapped after
        fullscreen()."""
        errors = []
        results = {}
        app = Adw.Application(application_id="test.sidemark.fullscreen")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
                # the header must be inside the ToolbarView that fills the
                # window, not the titlebar slot GTK4 hides in fullscreen
                results["content_is_toolbarview"] = isinstance(
                    win.get_content(), Adw.ToolbarView)
                results["header_in_toolbarview"] = (
                    win._header.get_ancestor(Adw.ToolbarView) is not None)
                win.fullscreen()
                ctx = GLib.MainContext.default()
                for _ in range(200):
                    ctx.iteration(False)
                results["header_mapped"] = win._header.get_mapped()
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]
        self.assertTrue(results["content_is_toolbarview"])
        self.assertTrue(results["header_in_toolbarview"])
        self.assertTrue(results["header_mapped"],
                        "header must stay mapped in fullscreen")

    def test_export_save_prompt_does_not_raise(self):
        """Ctrl+E with unsaved changes presents the 'Save before exporting?'
        dialog; without changes it goes straight to the options dialog.
        Both construct widgets and connect signals — must not raise."""
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.exportprompt")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    win._mark_dirty()
                    win._on_export()   # dirty → save prompt
                    win._clear_dirty()
                    win._on_export()   # clean → export options
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestCallouts(unittest.TestCase):
    # -- parser ---------------------------------------------------------------

    def test_anchor_without_callout(self):
        parsed = _parse_anchors("Heading\n<!-- anchor:10:20 -->\nBody text")
        self.assertEqual(len(parsed), 1)
        a = parsed[0]
        self.assertEqual((a["x"], a["y"]), (10, 20))
        self.assertIsNone(a["callout"])
        self.assertEqual(a["text"], "Body text")
        self.assertEqual(a["line"], 1)

    def test_anchor_with_callout(self):
        parsed = _parse_anchors("<!-- anchor:10:20 --> <!-- callout:30:40 -->\nBody")
        self.assertEqual(parsed[0]["callout"], (30, 40))
        self.assertEqual(parsed[0]["text"], "Body")

    def test_callout_in_next_paragraph_not_paired(self):
        parsed = _parse_anchors("<!-- anchor:10:20 -->\nBody\n\n<!-- callout:30:40 -->")
        self.assertIsNone(parsed[0]["callout"])

    def test_callout_belongs_to_nearest_preceding_anchor(self):
        text = ("<!-- anchor:1:1 -->\n"
                "<!-- anchor:2:2 --> <!-- callout:5:5 -->\nB")
        parsed = _parse_anchors(text)
        self.assertIsNone(parsed[0]["callout"])
        self.assertEqual(parsed[1]["callout"], (5, 5))

    def test_text_strips_markers_and_markdown(self):
        parsed = _parse_anchors("<!-- anchor:1:1 --> <!-- callout:2:2 -->\n**bold** and `code`")
        self.assertEqual(parsed[0]["text"], "bold and code")

    # -- export rendering (real PDF, content asserted) -------------------------

    def test_export_callout_renders_text(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.pdf")
            out = os.path.join(d, "out.pdf")
            make_pdf(src)
            model = NotesModel()
            model.set(0, "<!-- anchor:100:200 --> <!-- callout:300:400 -->\n"
                         "Important callout fact")
            _export_pdf_with_notes(src, out, model, include_empty=False,
                                   accent=(0.2, 0.5, 0.9))
            doc = fitz.open(out)
            source_text = doc[0].get_text()
            self.assertIn("Important callout fact", source_text)   # box on the page
            self.assertIn("1", source_text)                        # anchor number
            notes_text = doc[1].get_text()
            self.assertNotIn("callout:", notes_text)   # marker stripped from notes page
            doc.close()

    def test_export_callout_near_page_edge_is_clamped(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.pdf")
            out = os.path.join(d, "out.pdf")
            make_pdf(src)
            model = NotesModel()
            model.set(0, "<!-- anchor:10:10 --> <!-- callout:590:838 -->\nEdge note")
            _export_pdf_with_notes(src, out, model, include_empty=False,
                                   accent=(0.2, 0.5, 0.9))
            doc = fitz.open(out)
            self.assertIn("Edge note", doc[0].get_text())
            doc.close()

    # -- canvas rendering -----------------------------------------------------

    def test_canvas_draw_with_callout_does_not_raise(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        try:
            make_pdf(path)
            canvas.load(path)
            canvas._fit_page(800, 600)
            canvas._anchors[0] = _parse_anchors(
                "<!-- anchor:100:100 --> <!-- callout:300:300 -->\nCanvas note")
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 600)
            canvas._draw(canvas, cairo.Context(surf), 800, 600)   # must not raise
        finally:
            os.unlink(path)

    # -- gesture: Ctrl+Alt+drag places a callout --------------------------------

    def _drag_gesture(self):
        g = mock.Mock()
        g.get_current_button.return_value = 1
        g.get_current_event_state.return_value = (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)
        return g

    def test_long_drag_fires_callout_callback(self):
        canvas = PDFCanvas()
        canvas.scale, canvas.offset_x, canvas.offset_y = 1.0, 0.0, 0.0
        placed = []
        canvas.on_callout_placed = lambda x, y: placed.append((x, y))
        canvas._on_drag_begin(self._drag_gesture(), 100, 100)
        self.assertTrue(canvas._callout_dragging)
        canvas._on_drag_end(None, 50, 30)
        self.assertEqual(placed, [(150, 130)])

    def test_short_drag_stays_anchor_only(self):
        canvas = PDFCanvas()
        placed = []
        canvas.on_callout_placed = lambda x, y: placed.append((x, y))
        canvas._on_drag_begin(self._drag_gesture(), 100, 100)
        canvas._on_drag_end(None, 3, 3)
        self.assertEqual(placed, [])

    # -- gesture: drag an anchor to reposition it -------------------------------

    def _plain_drag_gesture(self):
        g = mock.Mock()
        g.get_current_button.return_value = 1
        g.get_current_event_state.return_value = Gdk.ModifierType(0)
        g.get_start_point.return_value = (True, 100.0, 100.0)
        return g

    def _canvas_with_anchor(self):
        canvas = PDFCanvas()
        canvas.scale, canvas.offset_x, canvas.offset_y = 1.0, 0.0, 0.0
        canvas.page = object()
        canvas.select_mode = False
        canvas._anchors[canvas.current_page_idx] = _parse_anchors(
            "<!-- anchor:100:100 -->\nNote")
        return canvas

    def test_drag_moves_anchor_and_fires_callback(self):
        canvas = self._canvas_with_anchor()
        moved = []
        canvas.on_anchor_moved = lambda i, x, y: moved.append((i, x, y))
        canvas.on_anchor_clicked = lambda i: moved.append(("click", i))
        canvas._on_drag_begin(self._plain_drag_gesture(), 100, 100)
        self.assertEqual(canvas._anchor_dragging, 0)
        canvas._on_drag_update(self._plain_drag_gesture(), 40, 25)
        a = canvas._anchors[canvas.current_page_idx][0]
        self.assertEqual((a["x"], a["y"]), (140, 125))  # follows the cursor
        canvas._on_drag_end(self._plain_drag_gesture(), 40, 25)
        self.assertEqual(moved, [(0, 140, 125)])
        self.assertIsNone(canvas._anchor_dragging)

    def test_click_on_anchor_jumps_not_moves(self):
        canvas = self._canvas_with_anchor()
        events = []
        canvas.on_anchor_moved = lambda i, x, y: events.append(("move", i))
        canvas.on_anchor_clicked = lambda i: events.append(("click", i))
        canvas._on_drag_begin(self._plain_drag_gesture(), 100, 100)
        canvas._on_drag_update(self._plain_drag_gesture(), 2, 1)  # below threshold
        canvas._on_drag_end(self._plain_drag_gesture(), 2, 1)
        self.assertEqual(events, [("click", 0)])

    # -- window round-trip ----------------------------------------------------

    def test_window_anchor_move_rewrites_marker(self):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.anchormove")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    win._on_anchor_placed(0, 50, 60)
                    win._on_anchor_moved(0, 120, 200)
                    buf = win._notes_view.get_buffer()
                    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                    if "<!-- anchor:120:200 -->" not in text:
                        raise AssertionError(f"marker not rewritten: {text!r}")
                    if "<!-- anchor:50:60 -->" in text:
                        raise AssertionError(f"old marker remained: {text!r}")
                    if win.canvas._anchors[0][0]["x"] != 120:
                        raise AssertionError(f"canvas not refreshed: {win.canvas._anchors[0]}")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]

    def test_window_anchor_then_callout_in_buffer(self):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.callout")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    win._on_anchor_placed(0, 50, 60)
                    win._on_callout_placed(80, 90)
                    buf = win._notes_view.get_buffer()
                    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                    if "<!-- anchor:50:60 --> <!-- callout:80:90 -->" not in text:
                        raise AssertionError(f"markers not adjacent: {text!r}")
                    parsed = win.canvas._anchors[0]
                    if parsed[0]["callout"] != (80, 90):
                        raise AssertionError(f"canvas missed callout: {parsed}")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestNotesUndoIsolation(unittest.TestCase):
    def test_undo_cannot_cross_page_boundary(self):
        """Ctrl+Z in the notes view must only undo typing on the current
        page — the programmatic set_text on page switches used to enter the
        undo history, so undo could resurrect another page's text."""
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf, n_pages=2)
            app = Adw.Application(application_id="test.sidemark.notesundo")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    win.notes_model.set(0, "alpha")
                    win.notes_model.set(1, "beta")
                    win._restore_note()
                    buf = win._notes_view.get_buffer()
                    win._go_to_page(1)   # buffer now shows "beta"
                    if buf.get_can_undo():
                        raise AssertionError("undo history crossed page switch")
                    buf.undo()   # must be a no-op
                    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                    if text != "beta":
                        raise AssertionError(f"undo corrupted page text: {text!r}")
                    # typing on the current page stays undoable
                    buf.insert(buf.get_end_iter(), "X")
                    if not buf.get_can_undo():
                        raise AssertionError("typing not undoable after restore")
                    buf.undo()
                    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                    if text != "beta":
                        raise AssertionError(f"typing undo broken: {text!r}")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestTocSidebar(unittest.TestCase):
    def _pdf_with_toc(self, d):
        path = os.path.join(d, "toc.pdf")
        make_pdf(path, n_pages=3)
        doc = fitz.open(path)
        doc.set_toc([[1, "Chapter One", 1], [1, "Chapter Two", 2], [2, "Section 2.1", 3]])
        doc.saveIncr()
        doc.close()
        return path

    def test_toc_populated_and_navigates(self):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = self._pdf_with_toc(d)
            plain = os.path.join(d, "plain.pdf")
            make_pdf(plain)
            app = Adw.Application(application_id="test.sidemark.toc")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    rows = []
                    child = win._toc_list.get_first_child()
                    while child is not None:
                        rows.append(child)
                        child = child.get_next_sibling()
                    if len(rows) != 3:
                        raise AssertionError(f"expected 3 TOC rows, got {len(rows)}")
                    if not win._has_toc:
                        raise AssertionError("TOC not detected")
                    if "Ctrl+T" not in (win._toc_btn.get_tooltip_text() or ""):
                        raise AssertionError("tooltip not switched for TOC'd PDF")
                    win._toc_btn.set_active(True)
                    if not win._toc_revealer.get_reveal_child():
                        raise AssertionError("revealer did not open")
                    win._on_toc_row_activated(win._toc_list, rows[1])
                    if win.canvas.current_page_idx != 1:
                        raise AssertionError(
                            f"row activation went to page {win.canvas.current_page_idx}")
                    # a PDF without TOC: falls back to page thumbnails
                    win._do_open_file(plain)
                    if win._has_toc:
                        raise AssertionError("TOC wrongly detected for plain PDF")
                    if not win._toc_thumbs:
                        raise AssertionError("thumbnail mode not active for plain PDF")
                    if "thumbnails" not in (win._toc_btn.get_tooltip_text() or ""):
                        raise AssertionError("missing thumbnails tooltip")
                    win._toc_btn.set_active(False)
                    win._toc_btn.set_active(True)   # must NOT bounce
                    if not win._toc_btn.get_active() or not win._toc_revealer.get_reveal_child():
                        raise AssertionError("toggle bounced despite thumbnail fallback")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestThumbnailSidebar(unittest.TestCase):
    def _run_in_window(self, body):
        errors = []
        app = Adw.Application(application_id="test.sidemark.thumbs")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
                body(win)
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]

    @staticmethod
    def _rows(win):
        rows = []
        child = win._toc_list.get_first_child()
        while child is not None:
            rows.append(child)
            child = child.get_next_sibling()
        return rows

    @staticmethod
    def _pump_thumbs(win):
        ctx = GLib.MainContext.default()
        deadline = time.time() + 5
        while win._thumb_idle_id is not None:
            ctx.iteration(False)
            if time.time() > deadline:
                raise AssertionError("thumbnail render queue never drained")

    def test_no_document_bounces(self):
        def body(win):
            self.assertIn("No document", win._toc_btn.get_tooltip_text() or "")
            win._toc_btn.set_active(True)
            self.assertFalse(win._toc_btn.get_active())
            self.assertFalse(win._toc_revealer.get_reveal_child())

        self._run_in_window(body)

    def test_thumbnails_rendered_and_navigate(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "plain.pdf")
            make_pdf(pdf, n_pages=3)

            def body(win):
                win._do_open_file(pdf)
                rows = self._rows(win)
                self.assertEqual(len(rows), 3)
                self._pump_thumbs(win)
                for row in rows:
                    pic = row.get_child().get_first_child()
                    self.assertIsInstance(pic, Gtk.Picture)
                    tex = pic.get_paintable()
                    self.assertIsNotNone(tex, "thumbnail not rendered")
                    self.assertEqual(tex.get_width(), win.THUMB_WIDTH)
                # clicking a thumbnail navigates
                win._toc_btn.set_active(True)
                win._on_toc_row_activated(win._toc_list, rows[2])
                self.assertEqual(win.canvas.current_page_idx, 2)
                # page change moves the selection
                win.canvas.go_to_page(0)
                self.assertIs(win._toc_list.get_selected_row(), rows[0])

            self._run_in_window(body)

    def test_rows_follow_page_insert_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "plain.pdf")
            make_pdf(pdf, n_pages=2)

            def body(win):
                win._do_open_file(pdf)
                win._toc_btn.set_active(True)
                win._add_blank_page()
                self.assertEqual(len(self._rows(win)), 3)
                # selection tracks the newly inserted current page
                sel = win._toc_list.get_selected_row()
                self.assertIsNotNone(sel)
                self.assertEqual(sel.toc_page, win.canvas.current_page_idx)
                win._delete_current_page()
                self.assertEqual(len(self._rows(win)), 2)
                self._pump_thumbs(win)

            self._run_in_window(body)

    def test_switcher_flips_between_outline_and_thumbnails(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "toc.pdf")
            make_pdf(pdf, n_pages=3)
            doc = fitz.open(pdf)
            doc.set_toc([[1, "One", 1], [1, "Two", 2], [1, "Three", 3]])
            doc.saveIncr()
            doc.close()

            def body(win):
                win._do_open_file(pdf)
                self.assertTrue(win._has_toc)
                self.assertFalse(win._toc_thumbs)
                self.assertTrue(win._toc_switch.get_visible())
                self.assertEqual(win._toc_scroll.get_size_request()[0], 230)
                win._toc_seg_pages.set_active(True)
                self.assertTrue(win._toc_thumbs)
                rows = self._rows(win)
                self.assertEqual(len(rows), 3)
                self.assertIsInstance(rows[0].get_child(), Gtk.Box)
                self.assertEqual(win._toc_scroll.get_size_request()[0],
                                 win.THUMB_WIDTH + 32)
                self._pump_thumbs(win)
                win._toc_seg_outline.set_active(True)
                self.assertFalse(win._toc_thumbs)
                self.assertEqual(win._toc_scroll.get_size_request()[0], 230)
                self.assertIsInstance(self._rows(win)[0].get_child(), Gtk.Label)

            self._run_in_window(body)

    def test_switcher_hidden_without_toc(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "plain.pdf")
            make_pdf(pdf, n_pages=2)

            def body(win):
                win._do_open_file(pdf)
                self.assertFalse(win._toc_switch.get_visible())
                self.assertEqual(win._toc_scroll.get_size_request()[0],
                                 win.THUMB_WIDTH + 32)

            self._run_in_window(body)

    def test_toc_takes_precedence_over_thumbnails(self):
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, "plain.pdf")
            make_pdf(plain, n_pages=2)
            toc_pdf = os.path.join(d, "toc.pdf")
            make_pdf(toc_pdf, n_pages=2)
            doc = fitz.open(toc_pdf)
            doc.set_toc([[1, "One", 1], [1, "Two", 2]])
            doc.saveIncr()
            doc.close()

            def body(win):
                win._do_open_file(plain)
                self.assertTrue(win._toc_thumbs)
                win._do_open_file(toc_pdf)   # switching docs must leave thumbs mode
                self.assertFalse(win._toc_thumbs)
                rows = self._rows(win)
                self.assertEqual(len(rows), 2)
                self.assertIsInstance(rows[0].get_child(), Gtk.Label)

            self._run_in_window(body)


class TestNotesSearch(unittest.TestCase):
    """#43: Ctrl+F also searches the Markdown notes, unified with PDF hits."""

    def _run_in_window(self, body):
        errors = []
        app = Adw.Application(application_id="test.sidemark.notesearch")

        def on_activate(a):
            try:
                body(a)
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]

    def _text_pdf(self, path, page_texts):
        doc = fitz.open()
        for txt in page_texts:
            p = doc.new_page(width=300, height=400)
            p.insert_text((50, 50), txt)
        doc.save(path)
        doc.close()

    def _sel(self, win):
        buf = win._notes_view.get_buffer()
        a = buf.get_iter_at_mark(buf.get_insert())
        b = buf.get_iter_at_mark(buf.get_selection_bound())
        if a.compare(b) > 0:
            a, b = b, a
        return buf.get_text(a, b, False)

    def test_find_note_matches_offsets(self):
        def body(a):
            win = PDFEditorWindow(a); win.present()
            with tempfile.TemporaryDirectory() as d:
                pdf = os.path.join(d, "t.pdf")
                self._text_pdf(pdf, ["x", "y"])
                win._do_open_file(pdf)
                win.notes_model.set(0, "a needle and a needle")
                hits = win._find_note_matches("needle")
                self.assertEqual(hits, {0: [(2, 8), (15, 21)]})
                # case-insensitive
                self.assertEqual(win._find_note_matches("NEEDLE"), {0: [(2, 8), (15, 21)]})
        self._run_in_window(body)

    def test_unified_search_cycles_pdf_and_notes(self):
        def body(a):
            win = PDFEditorWindow(a); win.present()
            with tempfile.TemporaryDirectory() as d:
                pdf = os.path.join(d, "t.pdf")
                # PDF: needle only on page 1
                self._text_pdf(pdf, ["zzz", "needle here", "zzz"])
                win._do_open_file(pdf)
                win.notes_model.set(0, "a needle in notes")
                win.notes_model.set(2, "second needle\nmore")
                win._restore_note()                 # sync page-0 buffer so commit won't clobber

                win._search_entry.set_text("needle")
                win._on_search_changed(win._search_entry)

                # ordered by page: note(0), pdf(1), note(2)
                kinds = [m[0] for m in win._search_matches]
                pages = [m[1] for m in win._search_matches]
                self.assertEqual(kinds, ["note", "pdf", "note"])
                self.assertEqual(pages, [0, 1, 2])

                # starts on the current page's first match (the page-0 note)
                self.assertEqual(win._search_current, 0)
                self.assertEqual(win.canvas.current_page_idx, 0)
                self.assertEqual(self._sel(win).lower(), "needle")
                self.assertEqual(win._search_label.get_label(), "1 / 3")

                # next → PDF hit on page 1, canvas highlights it
                win._search_next()
                self.assertEqual(win.canvas.current_page_idx, 1)
                self.assertIsNotNone(win.canvas.search_current_rect)

                # next → note hit on page 2, notes selection lands on it
                win._search_next()
                self.assertEqual(win.canvas.current_page_idx, 2)
                self.assertEqual(self._sel(win).lower(), "needle")
                self.assertIsNone(win.canvas.search_current_rect)

                # wraps back to the page-0 note
                win._search_next()
                self.assertEqual(win._search_current, 0)
                self.assertEqual(win.canvas.current_page_idx, 0)
        self._run_in_window(body)

    def test_no_matches_marks_error(self):
        def body(a):
            win = PDFEditorWindow(a); win.present()
            with tempfile.TemporaryDirectory() as d:
                pdf = os.path.join(d, "t.pdf")
                self._text_pdf(pdf, ["nothing"])
                win._do_open_file(pdf)
                win._search_entry.set_text("absent")
                win._on_search_changed(win._search_entry)
                self.assertEqual(win._search_matches, [])
                self.assertEqual(win._search_label.get_label(), "0 / 0")
                self.assertTrue(win._search_entry.has_css_class("error"))
        self._run_in_window(body)


class TestMiddleMousePan(unittest.TestCase):
    def setUp(self):
        self.canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp)
        self.canvas.load(self._tmp)
        self.canvas._fit_page(800, 600)

    def tearDown(self):
        os.unlink(self._tmp)

    def test_middle_drag_pans_like_ctrl_drag(self):
        g = mock.Mock()
        g.get_current_button.return_value = 2
        self.canvas._on_drag_begin(g, 100, 100)
        self.assertTrue(self.canvas._panning)
        self.assertFalse(self.canvas._is_fitted)
        ox, oy = self.canvas._pan_start_offset
        g.get_start_point.return_value = (True, 100, 100)
        self.canvas._on_drag_update(g, 30, -20)
        self.assertEqual((self.canvas.offset_x, self.canvas.offset_y),
                         (ox + 30, oy - 20))
        self.canvas._on_drag_end(g, 30, -20)
        self.assertFalse(self.canvas._panning)
        self.assertEqual(len(self.canvas.strokes), 0)   # no stroke committed


class TestSelectMode(unittest.TestCase):
    """#41: in select-text mode a plain drag selects text instead of drawing."""

    def setUp(self):
        self.canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp)
        self.canvas.load(self._tmp)
        self.canvas._fit_page(800, 600)

    def tearDown(self):
        os.unlink(self._tmp)

    def _plain_drag(self):
        g = mock.Mock()
        g.get_current_button.return_value = 1
        g.get_current_event_state.return_value = Gdk.ModifierType(0)
        return g

    def test_draw_mode_starts_a_stroke(self):
        self.canvas.select_mode = False
        g = self._plain_drag()
        self.canvas._on_drag_begin(g, 100, 100)
        self.assertFalse(self.canvas._text_selecting)
        self.assertEqual(len(self.canvas.current_stroke), 1)

    def test_select_mode_selects_text_and_draws_nothing(self):
        self.canvas.select_mode = True
        g = self._plain_drag()
        self.canvas._on_drag_begin(g, 100, 100)
        self.assertTrue(self.canvas._text_selecting)
        g.get_start_point.return_value = (True, 100, 100)
        self.canvas._on_drag_update(g, 60, 8)
        self.canvas._on_drag_end(g, 60, 8)
        self.assertEqual(len(self.canvas.strokes), 0)
        self.assertFalse(self.canvas._text_selecting)


class TestDragAndDrop(unittest.TestCase):
    """#39: dropping a supported file onto the window opens it."""

    def _drop_value(self, paths):
        # mimic what a Wayland file manager delivers: a text/uri-list string
        return "\r\n".join(Gio.File.new_for_path(p).get_uri() for p in paths)

    def _drop(self, make_target, app_id):
        errors, result = [], {}
        with tempfile.TemporaryDirectory() as d:
            target = make_target(d)
            app = Adw.Application(application_id=app_id)

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._dirty = False   # open directly, no save prompt
                    paths = win._dnd_paths(self._drop_value([target]))
                    result["handled"] = win._open_dropped(paths)
                    result["path"] = win._path
                    result["target"] = target
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]
        return result

    def test_drop_pdf_opens_it(self):
        def make(d):
            pdf = os.path.join(d, "dropped.pdf")
            make_pdf(pdf)
            return pdf
        r = self._drop(make, "test.sidemark.dnd.pdf")
        self.assertTrue(r["handled"])
        self.assertEqual(r["path"], r["target"])

    def test_drop_unsupported_is_ignored(self):
        def make(d):
            txt = os.path.join(d, "notes.txt")
            open(txt, "w").close()
            return txt
        r = self._drop(make, "test.sidemark.dnd.txt")
        self.assertFalse(r["handled"])
        self.assertIsNone(r["path"])


class TestReorderPages(unittest.TestCase):
    """#14: drag-to-reorder moves a page and re-keys strokes / notes."""

    def _make_text_pdf(self, path, n):
        doc = fitz.open()
        for i in range(n):
            p = doc.new_page(width=300, height=400)
            p.insert_text((50, 50), f"PAGE{i}")
        doc.save(path)
        doc.close()

    def test_move_order_permutation(self):
        self.assertEqual(PDFCanvas._move_order(3, 0, 2), [1, 2, 0])
        self.assertEqual(PDFCanvas._move_order(3, 2, 0), [2, 0, 1])

    def test_move_page_reorders_document_and_strokes(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        try:
            self._make_text_pdf(tmp, 3)
            canvas.load(tmp)
            s0, s2 = [{"pts": [(1, 1)]}], [{"pts": [(2, 2)]}]
            canvas.all_strokes = {0: s0, 2: s2}
            old_to_new = canvas.move_page(0, 2)
            self.assertEqual(old_to_new, {1: 0, 2: 1, 0: 2})
            texts = [canvas.document[i].get_text().strip() for i in range(3)]
            self.assertEqual(texts, ["PAGE1", "PAGE2", "PAGE0"])
            self.assertEqual(canvas.all_strokes[2], s0)  # page 0 -> 2
            self.assertEqual(canvas.all_strokes[1], s2)  # page 2 -> 1
        finally:
            os.unlink(tmp)

    def test_notes_model_reorder(self):
        nm = NotesModel()
        nm.set(0, "zero")
        nm.set(2, "two")
        nm.reorder({1: 0, 2: 1, 0: 2})
        self.assertEqual(nm.get(2), "zero")
        self.assertEqual(nm.get(1), "two")
        self.assertEqual(nm.get(0), "")

    def test_window_move_page_reorders_notes(self):
        errors, result = [], {}
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            self._make_text_pdf(pdf, 3)
            app = Adw.Application(application_id="test.sidemark.reorder")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    # notes on pages 1 and 2; stay on page 0 so _commit_note
                    # (empty buffer) doesn't clobber them
                    win.notes_model.set(1, "note one")
                    win.notes_model.set(2, "note two")
                    win._move_page(1, 2)   # order -> [0, 2, 1]
                    result["n1"] = win.notes_model.get(1)
                    result["n2"] = win.notes_model.get(2)
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]
        self.assertEqual(result["n2"], "note one")  # page 1 -> 2
        self.assertEqual(result["n1"], "note two")  # page 2 -> 1


class TestThumbHoldPan(unittest.TestCase):
    def _canvas(self, n_pages=2):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            self._tmp = f.name
        make_pdf(self._tmp, n_pages=n_pages)
        canvas.load(self._tmp)
        canvas._fit_page(800, 600)
        return canvas

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.unlink(self._tmp)

    @staticmethod
    def _event(kind, button):
        e = mock.Mock()
        e.get_event_type.return_value = kind
        e.get_button.return_value = button
        return e

    def test_press_starts_pan_release_ends_it(self):
        canvas = self._canvas()
        canvas._mouse_x, canvas._mouse_y = 200, 150
        ctrl = mock.Mock()
        canvas._on_thumb_event(ctrl, self._event(Gdk.EventType.BUTTON_PRESS, 10))
        self.assertTrue(canvas._thumb_panning)
        self.assertFalse(canvas._is_fitted)
        self.assertEqual(canvas._thumb_origin, (200, 150))
        ox, oy = canvas._thumb_start_offset
        # motion while held pans relative to the press origin
        canvas._on_motion(None, 250, 130)
        self.assertEqual((canvas.offset_x, canvas.offset_y), (ox + 50, oy - 20))
        canvas._on_thumb_event(ctrl, self._event(Gdk.EventType.BUTTON_RELEASE, 10))
        self.assertFalse(canvas._thumb_panning)
        # motion after release no longer pans
        canvas._on_motion(None, 400, 400)
        self.assertEqual((canvas.offset_x, canvas.offset_y), (ox + 50, oy - 20))

    def test_other_buttons_ignored(self):
        canvas = self._canvas()
        ctrl = mock.Mock()
        canvas._on_thumb_event(ctrl, self._event(Gdk.EventType.BUTTON_PRESS, 1))
        self.assertFalse(canvas._thumb_panning)

    def test_marshals_event_from_controller_when_arg_none(self):
        canvas = self._canvas()
        ctrl = mock.Mock()
        ctrl.get_current_event.return_value = self._event(
            Gdk.EventType.BUTTON_PRESS, 10)
        canvas._on_thumb_event(ctrl, None)   # PyGObject quirk: arg is None
        self.assertTrue(canvas._thumb_panning)


class TestThumbScrollZoom(unittest.TestCase):
    def test_scroll_zooms_while_thumb_pan_latched(self):
        canvas = PDFCanvas()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        try:
            make_pdf(tmp, n_pages=2)
            canvas.load(tmp)
            canvas._fit_page(800, 600)
            canvas._thumb_panning = True
            canvas._mouse_x = canvas._mouse_y = 300
            ctrl = mock.Mock()
            ctrl.get_current_event_state.return_value = Gdk.ModifierType(0)
            scale = canvas.scale
            canvas._on_scroll(ctrl, 0, -1)
            self.assertAlmostEqual(canvas.scale, scale * 1.1)
            # pan origin rebased so the next motion event doesn't jump
            self.assertEqual(canvas._thumb_origin, (300, 300))
            self.assertEqual(canvas._thumb_start_offset,
                             (canvas.offset_x, canvas.offset_y))
            canvas._on_scroll(ctrl, 0, 1)   # zoom back out, no page flip
            self.assertAlmostEqual(canvas.scale, scale * 1.1 * 0.9)
            self.assertEqual(canvas.current_page_idx, 0)
        finally:
            os.unlink(tmp)


class TestNavKeepsZoom(unittest.TestCase):
    def test_page_keys_keep_zoom_when_zoomed(self):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf, n_pages=3)
            app = Adw.Application(application_id="test.sidemark.navzoom")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    c = win.canvas
                    c.scale = 2.0
                    c._is_fitted = False
                    win._nav_page(1)
                    if c.current_page_idx != 1:
                        raise AssertionError("PageDown did not navigate")
                    if c.scale != 2.0 or c._needs_fit:
                        raise AssertionError("zoom not preserved on PageDown")
                    if c.offset_y != 8.0:
                        raise AssertionError("new page not aligned to top")
                    win._nav_page(-1)
                    if c.current_page_idx != 0 or c.scale != 2.0:
                        raise AssertionError("zoom not preserved on PageUp")
                    # fitted views keep re-fitting
                    c._is_fitted = True
                    win._nav_page(1)
                    if c.current_page_idx != 1 or not c._needs_fit:
                        raise AssertionError("fitted view did not re-fit")
                    # bounds are a no-op
                    c._is_fitted = True
                    win._nav_page(5)
                    if c.current_page_idx != 2:
                        raise AssertionError("clamped nav failed")
                    win._nav_page(1)
                    if c.current_page_idx != 2:
                        raise AssertionError("nav past last page not a no-op")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestAutosave(unittest.TestCase):
    def setUp(self):
        import sidemark as sm
        self.sm = sm
        self._dir = tempfile.mkdtemp(prefix="sidemark-test-autosave-")
        self._patch = mock.patch.object(sm, "AUTOSAVE_DIR",
                                        os.path.join(self._dir, "autosave"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def _make_pdf(self, name="doc.pdf"):
        path = os.path.join(self._dir, name)
        make_pdf(path)
        return path

    def _write_snapshot(self, path, saved_at=None):
        d = self.sm._autosave_dir_for(path)
        os.makedirs(d, exist_ok=True)
        make_pdf(os.path.join(d, "doc.pdf"))
        with open(os.path.join(d, "meta.json"), "w") as f:
            import json
            json.dump({"path": os.path.abspath(path),
                       "saved_at": saved_at or (os.path.getmtime(path) + 100)}, f)
        return d

    def test_save_copy_keeps_original_untouched(self):
        path = self._make_pdf()
        original = open(path, "rb").read()
        canvas = PDFCanvas()
        canvas.load(path)
        canvas.strokes.append({"pts": [(10, 10), (50, 50)], "color": (0, 0, 1), "width": 2})
        out = os.path.join(self._dir, "snap.pdf")
        canvas.save_copy(out)
        doc = fitz.open(out)
        self.assertEqual(len(list(doc[0].annots())), 1)   # stroke is in the copy
        doc.close()
        self.assertEqual(open(path, "rb").read(), original)   # original untouched

    def test_save_still_works_after_save_copy(self):
        path = self._make_pdf()
        canvas = PDFCanvas()
        canvas.load(path)
        canvas.strokes.append({"pts": [(10, 10), (50, 50)], "color": (0, 0, 1), "width": 2})
        canvas.save_copy(os.path.join(self._dir, "snap.pdf"))
        canvas.save(path)
        canvas2 = PDFCanvas()
        canvas2.load(path)
        self.assertEqual(len(canvas2.strokes), 1)

    def test_find_autosave_returns_newer_snapshot(self):
        path = self._make_pdf()
        self._write_snapshot(path)
        found = self.sm._find_autosave(path)
        self.assertIsNotNone(found)
        self.assertTrue(found[0].endswith("doc.pdf"))

    def test_find_autosave_ignores_stale_snapshot(self):
        path = self._make_pdf()
        self._write_snapshot(path, saved_at=os.path.getmtime(path) - 100)
        self.assertIsNone(self.sm._find_autosave(path))

    def test_find_autosave_ignores_path_mismatch(self):
        path = self._make_pdf()
        d = self._write_snapshot(path)
        import json
        meta = json.load(open(os.path.join(d, "meta.json")))
        meta["path"] = "/somewhere/else.pdf"
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"))
        self.assertIsNone(self.sm._find_autosave(path))

    def test_find_autosave_none_when_missing(self):
        path = self._make_pdf()
        self.assertIsNone(self.sm._find_autosave(path))

    def test_discard_autosave_removes_snapshot(self):
        path = self._make_pdf()
        d = self._write_snapshot(path)
        self.sm._discard_autosave(path)
        self.assertFalse(os.path.exists(d))

    def test_prune_removes_only_old_snapshots(self):
        old_pdf = self._make_pdf("old.pdf")
        new_pdf = self._make_pdf("new.pdf")
        import time
        old_dir = self._write_snapshot(old_pdf, saved_at=time.time() - 40 * 86400)
        new_dir = self._write_snapshot(new_pdf, saved_at=time.time())
        self.sm._prune_autosaves(max_age_days=30)
        self.assertFalse(os.path.exists(old_dir))
        self.assertTrue(os.path.exists(new_dir))

    def test_window_autosave_tick_and_cleanup_on_save(self):
        """Dirty window → tick writes a snapshot; explicit save removes it.
        The recovery dialog construction must not raise either."""
        errors = []
        sm = self.sm
        pdf = self._make_pdf()
        app = Adw.Application(application_id="test.sidemark.autosave")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
                win._do_open_file(pdf)
                win.canvas.current_stroke = [(10, 10), (50, 50)]
                win.canvas._on_drag_end(None, 0, 0)   # draws → marks dirty
                if not win._dirty:
                    raise AssertionError("drawing did not mark window dirty")
                win._autosave_tick()
                snap_dir = sm._autosave_dir_for(pdf)
                for fn in ("doc.pdf", "notes.md", "meta.json"):
                    if not os.path.exists(os.path.join(snap_dir, fn)):
                        raise AssertionError(f"snapshot missing {fn}")
                win._maybe_offer_recovery(pdf)   # dialog construction must not raise
                win._on_save()
                if os.path.exists(snap_dir):
                    raise AssertionError("snapshot not cleaned up after save")
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]


class TestLogRetention(unittest.TestCase):
    """The session log must survive sessions that logged errors — atexit also
    runs after unhandled exceptions, which used to delete exactly the logs
    needed for debugging."""

    def setUp(self):
        import logging
        import sidemark as sm
        self._logging = logging
        self._sm = sm
        self._orig = (sm._log_path, sm._log_had_error)

    def tearDown(self):
        self._sm._log_path, self._sm._log_had_error = self._orig

    def _make_log(self):
        fd, path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        return path

    def test_clean_session_log_removed(self):
        path = self._make_log()
        self._sm._log_path = path
        self._sm._log_had_error = False
        self._sm._cleanup_log()
        self.assertFalse(os.path.exists(path))

    def test_log_kept_after_error(self):
        path = self._make_log()
        try:
            self._sm._log_path = path
            self._sm._log_had_error = True
            self._sm._cleanup_log()
            self.assertTrue(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_filter_flags_only_error_records(self):
        logging = self._logging
        self._sm._log_had_error = False
        info = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
        self.assertTrue(self._sm._flag_errors(info))   # filter must not drop records
        self.assertFalse(self._sm._log_had_error)
        err = logging.LogRecord("x", logging.ERROR, "f", 1, "boom", None, None)
        self.assertTrue(self._sm._flag_errors(err))
        self.assertTrue(self._sm._log_had_error)


class TestSaveCallback(unittest.TestCase):
    def test_after_callback_only_on_successful_save(self):
        """_on_save(after=...) must run the callback exactly once on success
        and not at all when the save fails (the unsaved-changes dialog relies
        on this to not destroy the window before/despite a failed save)."""
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.savecb")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)

                    called = []
                    win._on_save(after=lambda: called.append(True))
                    if called != [True]:
                        raise AssertionError(f"after not run on success: {called}")

                    win._path = os.path.join(d, "missing-dir", "doc.pdf")
                    called_on_failure = []
                    win._on_save(after=lambda: called_on_failure.append(True))
                    if called_on_failure:
                        raise AssertionError("after ran despite failed save")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestGlobalUndo(unittest.TestCase):
    """Ctrl+Z undoes the last user action chronologically across canvas and
    notes: each draw/erase gesture is one entry, each uninterrupted typing
    burst between two canvas actions is one entry."""

    @staticmethod
    def _simulate_draw(win):
        """Mimic the stroke-commit branch of PDFCanvas._on_drag_end."""
        canvas = win.canvas
        stroke = {"pts": [(10.0, 10.0), (40.0, 40.0)],
                  "color": (0, 0, 1), "width": 2.0}
        canvas.strokes.append(stroke)
        canvas._undo_stack.append(("draw", canvas.current_page_idx, stroke))
        canvas._redo_stack.clear()
        if canvas.on_change:
            canvas.on_change()
        if canvas.on_user_action:
            canvas.on_user_action()
        return stroke

    @staticmethod
    def _buf_text(win):
        buf = win._notes_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def _run_in_window(self, n_pages, body):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf, n_pages=n_pages)
            app = Adw.Application(application_id="test.sidemark.globalundo")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    body(win)
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]

    def test_draw_type_draw_undo_order(self):
        """The reported bug: draw1 → type → draw2 must undo as
        draw2 → typing → draw1, regardless of keyboard focus."""
        def body(win):
            buf = win._notes_view.get_buffer()
            s1 = self._simulate_draw(win)
            buf.insert(buf.get_end_iter(), "hello")
            s2 = self._simulate_draw(win)

            win._global_undo()   # undoes draw2
            if win.canvas.strokes != [s1]:
                raise AssertionError("first undo did not remove draw2")
            if self._buf_text(win) != "hello":
                raise AssertionError("first undo touched the notes")

            win._global_undo()   # undoes the typing burst
            if self._buf_text(win) != "":
                raise AssertionError(f"second undo did not clear typing: "
                                     f"{self._buf_text(win)!r}")
            if win.canvas.strokes != [s1]:
                raise AssertionError("second undo touched the canvas")

            win._global_undo()   # undoes draw1
            if win.canvas.strokes:
                raise AssertionError("third undo did not remove draw1")
            win._global_undo()   # empty timeline must be a no-op
        self._run_in_window(1, body)

    def test_typing_burst_undone_as_one(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            buf.insert(buf.get_end_iter(), "first ")
            buf.insert(buf.get_end_iter(), "second")
            if len(win._undo_timeline) != 1:
                raise AssertionError(f"expected one burst entry, got "
                                     f"{win._undo_timeline!r}")
            win._global_undo()
            if self._buf_text(win) != "":
                raise AssertionError("burst undo did not clear all typing")
        self._run_in_window(1, body)

    def test_canvas_action_splits_bursts(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            buf.insert(buf.get_end_iter(), "abc")
            self._simulate_draw(win)
            buf.insert(buf.get_end_iter(), "def")
            win._global_undo()   # second burst
            if self._buf_text(win) != "abc":
                raise AssertionError(f"expected 'abc', got {self._buf_text(win)!r}")
            win._global_undo()   # the stroke
            if win.canvas.strokes:
                raise AssertionError("stroke not undone")
            win._global_undo()   # first burst
            if self._buf_text(win) != "":
                raise AssertionError("first burst not undone")
        self._run_in_window(1, body)

    def test_undo_jumps_to_notes_page(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            buf.insert(buf.get_end_iter(), "page0 note")
            win._go_to_page(1)            # commits note, closes burst
            s = self._simulate_draw(win)  # stroke on page 1
            win._global_undo()
            if win.canvas.strokes:
                raise AssertionError("stroke on page 1 not undone")
            win._global_undo()            # typing was on page 0 → must jump back
            if win.canvas.current_page_idx != 0:
                raise AssertionError("undo did not navigate to the notes page")
            if self._buf_text(win) != "":
                raise AssertionError("page 0 typing not undone")
            if win.notes_model.get(0) != "":
                raise AssertionError("notes model kept the undone text")
        self._run_in_window(2, body)

    def test_page_restore_does_not_open_burst(self):
        def body(win):
            win.notes_model.set(0, "alpha")
            win.notes_model.set(1, "beta")
            win._restore_note()
            win._go_to_page(1)
            win._go_to_page(0)
            if win._undo_timeline:
                raise AssertionError("page switches polluted the undo timeline")
        self._run_in_window(2, body)

    def test_timeline_rekeyed_on_page_insert_delete(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            buf.insert(buf.get_end_iter(), "note on page 0")
            win._go_to_page(1)
            buf.insert(buf.get_end_iter(), "note on page 1")
            win._go_to_page(0)
            win._add_blank_page()    # insert at index 1 → old page 1 becomes 2
            pages = [op[1] for op in win._undo_timeline if op[0] == "notes"]
            if pages != [0, 2]:
                raise AssertionError(f"insert re-key wrong: {pages}")
            win._go_to_page(2)
            win._delete_current_page()   # drops page-2 token
            pages = [op[1] for op in win._undo_timeline if op[0] == "notes"]
            if pages != [0]:
                raise AssertionError(f"delete re-key wrong: {pages}")
        self._run_in_window(2, body)

    def test_erase_gesture_fires_user_action(self):
        canvas = PDFCanvas()
        fired = []
        canvas.on_user_action = lambda: fired.append(1)
        stroke = {"pts": [(0.0, 0.0), (5.0, 5.0)], "color": (0, 0, 1), "width": 2.0}
        # erase drag that removed a stroke
        canvas._erasing = True
        canvas._erase_group = 3
        canvas._undo_stack.append(("erase", 0, 0, stroke, 3))
        canvas._on_drag_end(None, 0, 0)
        self.assertEqual(len(fired), 1)
        # erase drag that removed nothing must not fire
        canvas._erasing = True
        canvas._erase_group = 4
        canvas._on_drag_end(None, 0, 0)
        self.assertEqual(len(fired), 1)


class TestGlobalRedo(TestGlobalUndo):
    """Ctrl+Y / Ctrl+Shift+Z re-applies undone actions in reverse order.
    Inherits the undo tests so redo plumbing cannot regress undo."""

    def test_redo_canvas_and_notes_in_reverse_undo_order(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            s1 = self._simulate_draw(win)
            buf.insert(buf.get_end_iter(), "hello")
            win._global_undo()   # typing gone
            win._global_undo()   # draw gone
            if win.canvas.strokes or self._buf_text(win) != "":
                raise AssertionError("undo precondition failed")
            win._global_redo()   # draw back first (last undone)
            if win.canvas.strokes != [s1]:
                raise AssertionError("redo did not restore the stroke")
            win._global_redo()   # typing back
            if self._buf_text(win) != "hello":
                raise AssertionError(f"redo did not restore typing: "
                                     f"{self._buf_text(win)!r}")
            if win.notes_model.get(0) != "hello":
                raise AssertionError("redo did not update the notes model")
            win._global_redo()   # empty redo stack must be a no-op
            # the redone actions are undoable again
            win._global_undo()
            if self._buf_text(win) != "":
                raise AssertionError("undo after redo broken")
        self._run_in_window(1, body)

    def test_new_action_clears_redo(self):
        def body(win):
            buf = win._notes_view.get_buffer()
            self._simulate_draw(win)
            win._global_undo()
            if not win._redo_timeline:
                raise AssertionError("undo did not fill the redo timeline")
            buf.insert(buf.get_end_iter(), "x")   # new action
            if win._redo_timeline:
                raise AssertionError("typing did not clear the redo timeline")
            win._global_redo()   # must be a no-op
            if win.canvas.strokes:
                raise AssertionError("stale redo re-applied a stroke")
        self._run_in_window(1, body)

    def test_canvas_erase_group_redo_roundtrip(self):
        canvas = PDFCanvas()
        s1 = {"pts": [(0.0, 0.0), (5.0, 5.0)], "color": (0, 0, 1), "width": 2.0}
        s2 = {"pts": [(9.0, 9.0), (5.0, 5.0)], "color": (0, 0, 1), "width": 2.0}
        canvas.all_strokes[0] = []
        # one erase gesture removed both strokes (indices as _erase_at records them)
        canvas._undo_stack.append(("erase", 0, 0, s1, 1))
        canvas._undo_stack.append(("erase", 0, 0, s2, 1))
        canvas.undo_last()
        self.assertEqual(canvas.all_strokes[0], [s1, s2])
        canvas.redo_last()
        self.assertEqual(canvas.all_strokes[0], [])
        self.assertEqual(len(canvas._undo_stack), 2)
        canvas.undo_last()   # the round-tripped stack must still undo correctly
        self.assertEqual(canvas.all_strokes[0], [s1, s2])


class TestHighlighter(unittest.TestCase):
    def test_pen_attrs_switch(self):
        canvas = PDFCanvas()
        color, width, opacity = canvas._pen_attrs()
        self.assertEqual((color, width, opacity),
                         (canvas.pen_color, canvas.pen_width, 1.0))
        canvas.highlighter = True
        color, width, opacity = canvas._pen_attrs()
        self.assertEqual((color, width, opacity),
                         (canvas.hl_color, canvas.hl_width, canvas.hl_opacity))

    def test_opacity_roundtrips_through_pdf(self):
        """Highlight strokes keep their translucency across save/load (CA key
        via annot.set_opacity); plain pen strokes stay fully opaque."""
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            canvas = PDFCanvas()
            canvas.load(pdf)
            canvas.all_strokes[0] = [
                {"pts": [(10.0, 10.0), (60.0, 60.0)], "color": (1.0, 0.85, 0.0),
                 "width": 12.0, "opacity": 0.4},
                {"pts": [(10.0, 80.0), (60.0, 90.0)], "color": (0.0, 0.0, 1.0),
                 "width": 2.0},   # pre-highlighter stroke without the key
            ]
            out = os.path.join(d, "saved.pdf")
            canvas.save(out)

            reloaded = PDFCanvas()
            reloaded.load(out)
            strokes = sorted(reloaded.all_strokes[0], key=lambda s: s["width"])
            self.assertEqual(len(strokes), 2)
            self.assertEqual(strokes[0]["opacity"], 1.0)
            self.assertAlmostEqual(strokes[1]["opacity"], 0.4, places=2)
            self.assertAlmostEqual(strokes[1]["width"], 12.0, places=1)
            self.assertAlmostEqual(strokes[1]["color"][0], 1.0, places=2)

    def test_toggle_routes_pen_popover_to_active_tool(self):
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.highlighter")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    pen_width = win.canvas.pen_width
                    pen_color = win.canvas.pen_color

                    win._hl_toggle.set_active(True)
                    if not win.canvas.highlighter:
                        raise AssertionError("toggle did not enable highlighter")
                    win._width_scale.set_value(18.0)
                    if win.canvas.hl_width != 18.0:
                        raise AssertionError("width scale did not set hl_width")
                    if win.canvas.pen_width != pen_width:
                        raise AssertionError("width scale leaked into pen_width")
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue, rgba.alpha = 0.0, 1.0, 0.0, 1.0
                    win._color_btn.set_rgba(rgba)
                    if win.canvas.hl_color != (0.0, 1.0, 0.0):
                        raise AssertionError("color button did not set hl_color")
                    if win.canvas.pen_color != pen_color:
                        raise AssertionError("color button leaked into pen_color")

                    win._pen_seg.set_active(True)   # grouped pair: back to pen
                    if win.canvas.highlighter:
                        raise AssertionError("pen segment did not disable highlighter")
                    if abs(win._width_scale.get_value() - pen_width) > 0.01:
                        raise AssertionError("scale did not return to pen width")
                    # Ctrl+H helper flips the pair both ways
                    win._toggle_highlighter()
                    if not win.canvas.highlighter:
                        raise AssertionError("Ctrl+H did not enable highlighter")
                    win._toggle_highlighter()
                    if win.canvas.highlighter:
                        raise AssertionError("Ctrl+H did not return to pen")
                except Exception as e:
                    errors.append(e)
                finally:
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


class TestRecentFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            sidemark, "RECENT_PATH",
            os.path.join(self._tmp.name, "recent.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _touch(self, name):
        p = os.path.join(self._tmp.name, name)
        open(p, "w").close()
        return p

    def test_add_dedupes_and_orders_newest_first(self):
        a, b = self._touch("a.pdf"), self._touch("b.pdf")
        sidemark._add_recent(a)
        sidemark._add_recent(b)
        sidemark._add_recent(a)   # re-open → moves to front, no duplicate
        paths = [it["path"] for it in sidemark._load_recent()]
        self.assertEqual(paths, [a, b])

    def test_capped_at_max(self):
        for i in range(sidemark.RECENT_MAX + 5):
            sidemark._add_recent(self._touch(f"f{i}.pdf"))
        self.assertEqual(len(sidemark._load_recent()), sidemark.RECENT_MAX)

    def test_missing_files_dropped_and_corrupt_json_tolerated(self):
        a = self._touch("a.pdf")
        sidemark._add_recent(a)
        os.unlink(a)
        self.assertEqual(sidemark._load_recent(), [])
        with open(sidemark.RECENT_PATH, "w") as f:
            f.write("{not json")
        self.assertEqual(sidemark._load_recent(), [])

    def test_list_recent_cli_prints_without_gtk(self):
        a = self._touch("doc.pdf")
        sidemark._add_recent(a)
        env = dict(os.environ, XDG_DATA_HOME=self._tmp.name)
        # the CLI reads $XDG_DATA_HOME/sidemark/recent.json
        os.makedirs(os.path.join(self._tmp.name, "sidemark"), exist_ok=True)
        import shutil
        shutil.copy(sidemark.RECENT_PATH,
                    os.path.join(self._tmp.name, "sidemark", "recent.json"))
        import subprocess
        out = subprocess.run(
            ["/usr/bin/python3", os.path.join(os.path.dirname(__file__), "sidemark.py"),
             "--list-recent"],
            env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(out.returncode, 0)
        self.assertIn(f"doc.pdf\t{a}", out.stdout)

    def test_open_file_records_recent_and_menu_lists_it(self):
        errors = []
        pdf = os.path.join(self._tmp.name, "doc.pdf")
        make_pdf(pdf)
        app = Adw.Application(application_id="test.sidemark.recent")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
                win._do_open_file(pdf)
                paths = [it["path"] for it in sidemark._load_recent()]
                if paths != [pdf]:
                    raise AssertionError(f"open did not record recent: {paths}")
                win._rebuild_recent_menu()
                scroller = win._recent_popover.get_child()
                box = scroller.get_child().get_child()   # viewport → box
                rows = []
                child = box.get_first_child()
                while child is not None:
                    rows.append(child)
                    child = child.get_next_sibling()
                if len(rows) != 1:
                    raise AssertionError(f"expected 1 menu row, got {len(rows)}")
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]

    def test_scratchpad_and_temp_blanks_not_recorded(self):
        errors = []
        app = Adw.Application(application_id="test.sidemark.recentskip")

        def on_activate(a):
            try:
                win = PDFEditorWindow(a)
                win.present()
                tmp_pdf = os.path.join(tempfile.gettempdir(), "sidemark_blank_test.pdf")
                make_pdf(tmp_pdf)
                try:
                    win._do_open_file(tmp_pdf)
                finally:
                    os.unlink(tmp_pdf)
                if sidemark._load_recent():
                    raise AssertionError("temp blank ended up in recents")
            except Exception as e:
                errors.append(e)
            finally:
                GLib.timeout_add(50, lambda: a.quit() or False)

        app.connect("activate", on_activate)
        app.run([])
        if errors:
            raise errors[0]


class TestNotesSidebarAnimation(unittest.TestCase):
    def test_toggle_animates_hide_then_show(self):
        """Toggling the notes panel slides the paned position; the box is
        hidden only once the collapse animation finished."""
        errors = []
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "doc.pdf")
            make_pdf(pdf)
            app = Adw.Application(application_id="test.sidemark.notesanim")

            def on_activate(a):
                try:
                    win = PDFEditorWindow(a)
                    win.present()
                    win._do_open_file(pdf)
                    win._notes_toggle.set_active(False)
                    if win._pane_anim is None:
                        raise AssertionError("toggle did not start an animation")
                    state = {"ticks": 0}

                    def poll():
                        state["ticks"] += 1
                        try:
                            if not win._notes_box.get_visible():
                                # hide completed → re-show must be immediate
                                win._notes_toggle.set_active(True)
                                if not win._notes_box.get_visible():
                                    raise AssertionError("notes box not shown on toggle on")
                                a.quit()
                                return False
                            if state["ticks"] > 40:   # 2 s
                                raise AssertionError("notes box never hidden")
                        except Exception as e:
                            errors.append(e)
                            a.quit()
                            return False
                        return True

                    GLib.timeout_add(50, poll)
                except Exception as e:
                    errors.append(e)
                    GLib.timeout_add(50, lambda: a.quit() or False)

            app.connect("activate", on_activate)
            app.run([])
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)

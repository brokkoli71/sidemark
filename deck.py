"""Sidemark Deck — the presentation editor (slide decks stored as .smdeck JSON).

Loaded lazily by sidemark.py (☰ New presentation, Ctrl+Alt+P, or the
--presentation / --deck CLI flag). This module owns the slide document model,
the editing canvas (select, move, resize, inline text editing, images, ink,
alignment snapping), the second-screen presenter window and PDF export.

Deck mode is one mode of Sidemark's unified window: the window supplies all
chrome — header tools, the deck cluster (new slide / add text / style), the
thumbnail sidebar and the notes panel — and routes it here through plain
callbacks (`on_changed`, `on_slides_changed`, `on_slide_switched`,
`on_selection_changed`, `on_before_slide_switch`). DeckView is only the slide
canvas. Slides are fixed 16:9 pages of 1280×720 logical units; a .smdeck file
is plain JSON, so decks diff, sync and round-trip like any other Sidemark
file."""

import base64
import io
import json
import logging
import os

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, GLib, Gio, Pango, PangoCairo
import cairo

# sidemark points these at its own machinery right after the lazy import:
# the session logger (so deck messages land in the same log) and the notes
# markup renderer (\alpha→α, x^2 superscripts, **bold** …) so deck textboxes
# render math exactly like callouts do. The fallbacks keep this module
# importable on its own.
logger = logging.getLogger(__name__)
notes_to_markup = None            # set to sidemark._notes_to_pango_markup

SLIDE_W, SLIDE_H = 1280, 720      # logical slide coordinates (16:9)
EXPORT_SCALE = 0.75               # 1280×720 logical → 960×540 pt (13.33″×7.5″)
FORMAT_VERSION = 1
SNAP_PX = 6                       # snap distance in logical units
HANDLE_PX = 8                     # resize-handle size in screen pixels
MIN_OBJ = 40                      # objects can't shrink below this (logical)
ERASE_RADIUS = 9                  # eraser hit distance in screen pixels

ACCENT = (0.20, 0.51, 0.89)       # selection / guides / placeholder tint


# ── document model ──────────────────────────────────────────────────────────

def _textbox(x, y, w, h, size, weight="normal", align="left", placeholder=""):
    return {"type": "textbox", "x": x, "y": y, "w": w, "h": h, "text": "",
            "size": size, "weight": weight, "align": align,
            "placeholder": placeholder}


# The standard quick-start layouts ("new slide" menu). Placeholders are plain
# textboxes — click to type; geometry lives here so a future theme can restyle.
LAYOUTS = {
    "title": lambda: [
        _textbox(140, 250, 1000, 130, 72, "bold", "center", "Click to add title"),
        _textbox(240, 410, 800, 70, 32, "normal", "center", "Click to add subtitle"),
    ],
    "content": lambda: [
        _textbox(60, 40, 1160, 90, 48, "bold", "left", "Click to add heading"),
        _textbox(60, 170, 1160, 500, 28, "normal", "left", "Click to add text"),
    ],
    "blank": lambda: [],
}
LAYOUT_LABELS = (("title", "Title slide"),
                 ("content", "Heading + text"),
                 ("blank", "Blank"))


def new_slide(layout="content"):
    return {"layout": layout, "objects": LAYOUTS[layout](), "ink": [],
            "notes": ""}


def _clean(obj):
    """Serializable copy of an object — runtime caches (_surface …) stripped."""
    return {k: v for k, v in obj.items() if not k.startswith("_")}


class DeckModel:
    """The slides and their objects, (de)serialized as .smdeck JSON."""

    def __init__(self):
        self.slides = [new_slide("title")]

    def to_json(self):
        return {"format": "smdeck", "version": FORMAT_VERSION,
                "slide_size": [SLIDE_W, SLIDE_H],
                "slides": [{"layout": s.get("layout", "blank"),
                            "notes": s.get("notes", ""),
                            "ink": s.get("ink", []),
                            "objects": [_clean(o) for o in s["objects"]]}
                           for s in self.slides]}

    @classmethod
    def from_json(cls, data):
        if data.get("format") != "smdeck":
            raise ValueError("not a Sidemark Deck file")
        m = cls()
        m.slides = data.get("slides") or [new_slide("title")]
        for s in m.slides:
            s.setdefault("objects", [])
            s.setdefault("ink", [])
            s.setdefault("notes", "")
        return m

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls.from_json(json.load(f))

    def has_content(self):
        return (len(self.slides) > 1 or bool(self.slides[0].get("ink"))
                or bool(self.slides[0].get("notes"))
                or any(o.get("text") or o["type"] != "textbox"
                       for o in self.slides[0]["objects"]))


def _fit_rect(iw, ih):
    """Rect (x, y, w, h) that fits an iw×ih image into the 16:9 slide,
    centered and letterboxed — a 16:9 source fills it exactly, a 4:3 source
    keeps side margins. Preserves the source aspect ratio."""
    iw, ih = max(iw, 1), max(ih, 1)
    scale = min(SLIDE_W / iw, SLIDE_H / ih)
    w, h = iw * scale, ih * scale
    return (SLIDE_W - w) / 2, (SLIDE_H - h) / 2, w, h


def deck_from_images(images):
    """Build a DeckModel from rendered slide pictures — the PPTX→deck import.

    `images` is a list of (png_bytes, iw, ih, notes): one entry per source
    slide, its page already rasterized to PNG. Each becomes a blank deck slide
    holding that single full-bleed image (fit into the 16:9 page) plus its
    speaker notes, so the imported deck looks pixel-identical to the original
    while staying a real, editable/reorderable/presentable Sidemark deck. The
    original text is a picture here — structured text extraction is a separate
    follow-up (see ideas.csv). An empty list yields a one-slide starter deck."""
    m = DeckModel()
    slides = []
    for png, iw, ih, notes in images:
        x, y, w, h = _fit_rect(iw, ih)
        obj = {"type": "image", "x": x, "y": y, "w": w, "h": h,
               "data": base64.b64encode(png).decode("ascii")}
        slides.append({"layout": "blank", "objects": [obj], "ink": [],
                       "notes": notes or ""})
    m.slides = slides or [new_slide("title")]
    return m


# ── rendering (shared by canvas, sidebar thumbnails, presenter and export) ──

def _image_surface(obj):
    """Decode (and cache) an image object's PNG payload as a cairo surface."""
    surf = obj.get("_surface")
    if surf is None:
        try:
            data = base64.b64decode(obj["data"])
            surf = cairo.ImageSurface.create_from_png(io.BytesIO(data))
        except Exception:
            logger.warning("deck: could not decode image object", exc_info=True)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        obj["_surface"] = surf
    return surf


def _text_layout(cr, obj):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription()
    desc.set_family("Sans")
    desc.set_weight(Pango.Weight.BOLD if obj.get("weight") == "bold"
                    else Pango.Weight.NORMAL)
    desc.set_absolute_size(obj["size"] * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_width(int(obj["w"] * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment({"center": Pango.Alignment.CENTER,
                          "right": Pango.Alignment.RIGHT}
                         .get(obj.get("align"), Pango.Alignment.LEFT))
    return layout


def _draw_ink_stroke(cr, stroke):
    pts = stroke.get("pts", [])
    if len(pts) < 2:
        return
    r, g, b = stroke.get("color", (0.05, 0.05, 0.8))
    cr.set_source_rgba(r, g, b, stroke.get("opacity", 1.0))
    cr.set_line_width(stroke.get("width", 2.0))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.stroke()


def render_slide(cr, slide, show_placeholders=False, skip=None):
    """Draw one slide in logical coordinates (0,0)-(SLIDE_W,SLIDE_H).
    `skip` suppresses one object (its inline editor is showing instead)."""
    cr.set_source_rgb(1, 1, 1)
    cr.rectangle(0, 0, SLIDE_W, SLIDE_H)
    cr.fill()
    for obj in slide["objects"]:
        if obj is skip:
            continue
        if obj["type"] == "image":
            surf = _image_surface(obj)
            iw, ih = max(surf.get_width(), 1), max(surf.get_height(), 1)
            cr.save()
            cr.translate(obj["x"], obj["y"])
            cr.scale(obj["w"] / iw, obj["h"] / ih)
            cr.set_source_surface(surf, 0, 0)
            cr.paint()
            cr.restore()
        elif obj["type"] == "textbox":
            text = obj.get("text", "")
            if not text and not show_placeholders:
                continue
            cr.save()
            cr.translate(obj["x"], obj["y"])
            layout = _text_layout(cr, obj)
            if text:
                cr.set_source_rgb(0.1, 0.1, 0.12)
                # render inline math / Markdown like sidemark's callouts do
                if notes_to_markup is not None:
                    layout.set_markup(notes_to_markup(text))
                else:
                    layout.set_text(text)
            else:
                cr.set_source_rgba(*ACCENT, 0.55)
                layout.set_text(obj.get("placeholder", ""))
            PangoCairo.show_layout(cr, layout)
            cr.restore()
    for stroke in slide.get("ink", []):
        _draw_ink_stroke(cr, stroke)


def render_slide_texture(slide, width):
    """Render a slide into a Gdk.Texture of the given pixel width — used by
    the window's thumbnail sidebar."""
    height = max(1, round(width * SLIDE_H / SLIDE_W))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surf)
    cr.scale(width / SLIDE_W, height / SLIDE_H)
    render_slide(cr, slide, show_placeholders=True)
    surf.flush()
    return Gdk.MemoryTexture.new(
        width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
        GLib.Bytes.new(surf.get_data()), surf.get_stride())


def export_pdf(model, path):
    """Render every slide into a 16:9 PDF page (960×540 pt) — vector text."""
    surface = cairo.PDFSurface(path, SLIDE_W * EXPORT_SCALE, SLIDE_H * EXPORT_SCALE)
    cr = cairo.Context(surface)
    for slide in model.slides:
        cr.save()
        cr.scale(EXPORT_SCALE, EXPORT_SCALE)
        render_slide(cr, slide)
        cr.restore()
        surface.show_page()
    surface.finish()


# ── the editor widget ───────────────────────────────────────────────────────

class DeckView(Gtk.Box):
    """The Deck slide canvas — no chrome of its own; the window provides the
    header tools, deck cluster, thumbnail sidebar and notes panel and drives
    this view through its public methods and callbacks.

    All geometry is kept in logical slide units; the canvas fits the current
    slide into its allocation and converts pointer coordinates back. Editing
    a textbox swaps in a real Gtk.TextView positioned over the box (the canvas
    skips drawing that object meanwhile). Ink shares the window's pen settings
    via `pen_style`. Every mutation lands on one undo stack (`undo()`/`redo()`
    — the window delegates Ctrl+Z/Y here in deck mode) and fires `on_changed`
    for dirty tracking."""

    MARGIN = 24               # surround gap around the slide, screen px

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.model = DeckModel()
        self.current = 0          # index of the slide being edited
        self.selected = None      # the selected object (dict) or None
        # window hooks (all optional):
        self.on_changed = None            # any mutation (dirty tracking)
        self.on_slides_changed = None     # slide count/order changed (sidebar)
        self.on_slide_switched = None     # current changed (sidebar marker …)
        self.on_before_slide_switch = None  # about to switch (commit notes)
        self.on_selection_changed = None  # selection changed (style cluster)
        self.on_live_draw = None          # per-motion ping mid-stroke
        self._undo_stack = []
        self._redo_stack = []
        self._guides = []         # [(x1,y1,x2,y2), …] active snap guides
        self._drag = None         # ("move"|"resize-<h>", obj, before)
        self._editing = None      # textbox currently in the inline editor
        self._editor = None       # the Gtk.TextView overlay child
        # ── ink (pen / highlighter / eraser share the window's pen settings) ──
        self.tool = "select"      # select | pen | highlighter | eraser
        self.pen_style = lambda hl: ((0.05, 0.05, 0.8), 2.0, 1.0)
        self.current_stroke = []  # logical points of the in-flight stroke
        self._stroke_style = None  # (color, width, opacity) of that stroke
        self._erased_now = []     # [(idx, stroke), …] removed in this gesture

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.set_focusable(True)
        self.canvas.set_draw_func(self._draw)
        self._overlay = Gtk.Overlay()
        self._overlay.set_child(self.canvas)
        self._fixed = Gtk.Fixed()       # hosts the inline text editor
        self._fixed.set_can_target(True)
        self._fixed.set_hexpand(True)
        self._fixed.set_vexpand(True)
        self._overlay.add_overlay(self._fixed)
        self._fixed.set_visible(False)
        self.append(self._overlay)

        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("pressed", self._on_click)
        self.canvas.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.canvas.add_controller(drag)
        # right-drag erases while an ink tool is active (like on a PDF page)
        erase = Gtk.GestureDrag()
        erase.set_button(3)
        erase.connect("drag-begin", self._on_erase_begin)
        erase.connect("drag-update", self._on_erase_update)
        erase.connect("drag-end", self._on_erase_end)
        self.canvas.add_controller(erase)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.canvas.add_controller(keys)

        # unique CSS class scoped to this instance styles the inline editor
        self._editor_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self._editor_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    # ── change plumbing ──────────────────────────────────────────────────

    def _slide(self):
        return self.model.slides[self.current]

    def _push(self, op):
        self._undo_stack.append(op)
        self._redo_stack.clear()

    def _changed(self):
        self.canvas.queue_draw()
        if self.on_changed:
            self.on_changed()

    def _slides_changed(self):
        """Slide count or order changed — the sidebar must rebuild."""
        if self.on_slides_changed:
            self.on_slides_changed()

    def _selection_changed(self):
        if self.on_selection_changed:
            self.on_selection_changed()

    # ── slide navigation (window sidebar / presenter / PageUp+Down) ──────

    def set_current(self, idx):
        if idx == self.current or not (0 <= idx < len(self.model.slides)):
            return
        self._commit_editor()
        if self.on_before_slide_switch:
            self.on_before_slide_switch()   # commit speaker notes first
        self.current = idx
        self.selected = None
        self._selection_changed()
        self.canvas.queue_draw()
        if self.on_slide_switched:
            self.on_slide_switched()

    def next_slide(self):
        self.set_current(self.current + 1)

    def prev_slide(self):
        self.set_current(self.current - 1)

    # ── model mutations (all undoable) ───────────────────────────────────

    def add_slide(self, layout="content"):
        self._commit_editor()
        idx = self.current + 1
        self.model.slides.insert(idx, new_slide(layout))
        self._push(("add_slide", idx))
        self._slides_changed()
        self.set_current(idx)
        self._changed()

    def delete_slide(self):
        if len(self.model.slides) <= 1:
            return
        self._commit_editor()
        if self.on_before_slide_switch:
            self.on_before_slide_switch()   # notes travel with the slide
        idx = self.current
        slide = self.model.slides.pop(idx)
        self._push(("remove_slide", idx, slide))
        self.current = min(idx, len(self.model.slides) - 1)
        self.selected = None
        self._slides_changed()
        self._selection_changed()
        if self.on_slide_switched:
            self.on_slide_switched()
        self._changed()

    def move_slide(self, frm, to):
        """Reorder (sidebar drag): move the slide at `frm` before index `to`."""
        n = len(self.model.slides)
        if not (0 <= frm < n):
            return
        to = max(0, min(to, n - 1))
        if frm == to:
            return
        self._commit_editor()
        slide = self.model.slides.pop(frm)
        self.model.slides.insert(to, slide)
        self._push(("move_slide", frm, to))
        self.current = to
        self._slides_changed()
        if self.on_slide_switched:
            self.on_slide_switched()
        self._changed()

    def add_textbox(self):
        obj = _textbox(340, 300, 600, 120, 28, placeholder="Click to add text")
        self._slide()["objects"].append(obj)
        self._push(("add", self.current, obj))
        self.selected = obj
        self._selection_changed()
        self._changed()
        self.start_edit(obj)

    def add_image_bytes(self, png_data, iw, ih):
        """Insert an image object (PNG payload), fit into the slide center."""
        iw, ih = max(iw, 1), max(ih, 1)
        # fit large images down, grow tiny ones to a graspable size — both
        # uniformly, so the aspect ratio always survives
        scale = max(min(640 / iw, 480 / ih, 1.0), MIN_OBJ / iw, MIN_OBJ / ih)
        w, h = iw * scale, ih * scale
        obj = {"type": "image", "x": (SLIDE_W - w) / 2, "y": (SLIDE_H - h) / 2,
               "w": w, "h": h,
               "data": base64.b64encode(png_data).decode("ascii")}
        self._slide()["objects"].append(obj)
        self._push(("add", self.current, obj))
        self.selected = obj
        self._selection_changed()
        self._changed()

    def add_image_file(self, path):
        try:
            tex = Gdk.Texture.new_from_filename(path)
        except GLib.Error as e:
            logger.warning("deck: cannot load image %s: %s", path, e)
            return False
        data = tex.save_to_png_bytes().get_data()
        self.add_image_bytes(bytes(data), tex.get_width(), tex.get_height())
        return True

    def delete_selected(self):
        obj = self.selected
        if not obj:
            return
        self._commit_editor()
        objs = self._slide()["objects"]
        if obj in objs:
            idx = objs.index(obj)
            objs.pop(idx)
            self._push(("remove", self.current, idx, obj))
        self.selected = None
        self._selection_changed()
        self._changed()

    def apply_style(self, size=None, bold=None, align=None):
        """Style the selected textbox (the window's deck cluster calls this)."""
        obj = self.selected
        if not obj or obj["type"] != "textbox":
            return
        before = _clean(obj)
        if size is not None:
            obj["size"] = int(size)
        if bold is not None:
            obj["weight"] = "bold" if bold else "normal"
        if align is not None:
            obj["align"] = align
        if _clean(obj) != before:
            self._push(("modify", self.current,
                        self._slide()["objects"].index(obj), before))
            self._changed()

    def selection_style(self):
        """(is_textbox, size, bold, align) of the selection, for the cluster."""
        obj = self.selected
        if not obj or obj["type"] != "textbox":
            return (False, 0, False, "left")
        return (True, obj["size"], obj.get("weight") == "bold",
                obj.get("align", "left"))

    def pick_image(self):
        dialog = Gtk.FileDialog.new()
        dialog.set_title("Add image…")
        f = Gtk.FileFilter()
        f.set_name("Images")
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.bmp",
                    "*.svg"):
            f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        def done(dlg, result):
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self.add_image_file(file.get_path())
        dialog.open(self.get_root(), None, done)

    def paste_image(self):
        """Ctrl+V: pull an image off the clipboard, if there is one."""
        clip = Gdk.Display.get_default().get_clipboard()
        def done(cb, result):
            try:
                tex = cb.read_texture_finish(result)
            except GLib.Error:
                return
            if tex is not None:
                data = tex.save_to_png_bytes().get_data()
                GLib.idle_add(self.add_image_bytes, bytes(data),
                              tex.get_width(), tex.get_height())
        clip.read_texture_async(None, done)

    # ── undo / redo ──────────────────────────────────────────────────────

    def undo(self):
        if not self._undo_stack:
            return
        self._commit_editor()
        op = self._undo_stack.pop()
        self._redo_stack.append(self._apply(op))
        self._after_history()

    def redo(self):
        if not self._redo_stack:
            return
        op = self._redo_stack.pop()
        self._undo_stack.append(self._apply(op))
        self._after_history()

    def _after_history(self):
        self._slides_changed()
        self._selection_changed()
        if self.on_slide_switched:
            self.on_slide_switched()
        self._changed()

    def _apply(self, op):
        """Apply the inverse of `op`, returning the op that re-inverts it."""
        kind = op[0]
        if kind == "add":                      # inverse: remove the object
            _, slide_idx, obj = op
            self._goto(slide_idx)
            objs = self.model.slides[slide_idx]["objects"]
            idx = objs.index(obj)
            objs.pop(idx)
            if self.selected is obj:
                self.selected = None
            return ("remove", slide_idx, idx, obj)
        if kind == "remove":                   # inverse: put it back
            _, slide_idx, idx, obj = op
            self._goto(slide_idx)
            self.model.slides[slide_idx]["objects"].insert(idx, obj)
            return ("add", slide_idx, obj)
        if kind == "modify":                   # inverse: swap stored state
            _, slide_idx, idx, before = op
            self._goto(slide_idx)
            obj = self.model.slides[slide_idx]["objects"][idx]
            now = _clean(obj)
            obj.clear()
            obj.update(before)
            return ("modify", slide_idx, idx, now)
        if kind == "add_slide":                # inverse: remove the slide
            _, idx = op
            slide = self.model.slides.pop(idx)
            self.current = min(self.current, len(self.model.slides) - 1)
            self.selected = None
            return ("remove_slide", idx, slide)
        if kind == "remove_slide":             # inverse: restore the slide
            _, idx, slide = op
            self.model.slides.insert(idx, slide)
            self.current = idx
            self.selected = None
            return ("add_slide", idx)
        if kind == "move_slide":               # inverse: move it back
            _, frm, to = op
            slide = self.model.slides.pop(to)
            self.model.slides.insert(frm, slide)
            self.current = frm
            return ("move_slide", to, frm)
        if kind == "ink_add":                  # inverse: lift the stroke off
            _, slide_idx, stroke = op
            self._goto(slide_idx)
            ink = self.model.slides[slide_idx]["ink"]
            idx = ink.index(stroke)
            ink.pop(idx)
            return ("ink_remove", slide_idx, idx, stroke)
        if kind == "ink_remove":               # inverse: lay it back down
            _, slide_idx, idx, stroke = op
            self._goto(slide_idx)
            self.model.slides[slide_idx]["ink"].insert(idx, stroke)
            return ("ink_add", slide_idx, stroke)
        if kind == "ink_erase":                # inverse: restore the erased set
            _, slide_idx, pairs = op
            self._goto(slide_idx)
            ink = self.model.slides[slide_idx]["ink"]
            for idx, stroke in reversed(pairs):
                ink.insert(min(idx, len(ink)), stroke)
            return ("ink_unerase", slide_idx, pairs)
        if kind == "ink_unerase":              # inverse: erase them again
            _, slide_idx, pairs = op
            self._goto(slide_idx)
            ink = self.model.slides[slide_idx]["ink"]
            for _, stroke in pairs:
                if stroke in ink:
                    ink.remove(stroke)
            return ("ink_erase", slide_idx, pairs)
        raise ValueError(f"unknown deck op {kind!r}")

    def _goto(self, idx):
        if idx != self.current and 0 <= idx < len(self.model.slides):
            self.current = idx

    # ── canvas drawing ───────────────────────────────────────────────────

    def _slide_rect(self):
        """The slide's screen rectangle (x, y, scale) inside the canvas."""
        w = self.canvas.get_width()
        h = self.canvas.get_height()
        scale = max(min((w - 2 * self.MARGIN) / SLIDE_W,
                        (h - 2 * self.MARGIN) / SLIDE_H), 0.05)
        return ((w - SLIDE_W * scale) / 2, (h - SLIDE_H * scale) / 2, scale)

    def _to_slide(self, px, py):
        x, y, scale = self._slide_rect()
        return (px - x) / scale, (py - y) / scale

    def _draw(self, _area, cr, w, h):
        x, y, scale = self._slide_rect()
        # drop shadow, like a PDF page
        cr.set_source_rgba(0, 0, 0, 0.28)
        cr.rectangle(x + 3, y + 3, SLIDE_W * scale, SLIDE_H * scale)
        cr.fill()
        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, SLIDE_W, SLIDE_H)
        cr.clip()
        render_slide(cr, self._slide(), show_placeholders=True,
                     skip=self._editing)
        # snap guides while dragging
        cr.set_source_rgba(*ACCENT, 0.9)
        cr.set_line_width(1 / scale)
        cr.set_dash([4 / scale, 4 / scale])
        for (x1, y1, x2, y2) in self._guides:
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
        cr.stroke()
        cr.set_dash([])
        # the in-flight stroke rides on top until it's committed
        if self.current_stroke and self._stroke_style:
            color, width, opacity = self._stroke_style
            _draw_ink_stroke(cr, {"pts": self.current_stroke, "color": color,
                                  "width": width, "opacity": opacity})
        cr.restore()
        # selection box + handles (drawn unscaled so handles keep their size)
        obj = self.selected
        if obj is not None:
            ox, oy = x + obj["x"] * scale, y + obj["y"] * scale
            ow, oh = obj["w"] * scale, obj["h"] * scale
            cr.set_source_rgba(*ACCENT, 0.9)
            cr.set_line_width(1.5)
            cr.rectangle(ox, oy, ow, oh)
            cr.stroke()
            cr.set_source_rgb(1, 1, 1)
            for hx, hy in self._handle_points(ox, oy, ow, oh):
                cr.rectangle(hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                             HANDLE_PX, HANDLE_PX)
            cr.fill_preserve()
            cr.set_source_rgba(*ACCENT, 1)
            cr.set_line_width(1)
            cr.stroke()

    @staticmethod
    def _handle_points(x, y, w, h):
        """The 8 resize handles: corners then edge midpoints (order matters —
        it matches _HANDLES)."""
        return ((x, y), (x + w, y), (x, y + h), (x + w, y + h),
                (x + w / 2, y), (x + w / 2, y + h),
                (x, y + h / 2), (x + w, y + h / 2))

    _HANDLES = ("nw", "ne", "sw", "se", "n", "s", "w", "e")

    # ── pointer interaction ──────────────────────────────────────────────

    def _hit_handle(self, px, py):
        obj = self.selected
        if obj is None:
            return None
        x, y, scale = self._slide_rect()
        ox, oy = x + obj["x"] * scale, y + obj["y"] * scale
        ow, oh = obj["w"] * scale, obj["h"] * scale
        for name, (hx, hy) in zip(self._HANDLES,
                                  self._handle_points(ox, oy, ow, oh)):
            if abs(px - hx) <= HANDLE_PX and abs(py - hy) <= HANDLE_PX:
                return name
        return None

    def _hit_object(self, sx, sy):
        """Topmost object under a slide-coordinate point."""
        for obj in reversed(self._slide()["objects"]):
            if (obj["x"] <= sx <= obj["x"] + obj["w"]
                    and obj["y"] <= sy <= obj["y"] + obj["h"]):
                return obj
        return None

    def _on_click(self, _g, n_press, px, py):
        self.canvas.grab_focus()
        if self.tool != "select":
            return
        if n_press == 2:
            obj = self._hit_object(*self._to_slide(px, py))
            if obj is not None and obj["type"] == "textbox":
                self.start_edit(obj)

    def _on_drag_begin(self, _g, px, py):
        self._commit_editor()
        if self.tool in ("pen", "highlighter"):
            self._ink_begin(px, py)
            return
        if self.tool == "eraser":
            self._erase_begin(px, py)
            return
        handle = self._hit_handle(px, py)
        if handle:
            obj = self.selected
            self._drag = (f"resize-{handle}", obj, _clean(obj))
            return
        obj = self._hit_object(*self._to_slide(px, py))
        if obj is not self.selected:
            self.selected = obj
            self._selection_changed()
            self.canvas.queue_draw()
        self._drag = ("move", obj, _clean(obj)) if obj else None

    def _on_drag_update(self, g, dx, dy):
        if self.tool in ("pen", "highlighter"):
            self._ink_motion(dx, dy)
            return
        if self.tool == "eraser":
            self._erase_motion(dx, dy)
            return
        if not self._drag:
            return
        mode, obj, before = self._drag
        _, _, scale = self._slide_rect()
        sdx, sdy = dx / scale, dy / scale
        if mode == "move":
            nx, ny = before["x"] + sdx, before["y"] + sdy
            nx, ny = self._snap_move(obj, nx, ny)
            obj["x"], obj["y"] = nx, ny
        else:
            self._resize(obj, before, mode.split("-", 1)[1], sdx, sdy)
        self.canvas.queue_draw()

    def _on_drag_end(self, _g, dx, dy):
        if self.tool in ("pen", "highlighter"):
            self._ink_commit()
            return
        if self.tool == "eraser":
            self._erase_commit()
            return
        self._guides = []
        if not self._drag:
            self.canvas.queue_draw()
            return
        mode, obj, before = self._drag
        self._drag = None
        if obj and _clean(obj) != before:
            self._push(("modify", self.current,
                        self._slide()["objects"].index(obj), before))
            self._changed()
        else:
            self.canvas.queue_draw()

    @staticmethod
    def _resize(obj, before, handle, sdx, sdy):
        x, y, w, h = before["x"], before["y"], before["w"], before["h"]
        if "w" in handle:
            nw = max(w - sdx, MIN_OBJ)
            obj["x"], obj["w"] = x + w - nw, nw
        if "e" in handle:
            obj["w"] = max(w + sdx, MIN_OBJ)
        if "n" in handle:
            nh = max(h - sdy, MIN_OBJ)
            obj["y"], obj["h"] = y + h - nh, nh
        if "s" in handle:
            obj["h"] = max(h + sdy, MIN_OBJ)

    def _snap_move(self, obj, nx, ny):
        """Snap a moving object to the slide center and its siblings' edges;
        remembers the guide lines to draw."""
        self._guides = []
        w, h = obj["w"], obj["h"]
        # candidate x positions: (slide-x of the guide, obj x that aligns to it)
        xcands = [(SLIDE_W / 2, SLIDE_W / 2 - w / 2)]
        ycands = [(SLIDE_H / 2, SLIDE_H / 2 - h / 2)]
        for other in self._slide()["objects"]:
            if other is obj:
                continue
            for gx in (other["x"], other["x"] + other["w"],
                       other["x"] + other["w"] / 2):
                xcands += [(gx, gx), (gx, gx - w), (gx, gx - w / 2)]
            for gy in (other["y"], other["y"] + other["h"],
                       other["y"] + other["h"] / 2):
                ycands += [(gy, gy), (gy, gy - h), (gy, gy - h / 2)]
        best = min(xcands, key=lambda c: abs(nx - c[1]))
        if abs(nx - best[1]) <= SNAP_PX:
            nx = best[1]
            self._guides.append((best[0], 0, best[0], SLIDE_H))
        best = min(ycands, key=lambda c: abs(ny - c[1]))
        if abs(ny - best[1]) <= SNAP_PX:
            ny = best[1]
            self._guides.append((0, best[0], SLIDE_W, best[0]))
        return nx, ny

    # ── ink (pen / highlighter / eraser) ─────────────────────────────────

    def set_tool(self, mode):
        """Window's tool switch: pen / highlighter / eraser ink the slide,
        anything else is the object-select arrow."""
        if mode not in ("pen", "highlighter", "eraser"):
            mode = "select"
        if mode == self.tool:
            return
        self._commit_editor()
        self.tool = mode
        if mode != "select":
            self.selected = None
            self._selection_changed()
        cursor = {"pen": "crosshair", "highlighter": "crosshair",
                  "eraser": "cell"}.get(mode, "default")
        self.canvas.set_cursor(Gdk.Cursor.new_from_name(cursor))
        self.canvas.queue_draw()

    def _ink_begin(self, px, py):
        color, width, opacity = self.pen_style(self.tool == "highlighter")
        self._stroke_style = (tuple(color), float(width), float(opacity))
        self._ink_start = (px, py)
        self.current_stroke = [list(self._to_slide(px, py))]

    def _ink_motion(self, dx, dy):
        if not self.current_stroke:
            return
        px, py = self._ink_start
        self.current_stroke.append(list(self._to_slide(px + dx, py + dy)))
        self.canvas.queue_draw()
        if self.on_live_draw:
            self.on_live_draw()

    def _ink_commit(self):
        pts, self.current_stroke = self.current_stroke, []
        if len(pts) >= 2 and self._stroke_style is not None:
            color, width, opacity = self._stroke_style
            stroke = {"pts": pts, "color": list(color),
                      "width": width, "opacity": opacity}
            self._slide()["ink"].append(stroke)
            self._push(("ink_add", self.current, stroke))
            self._changed()
        else:
            self.canvas.queue_draw()
        if self.on_live_draw:
            self.on_live_draw()

    def _erase_begin(self, px, py):
        self._erased_now = []
        self._erase_start = (px, py)
        self._erase_at(*self._to_slide(px, py))

    def _erase_motion(self, dx, dy):
        px, py = self._erase_start
        self._erase_at(*self._to_slide(px + dx, py + dy))

    def _erase_commit(self):
        if self._erased_now:
            self._push(("ink_erase", self.current, list(self._erased_now)))
            self._erased_now = []
            self._changed()

    def _erase_at(self, sx, sy):
        """Remove every stroke with a point within the eraser radius."""
        _, _, scale = self._slide_rect()
        r2 = (ERASE_RADIUS / scale) ** 2
        ink = self._slide()["ink"]
        for i in range(len(ink) - 1, -1, -1):
            if any((px - sx) ** 2 + (py - sy) ** 2 <= r2
                   for px, py in ink[i].get("pts", [])):
                self._erased_now.append((i, ink.pop(i)))
        self.canvas.queue_draw()
        if self.on_live_draw:
            self.on_live_draw()

    # right-drag erases whenever an ink tool is active (mirrors the PDF canvas)
    def _on_erase_begin(self, _g, px, py):
        if self.tool != "select":
            self._erase_begin(px, py)

    def _on_erase_update(self, _g, dx, dy):
        if self.tool != "select" and hasattr(self, "_erase_start"):
            self._erase_motion(dx, dy)

    def _on_erase_end(self, _g, dx, dy):
        if self.tool != "select":
            self._erase_commit()

    # ── keyboard ─────────────────────────────────────────────────────────

    def _on_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.selected:
            self.delete_selected()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.selected:
                self.selected = None
                self._selection_changed()
                self.canvas.queue_draw()
                return True
        if (state & Gdk.ModifierType.CONTROL_MASK
                and keyval in (Gdk.KEY_v, Gdk.KEY_V)):
            self.paste_image()
            return True
        return False

    # ── inline text editing ──────────────────────────────────────────────

    def start_edit(self, obj):
        self._commit_editor()
        self.selected = obj
        self._selection_changed()
        self._editing = obj
        x, y, scale = self._slide_rect()
        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.get_buffer().set_text(obj.get("text", ""))
        tv.set_justification({"center": Gtk.Justification.CENTER,
                              "right": Gtk.Justification.RIGHT}
                             .get(obj.get("align"), Gtk.Justification.LEFT))
        cls = f"deck-editor-{id(self)}"
        tv.add_css_class(cls)
        weight = 700 if obj.get("weight") == "bold" else 400
        self._editor_css.load_from_string(
            f".{cls}, .{cls} text {{ background-color: white; color: #1a1a1f;"
            f" font-size: {max(obj['size'] * scale, 6):.1f}px;"
            f" font-weight: {weight}; font-family: Sans; caret-color: #1a1a1f; }}")
        tv.set_size_request(int(obj["w"] * scale), int(obj["h"] * scale))
        self._fixed.put(tv, x + obj["x"] * scale, y + obj["y"] * scale)
        self._fixed.set_visible(True)
        self._editor = tv
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: self._commit_editor())
        tv.add_controller(focus)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_editor_key)
        tv.add_controller(keys)
        self.canvas.queue_draw()
        tv.grab_focus()

    def _on_editor_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self._commit_editor()
            self.canvas.grab_focus()
            return True
        return False

    def _commit_editor(self):
        """Write the inline editor's text back into its object and drop it."""
        tv, obj = self._editor, self._editing
        if tv is None or obj is None:
            return
        self._editor = None
        self._editing = None
        buf = tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        self._fixed.remove(tv)
        self._fixed.set_visible(False)
        if text != obj.get("text", ""):
            objs = self._slide()["objects"]
            if obj in objs:
                before = _clean(obj)
                before["text"] = obj.get("text", "")
                obj["text"] = text
                self._push(("modify", self.current, objs.index(obj), before))
                self._changed()
                return
        self.canvas.queue_draw()

    # ── document plumbing (called by the window) ─────────────────────────

    def _reset_view(self):
        self.current = 0
        self.selected = None
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._slides_changed()
        self._selection_changed()
        self.canvas.queue_draw()

    def load(self, path):
        self._commit_editor()
        self.model = DeckModel.load(path)
        self._reset_view()

    def reset(self):
        self._commit_editor()
        self.model = DeckModel()
        self._reset_view()

    def save(self, path):
        self._commit_editor()
        self.model.save(path)

    def export_pdf(self, path):
        self._commit_editor()
        export_pdf(self.model, path)


# ── second-screen presenter ─────────────────────────────────────────────────

class DeckPresenterWindow(Adw.Window):
    """A view-only mirror of the deck for a projector: fullscreen black
    surround, the current slide fit whole, live ink while a stroke is still
    being drawn. Kept deliberately bare, like the PDF PresenterWindow — the
    presentation timer and big prev/next controls live on the editor window.
    It still pages when focused (clicker-friendly): click / Space / arrows /
    PageUp+Down advance and go back; Esc or F5 closes."""

    def __init__(self, app, deck_view, on_nav=None):
        super().__init__(application=app)
        self.set_title("Sidemark — Presenter")
        self._dv = deck_view
        self._on_nav = on_nav   # callback(delta) — drives the editor's nav
        area = Gtk.DrawingArea()
        area.set_draw_func(self._draw)
        self.canvas = area
        self.set_content(area)
        deck_view.on_live_draw = area.queue_draw

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        click = Gtk.GestureClick()
        click.set_button(0)   # 0 = listen to every button
        click.connect("pressed", self._on_click)
        area.add_controller(click)

    def _draw(self, _a, cr, w, h):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        dv = self._dv
        slide = dv.model.slides[dv.current]
        scale = min(w / SLIDE_W, h / SLIDE_H)
        cr.translate((w - SLIDE_W * scale) / 2, (h - SLIDE_H * scale) / 2)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, SLIDE_W, SLIDE_H)
        cr.clip()
        render_slide(cr, slide)          # no placeholders on the projector
        if dv.current_stroke and dv._stroke_style:
            color, width, opacity = dv._stroke_style
            _draw_ink_stroke(cr, {"pts": dv.current_stroke, "color": color,
                                  "width": width, "opacity": opacity})

    def sync_page(self):
        self.canvas.queue_draw()

    def refresh(self):
        self.canvas.queue_draw()

    def detach(self):
        """Stop receiving the deck's live-draw pings (window is closing)."""
        if self._dv.on_live_draw == self.canvas.queue_draw:
            self._dv.on_live_draw = None

    def _nav(self, delta):
        if self._on_nav is not None:
            self._on_nav(delta)
        else:
            self._dv.set_current(self._dv.current + delta)

    def _on_key(self, _c, keyval, _code, _state):
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_F5):
            self.close()
            return True
        if keyval in (Gdk.KEY_space, Gdk.KEY_Right, Gdk.KEY_Down,
                      Gdk.KEY_Page_Down):
            self._nav(1)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Page_Up):
            self._nav(-1)
            return True
        return False

    def _on_click(self, gesture, _n, _x, _y):
        # click / side button 8 advance; right-click / side button 9 go back
        # (same bindings as the PDF presenter)
        button = gesture.get_current_button()
        if button in (1, 8):
            self._nav(1)
        elif button in (3, 9):
            self._nav(-1)

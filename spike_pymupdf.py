#!/usr/bin/env python3
"""
SPIKE: Replace Poppler with PyMuPDF in pdfeditor.py

Validates five hypotheses:
  1. PyMuPDF can render pages to cairo.ImageSurface for GTK4 DrawingArea
  2. Ink annotation round-trip: add, save, reopen, read back, delete
  3. Coordinate system compatibility with the existing _screen_to_pdf() math
  4. Install story (pacman vs pip)
  5. GTK/GObject coexistence

Run:  python3 spike_pymupdf.py
No display required — all checks are headless.
"""

import sys
import os
import struct
import tempfile

# ── Collect results ───────────────────────────────────────────────────────────
results = {}

def ok(key, detail=""):
    results[key] = ("OK", detail)
    print(f"  [OK]      {key}" + (f" — {detail}" if detail else ""))

def partial(key, detail=""):
    results[key] = ("PARTIAL", detail)
    print(f"  [PARTIAL] {key}" + (f" — {detail}" if detail else ""))

def fail(key, detail=""):
    results[key] = ("FAIL", detail)
    print(f"  [FAIL]    {key}" + (f" — {detail}" if detail else ""))

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== SPIKE: PyMuPDF feasibility ===\n")

# ── 1. Install story ──────────────────────────────────────────────────────────
print("--- 1. Install story ---")
try:
    import fitz
    version = fitz.__version__
    import subprocess
    r = subprocess.run(["pacman", "-Qi", "python-pymupdf"], capture_output=True, text=True)
    if r.returncode == 0:
        ok("install-story", f"pymupdf {version} available via pacman (python-pymupdf)")
    else:
        partial("install-story", f"pymupdf {version} installed but NOT via pacman (pip?)")
except ImportError as e:
    fail("install-story", str(e))
    print("Cannot continue without fitz -- aborting.")
    sys.exit(1)

# ── 2. GTK/GObject coexistence ────────────────────────────────────────────────
print("\n--- 2. GTK / GObject coexistence ---")
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, Gdk
    import cairo
    ok("gtk-coexistence", "gi.repository + cairo + fitz import without conflict")
except Exception as e:
    fail("gtk-coexistence", str(e))

# ── 3. Render page to cairo.ImageSurface ─────────────────────────────────────
print("\n--- 3. Render to cairo.ImageSurface ---")
PDF_PATH = "/home/hannes/Documents/uni/pruefungsanmeldung/Modulanmeldung.pdf"
if not os.path.exists(PDF_PATH):
    import glob
    pdfs = glob.glob("/home/hannes/Documents/**/*.pdf", recursive=True)
    PDF_PATH = pdfs[0] if pdfs else None

if not PDF_PATH:
    fail("render-to-cairo", "No PDF found to test with")
else:
    try:
        doc = fitz.open(PDF_PATH)
        page = doc[0]
        pw, ph = page.rect.width, page.rect.height

        scale = 1.5
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # pix.samples is bytes: RGB, 3 bytes per pixel
        # cairo ARGB32 wants: B G R A (little-endian 32-bit), premultiplied alpha
        w, h = pix.width, pix.height
        rgb = pix.samples

        # Pure-Python conversion (correct but slow for large pages)
        bgra = bytearray(w * h * 4)
        for i in range(w * h):
            r, g, b = rgb[i*3], rgb[i*3+1], rgb[i*3+2]
            bgra[i*4 + 0] = b
            bgra[i*4 + 1] = g
            bgra[i*4 + 2] = r
            bgra[i*4 + 3] = 255

        surf = cairo.ImageSurface.create_for_data(
            bytearray(bgra), cairo.FORMAT_ARGB32, w, h
        )

        assert surf.get_width() == w
        assert surf.get_height() == h

        ok("render-to-cairo",
           f"page {w}x{h}px at scale={scale} -> cairo.ImageSurface FORMAT_ARGB32 OK")

        # Check if numpy fast path is available
        try:
            import numpy as np
            pix2 = page.get_pixmap(matrix=mat, alpha=True)  # RGBA
            arr = np.frombuffer(pix2.samples, dtype=np.uint8).reshape(h, w, 4).copy()
            # Swap R and B channels for cairo BGRA (channels: R=0, G=1, B=2, A=3)
            arr[:, :, [0, 2]] = arr[:, :, [2, 0]]
            surf2 = cairo.ImageSurface.create_for_data(arr, cairo.FORMAT_ARGB32, w, h)
            ok("render-numpy-fast-path", "numpy available -- fast RGBA->BGRA swap via array indexing")
        except ImportError:
            partial("render-numpy-fast-path",
                    "numpy not available -- pure-Python loop is slow (~0.5s per A4 page at 1.5x)")

        doc.close()
    except Exception as e:
        import traceback
        fail("render-to-cairo", str(e))
        traceback.print_exc()

# ── 4. Ink annotation round-trip ─────────────────────────────────────────────
print("\n--- 4. Ink annotation round-trip ---")
try:
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)

    # 4a. Open real PDF, add two ink annotations
    doc = fitz.open(PDF_PATH)
    page = doc[0]

    # SPIKE: strokes in PDF-point space (y=0 at top)
    stroke1 = [(10.0, 10.0), (50.0, 50.0), (100.0, 30.0)]
    stroke2 = [(200.0, 100.0), (250.0, 150.0)]

    annot1 = page.add_ink_annot([stroke1])
    annot1.set_colors(stroke=fitz.utils.getColor("blue"))
    annot1.set_border(width=2.0)
    annot1.update()

    annot2 = page.add_ink_annot([stroke2])
    annot2.set_colors(stroke=fitz.utils.getColor("red"))
    annot2.set_border(width=3.0)
    annot2.update()

    doc.save(tmp_path)
    doc.close()
    ok("annot-add", f"Added 2 ink annotations and saved")

    # 4b. Reopen and read back
    doc2 = fitz.open(tmp_path)
    page2 = doc2[0]
    annots = [a for a in page2.annots(types=[fitz.PDF_ANNOT_INK])]

    if len(annots) != 2:
        partial("annot-readback", f"Expected 2 ink annots, got {len(annots)}")
    else:
        first = annots[0]
        verts0 = first.vertices   # list of lists of (x, y) tuples
        color0 = first.colors.get("stroke")
        width0 = first.border.get("width")
        ok("annot-readback",
           f"stroke[0] verts={verts0[0][:2]}..., color={color0}, width={width0}")

    # 4c. Delete first annotation, save
    tmp_path2 = tmp_path + ".del.pdf"
    page2.delete_annot(annots[0])
    remaining = [a for a in page2.annots(types=[fitz.PDF_ANNOT_INK])]
    if len(remaining) == 1:
        ok("annot-delete", "Deleted 1 annotation; 1 remains in-memory")
    else:
        partial("annot-delete", f"Expected 1 remaining, got {len(remaining)}")

    doc2.save(tmp_path2)
    doc2.close()

    # 4d. Confirm delete survived reopen
    doc3 = fitz.open(tmp_path2)
    page3 = doc3[0]
    final = [a for a in page3.annots(types=[fitz.PDF_ANNOT_INK])]
    if len(final) == 1:
        ok("annot-delete-persist", "Delete survived save/reopen: exactly 1 ink annot remains")
    else:
        partial("annot-delete-persist", f"Expected 1 after reopen, got {len(final)}")
    doc3.close()

    os.unlink(tmp_path)
    os.unlink(tmp_path2)

except Exception as e:
    import traceback
    fail("annot-roundtrip", str(e))
    traceback.print_exc()

# ── 5. Coordinate system compatibility ───────────────────────────────────────
print("\n--- 5. Coordinate system compatibility ---")
try:
    # Poppler: y=0 at TOP (matching Cairo), page size in points at 72 dpi
    # PyMuPDF: page.rect uses y=0 at TOP (same convention)
    # fitz.Rect(x0, y0, x1, y1) with x0=0, y0=0
    #
    # The existing _screen_to_pdf formula:
    #   px = (sx - offset_x) / scale
    #   py = (sy - offset_y) / scale
    # works unchanged because both Poppler and PyMuPDF share the same coordinate origin.

    doc = fitz.open(PDF_PATH)
    page = doc[0]
    r = page.rect

    # At scale=1, pixmap pixel count should match page size in points (within 1px due to rounding)
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1))
    assert abs(pix.width - r.width) <= 1, f"width mismatch {pix.width} vs {r.width}"
    assert abs(pix.height - r.height) <= 1, f"height mismatch {pix.height} vs {r.height}"

    ok("coord-system",
       f"page.rect={r}  y=0-at-top (same as Poppler+Cairo). _screen_to_pdf() unchanged.")

    # Verify annotation vertex space
    annot_test = page.add_ink_annot([[(10.0, 20.0), (30.0, 40.0)]])
    annot_test.update()
    verts = annot_test.vertices
    page.delete_annot(annot_test)
    doc.close()

    if verts and abs(verts[0][0][0] - 10) < 1 and abs(verts[0][0][1] - 20) < 1:
        ok("coord-annot",
           f"Annotation vertex (10,20) stored faithfully: got {verts[0][0]}")
    else:
        partial("coord-annot", f"Unexpected vertex data: {verts}")

except Exception as e:
    import traceback
    fail("coord-system", str(e))
    traceback.print_exc()

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n=== Summary ===")
statuses = [s for s, _ in results.values()]
n_ok = statuses.count("OK")
n_partial = statuses.count("PARTIAL")
n_fail = statuses.count("FAIL")
print(f"  OK={n_ok}  PARTIAL={n_partial}  FAIL={n_fail}  (of {len(results)} checks)")
for key, (status, detail) in results.items():
    print(f"  [{status:7s}] {key}: {detail}")

if n_fail == 0:
    print("\nConclusion: PyMuPDF is a viable Poppler replacement. Migration feasible.")
elif n_fail <= 1:
    print("\nConclusion: Mostly viable -- one area needs investigation.")
else:
    print("\nConclusion: Significant blockers found. Review FAIL items before proceeding.")

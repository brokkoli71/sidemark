// The open document: pages, outline, per-page ink, and the merge import.
//
// Sidemark shows ONE page at a time and flips between them — it is not a
// continuous scroll — so this exposes a page index and a renderer for it rather
// than a strip of laid-out pages.

import * as pdfjs from "../vendor/pdf.min.mjs";
import { PDFDocument } from "../vendor/pdf-lib.esm.js";
import { NotesModel } from "./notes-model.js";
import { readInk, readImages } from "./inkpdf.js";

// How many lazily fetched pages stay resident (LIVE mode). Each is a whole
// pdf.js document held in the worker, so this is a memory ceiling, not a
// speed knob: big enough that paging back and forth costs no re-fetch, small
// enough that a 400-page deck cannot accumulate into a killed tab.
const LAZY_PAGE_CACHE = 8;
import { makeImage } from "./images.js";

// The single-file build has no URLs to fetch from, so it hands us a Worker it
// built from an inlined blob. Served normally, the worker is just a file next
// to us.
if (globalThis.__SIDEMARK_PDF_WORKER__) {
  pdfjs.GlobalWorkerOptions.workerPort = globalThis.__SIDEMARK_PDF_WORKER__;
} else {
  pdfjs.GlobalWorkerOptions.workerSrc =
    new URL("../vendor/pdf.worker.min.mjs", import.meta.url).href;
}

export const A4 = [595.0, 842.0];

/** One open document. `ink` is per page: index → array of strokes. */
export class Doc {
  constructor(pdf, bytes, name) {
    this.pdf = pdf;              // a pdf.js PDFDocumentProxy
    this.bytes = bytes;          // the source bytes, for a re-merge
    this.name = name;
    this.pageCount = pdf ? pdf.numPages : 1;
    this.ink = new Map();        // page index → strokes[]
    // The `.md` sidecar's model. It belongs to the document, not to the editor:
    // the panel is a view of one page of it.
    this.notes = new NotesModel();
    // the `![[name.pdf]]` embed line names the PDF the notes belong to — only
    // when there IS one, so an untitled blank does not claim a file
    this.notes.pdfName = /\.pdf$/i.test(name || "") ? name : null;
    this.outline = [];           // [{title, page, level}]
    this._pageCache = new Map(); // page index → pdf.js PDFPageProxy
    this._sizeCache = new Map();
    this._thumbs = new Map();
  }

  /** Adopt any ink and images WE wrote, and take them back OUT of the bytes
   * before pdf.js sees them.
   *
   * Without the strip they are painted twice — once by pdf.js as annotation
   * appearances and once by us as objects — and the copy pdf.js drew cannot
   * be erased, lassoed or undone, which is the desktop's image-layer trap in
   * a different coat. Shared with the lazy per-page path (LIVE mode) so a
   * page fetched on its own cannot diverge from one that arrived with the
   * document. */
  static async _adopt(bytes) {
    let ink = new Map();
    try {
      const lib = await PDFDocument.load(bytes, { ignoreEncryption: true });
      ink = readInk(lib);
      // Images are adopted the same way and into the SAME per-page list, so
      // everything downstream — selecting, moving, undoing, saving — sees one
      // kind of thing. They go in FIRST because ink is painted over them.
      const images = await readImages(lib, makeImage);
      for (const [page, objs] of images) {
        ink.set(page, objs.concat(ink.get(page) || []));
      }
      if (ink.size) bytes = await lib.save();
    } catch {
      // an unreadable document is pdf.js's problem to report, not ours; a file
      // we cannot adopt ink from still opens
    }
    const pdf = await pdfjs.getDocument({
      data: bytes.slice(0),      // pdf.js transfers the buffer; keep ours intact
      isEvalSupported: false,
    }).promise;
    return { pdf, ink, bytes };
  }

  static async open(bytes, name) {
    const got = await Doc._adopt(bytes);
    const doc = new Doc(got.pdf, got.bytes, name);
    doc.ink = got.ink;
    await doc._readOutline();
    return doc;
  }

  /** One page fetched on its own (LIVE mode). Returns the pdf.js page and the
   * ink adopted off it, which the caller files under its REAL index — the
   * fetched PDF is one page long, so everything in it says page 0.
   *
   * The DOCUMENT rides along so it can be destroyed later. Each fetched page
   * is a whole PDFDocumentProxy, and pdf.js holds its data in the WORKER —
   * which is why a leak here does not show up in the main thread's heap at
   * all, and why a phone browsing a long deck could run out of memory with
   * every JS measurement looking healthy. */
  static async openLoosePage(bytes) {
    const got = await Doc._adopt(bytes);
    return { page: await got.pdf.getPage(1), ink: got.ink.get(0) || [],
             pdf: got.pdf };
  }

  /** An empty document — the blank A4 sheet `blank_pdf_file()` makes. */
  static async blank() {
    const pdf = await PDFDocument.create();
    pdf.addPage(A4);
    const bytes = await pdf.save();
    return Doc.open(bytes, "Untitled");
  }

  strokesFor(page) {
    if (!this.ink.has(page)) this.ink.set(page, []);
    return this.ink.get(page);
  }

  async _readOutline() {
    let raw;
    try { raw = await this.pdf.getOutline(); } catch { raw = null; }
    if (!raw || !raw.length) { this.outline = []; return; }
    const out = [];
    const walk = async (items, level) => {
      for (const item of items) {
        let page = null;
        try {
          const dest = typeof item.dest === "string"
            ? await this.pdf.getDestination(item.dest) : item.dest;
          if (dest && dest[0]) {
            page = await this.pdf.getPageIndex(dest[0]);
          }
        } catch { /* a broken destination is not worth losing the row over */ }
        out.push({ title: item.title || "Untitled", page: page ?? 0, level });
        if (item.items && item.items.length) await walk(item.items, level + 1);
      }
    };
    await walk(raw, 0);
    this.outline = out;
  }

  async page(index) {
    this._lastPage = index;
    if (this._pageCache.has(index)) return this._pageCache.get(index);
    // LIVE mode fetches pages one at a time (see attachLazyPages): `this.pdf`
    // then holds ONE page, not the document, so asking it for page index+1
    // would be a different page or none at all.
    let p;
    if (this._lazy && index !== this._lazy.have) {
      const got = await this._lazy.fetch(index);
      p = got.page;
      this._lazy.docs.set(index, got.pdf);
      this._evictLazyPages();
      // Ink adopted off a page fetched alone is filed under its REAL index.
      // Tested for EMPTY, not for absence: `strokesFor` creates the key the
      // moment anything asks for the page's strokes, and the renderer does
      // that before this fetch resolves — so a `has()` guard here silently
      // dropped every lazily fetched page's ink. Filled in place, because
      // whoever created the key is already holding that array.
      const cur = this.ink.get(index);
      if (!cur) this.ink.set(index, got.ink);
      else if (!cur.length) for (const o of got.ink) cur.push(o);
    } else {
      p = await this.pdf.getPage((this._lazy ? 0 : index) + 1);
    }
    this._pageCache.set(index, p);
    return p;
  }

  /** Turn this into a lazily-paged document (LIVE mode only).
   *
   * The phone is handed the page it is looking at and nothing else — a first
   * paint costs one page rather than a whole lecture — and pages arrive as it
   * reaches them. Everything downstream (rendering, page sizes, thumbnails)
   * already goes through `page()`, so this is the only place that has to know.
   *
   * `count` is the real page count, which the document itself can no longer
   * say: it is a one-page PDF.
   */
  attachLazyPages(count, fetch) {
    this._lazy = { have: this.pageIndexLoaded ?? 0, fetch, docs: new Map() };
    this.pageCount = count;
  }

  /** Warm the page we are about to need, WITHOUT moving "here".
   *
   * A page turn in LIVE mode costs a round trip to the desktop before a single
   * pixel can be rasterised, and that round trip is the "split second" — the
   * rendering is fast once the bytes are local. Reading is overwhelmingly
   * sequential, so the next page is worth having before it is asked for.
   *
   * `_lastPage` is restored immediately, and that matters: it is what
   * `_evictLazyPages` treats as where you are, so a prefetch that moved it
   * would evict the neighbours of a page nobody is looking at. `page()` sets
   * it synchronously at the top, before its first await, so putting it back
   * right after the call is enough.
   *
   * A no-op outside live mode: there the whole document is already here. */
  prefetchPage(index) {
    if (!this._lazy) return;
    if (!(index >= 0 && index < this.pageCount)) return;
    if (this._pageCache.has(index)) return;
    const here = this._lastPage;
    const p = this.page(index);
    this._lastPage = here;
    // Failure is not interesting — this page was never asked for, and the
    // real request will report its own error if it ever comes.
    if (p && typeof p.catch === "function") p.catch(() => {});
  }

  /** Keep only a window of fetched pages alive.
   *
   * Every lazily fetched page is its own pdf.js DOCUMENT, and pdf.js keeps
   * the bytes and the parsed objects in its worker. Holding all of them means
   * a phone that pages through a lecture accumulates the whole deck a page at
   * a time — invisible in `performance.memory`, since none of it is on the
   * main thread, and on a phone that ends as a killed tab rather than an
   * error anyone can read.
   *
   * Evicts the FURTHEST page from where you are, not the oldest: paging back
   * and forth around one spot should keep its neighbours, which is what
   * paging actually looks like. */
  _evictLazyPages(keep = LAZY_PAGE_CACHE) {
    if (!this._lazy || this._lazy.docs.size <= keep) return;
    const here = this._lastPage ?? this._lazy.have;
    const far = [...this._lazy.docs.keys()]
      .sort((a, b) => Math.abs(b - here) - Math.abs(a - here));
    for (const idx of far.slice(0, this._lazy.docs.size - keep)) {
      const pdf = this._lazy.docs.get(idx);
      this._lazy.docs.delete(idx);
      this._pageCache.delete(idx);
      this._sizeCache.delete(idx);
      // the ink stays: it is OURS now, filed under the real page index, and
      // re-fetching the page would adopt it a second time
      try { pdf.destroy(); } catch { /* already gone */ }
    }
  }

  /** The page's size in PDF points — which ARE the document units the ink
   * pipeline's lengths are calibrated in, so nothing needs converting. */
  async pageSize(index) {
    if (this._sizeCache.has(index)) return this._sizeCache.get(index);
    const p = await this.page(index);
    const vp = p.getViewport({ scale: 1 });
    const size = [vp.width, vp.height];
    this._sizeCache.set(index, size);
    return size;
  }

  /** Render a page to a canvas at `scale` device pixels per document unit. */
  async render(index, scale, canvas) {
    const p = await this.page(index);
    const vp = p.getViewport({ scale });
    canvas.width = Math.max(1, Math.round(vp.width));
    canvas.height = Math.max(1, Math.round(vp.height));
    const ctx = canvas.getContext("2d", { alpha: false });
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    // ENABLE, not pdf.js's default ENABLE_FORMS: that one leaves form fields
    // to an HTML layer we do not build, so a filled-in form's values vanished.
    // ENABLE paints them from their appearance streams like any annotation.
    // Our own ink is stripped out of the bytes at open (`_adopt`), so this
    // cannot paint it a second time.
    await p.render({ canvasContext: ctx, viewport: vp,
                     annotationMode: pdfjs.AnnotationMode.ENABLE }).promise;
    return canvas;
  }

  async thumbnail(index, width = 120) {
    if (this._thumbs.has(index)) return this._thumbs.get(index);
    const [w] = await this.pageSize(index);
    const canvas = document.createElement("canvas");
    // Rendered above DEVICE pixels and laid out at CSS width, or every
    // thumbnail is soft on a HiDPI screen. A thumbnail is small enough that
    // oversampling it costs almost nothing, and it is nearly all fine detail,
    // which is exactly what benefits.
    const dpr = Math.min(window.devicePixelRatio || 1, 2) * 2;
    await this.render(index, (width * dpr) / w, canvas);
    canvas.style.width = "100%";
    this._thumbs.set(index, canvas);
    return canvas;
  }

  dropThumbnails() { this._thumbs.clear(); }
}

export { mergeDocuments, insertDocuments } from "./merge.js";

// The drawing surface: one press router, the live stroke, and the committed
// ink layer. This is the prototype's stand-in for PDFCanvas — the page is a
// blank sheet rather than a rendered PDF, but everything about how a press
// becomes ink is the real design.

import {
  adaptiveSpacing, finishInkStroke, liveInkStroke, hoverLeadIn, eraseRadius,
  HOVER_TRAIL_MS,
} from "./ink.js";
import { drawInkStroke, strokeHit, rgbCss } from "./draw.js";
import { BTN_FINGER, BTN_LEFT, buttonForEvent, chordId } from "./bindings.js";
import { recognizeShape, snapGridDivider, respaceDividers, polylineIsClosed,
         ellipseResizeState, resizedEllipse, SNAP_LABELS } from "./shapes.js";
import { pageWords, nearestWord, wordsBetween, wordsInRect,
         selectionText, selectionRects } from "./textlayer.js";
import { copySelection, takeCopy, hasCopy, copyExtent } from "./clipboard.js";
import { parsePageMarks, coveredBy, addAnchor, moveAnchor, moveCallout, calloutBox,
         drawAnchor, ANCHOR_R } from "./anchors.js";
import { isImage, imageHit, imageQuad, imageFrame, drawImageObject,
         makeImage, pasteScale } from "./images.js";
import {
  pointInPolygon, lassoHandlePoints, lassoHandleAnchor, lassoScaleFactors,
  lassoHandleCursor, lassoChipCentre, lassoChipHit, lassoDeleteCentre,
  lassoDeleteHit, drawLassoChip, drawLassoDelete, mergeSelection, selectionBbox,
  scalePoint, rotatePoint, rotateKnobCentre, rotateKnobHit, drawRotateKnob,
  LASSO_PAD, HANDLE_HIT, ROTATE_SNAP_DEG, DUPLICATE_OFFSET,
  shapeVertices, moveShapeVertex, weldedVertices, vertexSnapRadius, snapPoint,
  curveSnapShapes, drawShapeVertices, VERTEX_HIT_PX,
} from "./lasso.js";

export const PAGE_W = 595.0, PAGE_H = 842.0;   // A4 in document units, the size
                                               // blank_pdf_file() makes

// Tools this prototype implements. The others stay in the table and in the bar
// — removing them would change the binding model, which is not ours to change —
// but a press that resolves to one of them does nothing here.
// The page is rasterised at EXACTLY the resolution it is shown at, and blitted
// 1:1. Supersampling was tried and measured and is worse: rendering at 1x, 2x
// and 3x and scaling down scored 5.13, 5.12 and 5.01 on edge contrast through a
// band of text — the rasteriser antialiases ONTO THE PIXEL GRID, and a smooth
// downscale can only blur what it had already placed exactly.
//
// What actually costs sharpness is sub-pixel misalignment: the same 1x render
// blitted half a pixel off scored 4.54, an 11% loss. So everything below is
// about landing on whole device pixels, not about making more of them.
export const MAX_RENDER_SIDE = 8192;

/** The zoom range a view may hold. The FLOOR is what stops a container with
 * no size producing a negative scale — `(0 - 48) / 595` is a perfectly good
 * number and a completely broken view. */
export const MIN_ZOOM = 0.05, MAX_ZOOM = 16;

/** A first, blurry pass while the real one rasterises.
 *
 * A page arrives as a white rectangle otherwise — pdf.js takes long enough at
 * full device resolution to be seen, and on a phone a page turn is a white
 * flash. A bitmap this size comes back in a fraction of the time and is
 * replaced the moment the sharp one lands, so the page FADES IN rather than
 * appearing. Only worth doing when the real bitmap is meaningfully bigger;
 * below that the preview is the same work twice. */
export const PREVIEW_SIDE = 700;

export const STRAIGHT_HOLD_MS = 500;   // hold still this long mid-stroke to snap
// The same hold time, so there is only one to learn. What separates the two is
// the pen LIFT, not the shape: hold WITHOUT lifting and the stroke snaps; hold
// on a FINISHED stroke and it becomes the lasso. A stroke available to convert
// was therefore never snapped.
export const CIRCLE_LASSO_HOLD_MS = 500;

/** HOW STILL "STILL" HAS TO BE — for both holds, because it is one fact about a
 * hand, not two facts about two gestures.
 *
 * A hand holding a pen against glass for half a second does not stop moving; it
 * drifts. Reported: both dwells were "too precisely not moving" to be usable.
 * Here the circle-to-lasso hold had NO tolerance at all — any motion event
 * cancelled it — and the shape dwell re-armed its timer on every event, so a
 * shaking hand reset the clock forever and it never fired.
 *
 * The two measure from different places, and that difference is the whole
 * design. Circle-to-lasso measures from where the press LANDED, so a slow drag
 * across the page can never become a selection. The shape dwell measures from
 * wherever the pen was last moving, because you draw a shape and then stop, and
 * the hold has to start from where you stopped. */
export const HOLD_SLOP_PX = 16.0;

/** Tools that only change what you are LOOKING at. They are the exemption from
 * "a press outside a selection dismisses it": moving the view is something you
 * do while arranging a selection, and losing it because you panned would be a
 * bug of its own. Everything else marks the page, and the press that dismisses
 * must not also mark it. */
export const VIEW_TOOLS = new Set(["pan", "zoom"]);
export const pressDismissesSelection = (tool) => !VIEW_TOOLS.has(tool);

export const IMPLEMENTED_TOOLS = new Set(["pen", "highlighter", "eraser", "pan",
                                          "zoom", "lasso", "text", "anchor"]);

/** How many fingers are on the glass — and whether THIS hand ever had two.
 *
 * TWO FINGERS ARE A FACT ABOUT THE HAND, NEVER ABOUT THE GESTURE (rows 148 and
 * 150). The count must come off the raw pointer stream, not off a recognised
 * pinch: a gesture recogniser only fires once it recognises, by which time the
 * press router may already have claimed the first finger.
 *
 * Two invariants: `multi` latches on the second touchdown and clears only when
 * the LAST finger lifts (a pinch ends while a finger is still down, and the
 * survivor then arrives as a brand-new press); and a second finger ABANDONS
 * whatever the first started rather than committing it. */
export class TouchLatch {
  constructor() {
    this.points = new Map();   // pointerId → [x, y]
    this.multi = false;
  }
  down(id, x, y) {
    this.points.set(id, [x, y]);
    if (this.points.size >= 2) this.multi = true;
    return this.points.size;
  }
  move(id, x, y) {
    if (this.points.has(id)) this.points.set(id, [x, y]);
  }
  up(id) {
    this.points.delete(id);
    if (this.points.size === 0) this.multi = false;   // only the LAST lift
    return this.points.size;
  }
  get count() { return this.points.size; }
  /** Centroid and mean spread — the pinch's own arithmetic, kept beside the
   * count so no gesture recogniser is needed to drive zoom and pan. */
  metrics() {
    const pts = [...this.points.values()];
    if (!pts.length) return null;
    const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length;
    const cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const spread = pts.length < 2 ? 0
      : pts.reduce((s, p) => s + Math.hypot(p[0] - cx, p[1] - cy), 0) / pts.length;
    return { cx, cy, spread };
  }
}

export class Surface {
  constructor(canvas, pen, bindings, opts = {}) {
    this.el = canvas;
    this.ctx = canvas.getContext("2d");
    this.pen = pen;             // the shared pen settings object
    this.bindings = bindings;
    this.docMode = "pdf";       // routing NEVER reads bindings.mode
    this.onChange = opts.onChange || (() => {});
    /** LIVE MODE only: raw stroke samples on their way to a desktop Sidemark.
     * Called with a plain object per batch; see app.js's LIVE. The points are
     * DOCUMENT coordinates, which is exactly what lets the phone hold a zoom
     * of its own — a screen coordinate would only mean something against the
     * desktop's view. The desktop runs its own pipeline over these, so what
     * it commits is what THIS side commits: the same tuned code over the same
     * raw samples, which is what the conformance vectors are for. */
    this.onInkStream = opts.onInkStream || null;
    this._streamSeq = 0;
    // The gesture currently mirrored to the desktop, tracked HERE rather than
    // on the gesture object — a gesture can be abandoned without ever
    // reaching _onUp's branches (a circle-to-lasso conversion and a second
    // finger both null `active` where they stand), and a stream left open
    // wedges the desktop's one-finger rule for good.
    this._streamOpen = null;
    this._streamTool = "pen";
    /** LIVE mode: the desktop's stroke while their pen is still down. */
    this.remoteLive = null;
    /** Called with each committed freehand stroke. Used by the write-and-
     * advance aid, which needs to know WHERE a stroke finished, not merely
     * that the document changed. */
    this.onStrokeDone = opts.onStrokeDone || null;
    this.onPageChange = opts.onPageChange || (() => {});
    this.onNotesRestored = opts.onNotesRestored || (() => {});
    // called on every repaint, so a mirror can follow the live stroke
    this.onLiveDraw = opts.onLiveDraw || (() => {});
    this.onNotesChanged = opts.onNotesChanged || (() => {});
    // a press found the tracked modifiers stale: the window owns them and the
    // stripes read from them, so both have to hear about the correction
    this.onHeldModsCorrected = opts.onHeldModsCorrected || (() => {});

    // The document, and which page is in front. Sidemark shows ONE page at a
    // time and flips between them; it is not a continuous scroll.
    this.doc = null;
    this.pageIndex = 0;
    this.pageW = PAGE_W;
    this.pageH = PAGE_H;
    this._pageCanvas = document.createElement("canvas");
    this._pageKey = null;
    this._renderToken = 0;

    this.undoStack = [];
    this.redoStack = [];

    // The lasso selection. `selected` is the strokes; `loop` is the path you
    // drew, in DOCUMENT units, and is the grab region while it exists — a
    // selection wears the loop it was drawn with, not a box. The chip switches
    // to the 8-handle box, which is the only way to scale.
    this.selected = [];
    this.selectionLoop = null;
    this.selectionBoxed = false;
    // where a dragged control point is currently snapped, for the halo
    this._snapAtPoint = null;

    // view: document units → CSS px is `zoom`, origin at (offX, offY)
    this.zoom = 1.0;
    this.offX = 0.0;
    this.offY = 0.0;
    this._fitPending = true;
    // "is this view still the one `fit` produced?" — what decides whether a
    // container resize re-fits the page or preserves the zoom you chose
    this._fitted = true;
    this._hostSize = null;

    // the press in flight
    this.active = null;
    this.hoverTrail = [];       // [x, y, t_ms] in DOCUMENT units
    this.latch = new TouchLatch();
    this._heldMods = { ctrl: false, shift: false, alt: false };
    this._pinch = null;
    // the extended dwell: hold still mid-stroke and the freehand line is
    // replaced by a clean primitive. `_straightMode` says this stroke has
    // already been settled — it is exempt from live smoothing exactly as it is
    // at commit, because denoising a recognised rectangle rounds the corners
    // the dwell just gave it.
    this._snapTimer = null;
    this._circleTimer = null;   // press-and-hold on the loop you just drew
    this._straightMode = false;
    this._snapKind = null;
    this._snapLabel = null;
    this._snapAt = null;
    this._frame = null;
    this._layer = null;         // cached committed ink
    this._layerKey = null;
    // the caret's word selection on this page
    this.textSelection = [];
    this._words = new Map();       // page → words, extracted on demand
    this.textStyle = "reading";    // "reading" | "rect"
    // rects to highlight, asked for per page — the search owns the results, the
    // canvas only paints them
    this.searchRects = null;

    this._install();
    this._installResizeWatch();
  }

  // ── the document ───────────────────────────────────────────────────────────

  /** Strokes are stored PER PAGE on the document, so paging away and back
   * finds the ink where you left it. */
  get strokes() {
    return this.doc ? this.doc.strokesFor(this.pageIndex) : (this._loose ||= []);
  }

  async setDoc(doc, page = 0) {
    this.doc = doc;
    this._words.clear();
    this.textSelection = [];
    this.undoStack.length = 0;
    this.redoStack.length = 0;
    await this.setPage(page, { fit: true });
    this.onChange();
  }

  async setPage(index, { fit = false } = {}) {
    if (!this.doc) return;
    index = Math.max(0, Math.min(this.doc.pageCount - 1, index));
    if (index !== this.pageIndex) {
      // A selection belongs to the page it was made on. Carried across, its
      // frame draws over ink that is no longer there and its verbs act on
      // strokes nobody can see — a delete that removes something invisible.
      this.clearSelection();
      this._clearSnap();
      this.textSelection = [];
    }
    this.pageIndex = index;
    [this.pageW, this.pageH] = await this.doc.pageSize(index);
    if (fit) this.fit();
    this._pageKey = null;
    this.invalidateLayer();
    await this._renderPage();
    this.onPageChange(index);
    this.requestDraw();
    // The page after this one, before it is asked for. A no-op unless the
    // document is fetching pages one at a time (LIVE mode), where the round
    // trip — not the rasterising — is what makes a page turn wait. Forward
    // first because that is the direction reading goes.
    if (this.doc.prefetchPage) {
      this.doc.prefetchPage(index + 1);
      this.doc.prefetchPage(index - 1);
    }
  }

  /** Only RELATIVE navigation is a "flip" — and only relative navigation SKIPS
   * a hidden page. A thumbnail click, a bookmark or an outline row still opens
   * one: that is what keeps it reachable and editable, and the dimmed row in
   * the strip is the only way to select it again and bring it back. */
  flipPage(delta) {
    if (!this.doc) return;
    const next = this.nextPageFor(this.pageIndex, delta);
    if (next === null) return;
    this.setPage(next, { fit: false });
  }

  /** The page `delta` steps away, skipping hidden ones. Resolved in ONE place —
   * resolving it in both the buttons and the scroll-past-edge is how paging
   * from 1 over a hidden 2-4 lands on 7 instead of 5. */
  nextPageFor(from, delta) {
    if (!this.doc) return null;
    const step = delta > 0 ? 1 : -1;
    let page = from;
    for (let n = 0; n < Math.abs(delta); n++) {
      let next = page + step;
      while (next >= 0 && next < this.doc.pageCount
             && this.doc.notes.isHidden(next)) next += step;
      if (next < 0 || next >= this.doc.pageCount) return page === from ? null : page;
      page = next;
    }
    return page === from ? null : page;
  }

  /** Device pixels per document unit to rasterise at: exactly what will be
   * shown, so the blit is 1:1 and nothing is resampled. */
  _renderScale() {
    const dpr = window.devicePixelRatio || 1;
    let scale = this.zoom * dpr;
    // …except past the canvas size limit, where an over-large bitmap comes back
    // BLANK rather than throwing. A soft page beats no page.
    const side = Math.max(this.pageW, this.pageH) * scale;
    if (side > MAX_RENDER_SIDE) scale *= MAX_RENDER_SIDE / side;
    return scale;
  }

  /** Put the page's origin on a whole DEVICE pixel.
   *
   * A fractional origin makes the blit land between pixels, and the filter then
   * smears an image that was rasterised perfectly — measured at an 11% loss of
   * edge contrast, which is most of "not quite sharp". The cost is that a pan
   * moves in device-pixel steps, which at any real dpr is invisible. */
  _snapView() {
    const dpr = window.devicePixelRatio || 1;
    this.offX = Math.round(this.offX * dpr) / dpr;
    this.offY = Math.round(this.offY * dpr) / dpr;
  }

  /** Rasterise one page at one scale, into a canvas of its own.
   *
   * A PRIVATE canvas per render, never the shared one: `doc.render` sets the
   * size and paints, so two renders sharing a buffer means a slower earlier
   * one can finish last and overwrite the bitmap the key says is on screen.
   * Returns null if it failed, lost, or came back degenerate — a page that
   * will not render must not wedge, and must not be cached either. */
  async _renderAt(page, scale, token) {
    if (!(scale > 0)) return null;
    const target = document.createElement("canvas");
    try {
      await this.doc.render(page, scale, target);
    } catch {
      return null;
    }
    if (token !== this._renderToken) return null;
    // `doc.render` clamps a nonsense viewport to 1px rather than throwing, and
    // `draw` refuses to blit that — so caching it under a key is how a page
    // stays blank until something else changes the scale.
    if (target.width <= 1 || target.height <= 1) return null;
    return target;
  }

  /** True while the bitmap on screen belongs to `page`, at any scale. */
  _showingPage(page) {
    return !!this._pageKey && this._pageKey.startsWith(`${page}@`);
  }

  async _renderPage() {
    if (!this.doc) return;
    const scale = this._renderScale();
    if (!(scale > 0)) return;                  // no view to render into yet
    const page = this.pageIndex;
    const key = `${page}@${scale.toFixed(3)}`;
    if (this._pageKey === key) return;
    const token = ++this._renderToken;

    // A BLURRY PAGE BEATS A WHITE ONE. With nothing on screen for this page,
    // a small bitmap goes up first and the full-scale one replaces it when it
    // lands, so the page sharpens instead of appearing. Skipped when this page
    // is ALREADY showing — there the bitmap in hand is the better placeholder,
    // and dropping to a soft one would be a visible step backwards.
    const preview = Math.min(scale, PREVIEW_SIDE / Math.max(this.pageW, this.pageH));
    if (!this._showingPage(page) && preview < scale * 0.9) {
      const quick = await this._renderAt(page, preview, token);
      if (token !== this._renderToken) return;
      if (quick) {
        this._pageCanvas = quick;
        this._pageKey = `${page}@preview`;
        this.requestDraw();
      }
    }

    const full = await this._renderAt(page, scale, token);
    if (token !== this._renderToken || !full) return;
    this._pageCanvas = full;
    this._pageKey = key;
    this.requestDraw();
  }

  // ── view ───────────────────────────────────────────────────────────────────

  get cssW() { return this.el.clientWidth; }
  get cssH() { return this.el.clientHeight; }

  fit() {
    // A CONTAINER WITH NO SIZE HAS NO FIT, and computing one anyway is how the
    // page came up blank on a phone: `(0 - 48) / 595` is a NEGATIVE zoom, which
    // renders a 1px bitmap that `draw` then refuses to blit — and caches it
    // under a key, so nothing re-rendered until a gesture happened to change
    // the scale. Wait for a box instead; `draw` re-fits as soon as there is one.
    const w = this.cssW, h = this.cssH;
    if (!(w >= 1) || !(h >= 1)) { this._fitPending = true; return; }
    // The margin cannot be bigger than the box it is inset from — a narrow
    // column is a small page, not an inverted one.
    const m = Math.min(24, w / 10, h / 10);
    this.zoom = Math.max(MIN_ZOOM,
                         Math.min((w - m * 2) / this.pageW,
                                  (h - m * 2) / this.pageH));
    this.offX = (this.cssW - this.pageW * this.zoom) / 2;
    this.offY = (this.cssH - this.pageH * this.zoom) / 2;
    this._snapView();
    this.invalidateLayer();
    this._schedulePageRender();
    // this view IS the fit now — until a hand changes it
    this._fitted = true;
  }

  /** THE VIEW FOLLOWS ITS CONTAINER, whatever changed it.
   *
   * A window resize is not the only way this canvas changes size, and it was
   * the only one being watched: hiding the sidebar, dragging the divider, or
   * the frame the tour runs in settling all resize it with no window event at
   * all. The page then kept the scale and position it had for a box that no
   * longer existed — a page fitted to a narrow column stayed that size when
   * the column got the whole window, and one rendered while the canvas was
   * still zero-sized never rendered again.
   *
   * A resize must NOT throw away a zoom the hand chose, so there are two
   * cases: a view still at its fit re-fits, and a zoomed one keeps its scale
   * with whatever was in the middle of the view staying in the middle. */
  _installResizeWatch() {
    const host = this.el.parentElement || this.el;
    if (typeof ResizeObserver !== "function") return;
    this._resizeWatch = new ResizeObserver(() => this._onHostResize(host));
    this._resizeWatch.observe(host);
  }

  _onHostResize(host) {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;                       // laid out to nothing yet
    const prev = this._hostSize;
    if (prev && prev[0] === w && prev[1] === h) return;
    this._hostSize = [w, h];
    if (!prev || this._fitted) {
      // `fit` measures the CANVAS, which is resized from the host during the
      // draw and so still carries the old size at this moment
      this._fitPending = true;
    } else {
      this.offX += (w - prev[0]) / 2;
      this.offY += (h - prev[1]) / 2;
      this._snapView();
      this._schedulePageRender();
    }
    this.invalidateLayer();
    this.requestDraw();
  }

  /** Re-rendering the page is expensive, so it is debounced behind the zoom
   * gesture: the old bitmap is scaled meanwhile, which is what keeps a pinch
   * smooth instead of blocking on pdf.js at every step. */
  _schedulePageRender() {
    clearTimeout(this._renderTimer);
    this._renderTimer = setTimeout(() => this._renderPage(), 120);
  }

  toDoc(x, y) { return [(x - this.offX) / this.zoom, (y - this.offY) / this.zoom]; }
  toView(x, y) { return [x * this.zoom + this.offX, y * this.zoom + this.offY]; }

  zoomAt(factor, cx, cy) {
    const [dx, dy] = this.toDoc(cx, cy);
    this._fitted = false;        // a scale the hand chose; a resize keeps it
    this.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.zoom * factor));
    this.offX = cx - dx * this.zoom;
    this.offY = cy - dy * this.zoom;
    this._snapView();
    this.invalidateLayer();
    this._schedulePageRender();
    this.requestDraw();
  }

  panBy(dx, dy) {
    this._fitted = false;
    this.offX += dx;
    this.offY += dy;
    this._snapView();
    this.invalidateLayer();
    this.requestDraw();
  }

  // ── the press router ───────────────────────────────────────────────────────

  setHeldMods(mods) { this._heldMods = mods; }

  /** Merge the event's own modifier state with the window-tracked held keys —
   * the same rule the Python routers use, because a press can arrive without
   * the modifier mask and a Ctrl+press that reads as unmodified falls into the
   * wrong branch.
   *
   * A POINTER EVENT IS THE TRUTH ABOUT THIS MOMENT, so a real press also
   * CORRECTS the tracked state instead of only adding to it. Tracked keys are
   * inferred from keydown/keyup, and a keyup that never arrives leaves a
   * modifier held for ever — after which `toolFor` resolves a chord nobody is
   * pressing, that chord is usually bound to nothing, and the pen is silently
   * DEAD until you press and release the key again. There is no visible cause:
   * the bar still shows a tool and the page just stops taking ink. Observed
   * with an injected Ctrl+Shift+Z whose modifier keyups were never delivered;
   * the same hole is any keyup the window does not see, and `blur` only covers
   * the ones where focus moves. Correcting here means the next press always
   * fixes it. */
  _chordState(e) {
    if (e && e.type === "pointerdown") {
      const held = this._heldMods;
      if ((held.ctrl && !e.ctrlKey) || (held.shift && !e.shiftKey)
          || (held.alt && !e.altKey)) {
        this._heldMods = { ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey };
        this.onHeldModsCorrected(this._heldMods);
      }
    }
    return {
      ctrl: e.ctrlKey || this._heldMods.ctrl,
      shift: e.shiftKey || this._heldMods.shift,
      alt: e.altKey || this._heldMods.alt,
    };
  }

  /** The tool this press runs. Routing passes its OWN doc mode rather than
   * reading `bindings.mode`, which belongs to the chrome. */
  toolForEvent(e) {
    const { ctrl, shift, alt } = this._chordState(e);
    return this.bindings.toolFor(buttonForEvent(e), ctrl, shift, alt, this.docMode);
  }

  /** `canvas.tool` is DERIVED: the tool of the button being pressed, else what
   * LEFT would do. There is no stored active tool. */
  get tool() {
    if (this.active) return this.active.tool;
    const { ctrl, shift, alt } = this._heldMods;
    return this.bindings.toolFor(1, ctrl, shift, alt, this.docMode);
  }

  _install() {
    const el = this.el;
    el.addEventListener("pointerdown", (e) => this._onDown(e));
    el.addEventListener("pointermove", (e) => this._onMove(e));
    el.addEventListener("pointerup", (e) => this._onUp(e));
    el.addEventListener("pointercancel", (e) => this._onUp(e, true));
    el.addEventListener("pointerleave", (e) => this._onLeave(e));
    // the right button and the pen barrel both raise a context menu otherwise,
    // and both of them are bound buttons here
    el.addEventListener("contextmenu", (e) => e.preventDefault());
    el.addEventListener("wheel", (e) => this._onWheel(e), { passive: false });
    // `touch-action: none` in CSS is what stops the browser's own pan/zoom from
    // stealing the gesture before a pointer event is ever delivered.
  }

  _onWheel(e) {
    e.preventDefault();
    if (e.ctrlKey) {
      // the touchpad pinch, which arrives as ctrl+wheel and carries no pointers
      this.zoomAt(Math.exp(-e.deltaY * 0.01), e.offsetX, e.offsetY);
      return;
    }
    // Scrolling PAST THE EDGE turns the page — the page is the unit of
    // navigation here, so a scroll that has nowhere left to go means "next".
    const pageTop = this.offY;
    const pageBottom = this.offY + this.pageH * this.zoom;
    const atBottom = pageBottom <= this.cssH + 1;
    const atTop = pageTop >= -1;
    if (e.deltaY > 0 && atBottom) { this._flipVisible(1); return; }
    if (e.deltaY < 0 && atTop) { this._flipVisible(-1); return; }
    this.panBy(-e.deltaX, -e.deltaY);
  }

  /** Scroll-past-edge and the page buttons resolve navigation in ONE place, so
   * the two cannot disagree about where "next" lands. */
  _flipVisible(delta) {
    const now = performance.now();
    if (now - (this._lastFlip || 0) < 220) return;   // one page per gesture
    this._lastFlip = now;
    this.flipPage(delta);
  }

  _onDown(e) {
    this.el.setPointerCapture(e.pointerId);
    const [dx, dy] = this.toDoc(e.offsetX, e.offsetY);

    if (e.pointerType === "touch") {
      const n = this.latch.down(e.pointerId, e.offsetX, e.offsetY);
      if (n >= 2) {
        // A SECOND FINGER ABANDONS whatever the first started, rather than
        // committing it — and the guard is here AND at the commit, because a
        // press can be cancelled with the tool still in hand.
        this._abandon();
        this._pinch = { ...this.latch.metrics(), zoom: this.zoom };
        this.requestDraw();
        return;
      }
      // A lone finger falls through and is routed through the table like any
      // other press. That includes the SURVIVOR of a pinch: a pinch ends while
      // a finger is still down, so the one left arrives here as a brand-new
      // press and is re-resolved. ceiling: it therefore restarts its tool
      // rather than continuing the pan (row 151) — worth a third exception AT
      // the router if it ever feels wrong in the hand, never a fork of the
      // table.
    }

    // An anchor or a callout under a plain LEFT press (a pen tip is left) is
    // grabbed whatever tool left holds, like a selection — it is a thing ON
    // the page, and having to change tools to nudge one is the sort of
    // friction the binding table exists to avoid. Left only, as on the
    // desktop: a finger bound to pan must still pan across a callout.
    const mods = this._chordState(e);
    const grabbed = buttonForEvent(e) === BTN_LEFT
      && !(mods.ctrl || mods.shift || mods.alt)
      ? this._anchorAt(e.offsetX, e.offsetY) : null;
    if (grabbed) {
      this.active = {
        pointerId: e.pointerId, tool: "anchor-move", device: e.pointerType,
        grabbed, start: [dx, dy],
        offset: grabbed.part === "dot"
          ? [dx - grabbed.anchor.x, dy - grabbed.anchor.y]
          : [dx - grabbed.anchor.callout[0], dy - grabbed.anchor.callout[1]],
        lastView: [e.offsetX, e.offsetY], startView: [e.offsetX, e.offsetY],
        pts: [], press: [], erased: [],
      };
      return;
    }

    // ── the selection's own targets, before any tool is resolved ────────────
    // A live selection is grabbable with ANY tool (row 125), and its chip and
    // delete cross are tap targets that have to beat the tool underneath them.
    if (this.hasSelection() && this._selectionPress(e, dx, dy)) return;

    const tool = this.toolForEvent(e);
    if (!tool || !IMPLEMENTED_TOOLS.has(tool)) return;

    // A PRESS OUTSIDE A SELECTION DISMISSES IT, AND DOES NOTHING ELSE.
    //
    // The press above this line is one that HIT the selection — a grab, the
    // chip, the delete cross. Anything else means you are done with it, and the
    // first thing you do afterwards should not also land on the page: a stroke
    // drawn while a selection was still up is one you have to undo, and the
    // marks are on top of work you had just been arranging.
    //
    // The view tools are exempt on purpose. Panning or zooming does not touch
    // the page and is a thing you do WHILE arranging a selection — losing it
    // because you moved the view would be its own bug.
    if (this.hasSelection() && pressDismissesSelection(tool)) {
      this.clearSelection();
      // and the REST of the gesture goes with it. Leaving `active` unset is
      // what does that here: a pen press always jitters, and the drawing
      // branch would otherwise take the motion and leave a stray mark beside
      // the selection you were only dismissing.
      return;
    }

    const startPts = [];
    const startPress = [];
    if ((tool === "pen" || tool === "highlighter") && this.pen.hover_lead
        && e.pointerType === "pen") {
      // free REAL data: the positions from just before contact, which the panel
      // tracked anyway. Nothing is guessed here.
      for (const [hx, hy] of hoverLeadIn(this.hoverTrail, dx, dy, e.timeStamp)) {
        startPts.push([hx, hy]);
        startPress.push(0.0);
      }
    }
    startPts.push([dx, dy]);
    startPress.push(this._pressureOf(e));

    this.active = {
      pointerId: e.pointerId,
      tool,
      device: e.pointerType,
      pts: startPts,
      press: startPress,
      lastView: [e.offsetX, e.offsetY],
      startView: [e.offsetX, e.offsetY],
      erased: [],
      touchStrokes: [],
    };
    this._clearSnap();
    if (tool === "text") {
      // a fresh press starts a new selection; the words may still be loading,
      // in which case the drag simply catches up when they arrive
      this.textSelection = [];
      this.active.cur = [dx, dy];
      this._wordsFor(this.pageIndex).then(() => {
        if (this.active && this.active.tool === "text") {
          this._updateTextSelection(this.active);
        }
      });
    }
    if (tool === "eraser") this._eraseAt(dx, dy);
    if (tool === "pen" || tool === "highlighter") {
      // both clocks start here, and the dwell's anchor with them
      this._snapAnchor = [e.offsetX, e.offsetY];
      this._armSnapTimer();
      this._armCircleLasso(dx, dy);
    }
    this._updateCursor();
    this.requestDraw();
  }

  _pressureOf(e) {
    // A mouse has no pressure, and Sidemark treats "no pressure" as a real
    // state: the profile then comes from the taper alone. The browser reports a
    // flat 0.5 for a mouse, which would be a lie about the device.
    return e.pointerType === "pen" ? e.pressure : 1.0;
  }

  _hasPressure() {
    return this.active && this.active.device === "pen";
  }

  _onMove(e) {
    const [dx, dy] = this.toDoc(e.offsetX, e.offsetY);

    if (e.pointerType === "touch") this.latch.move(e.pointerId, e.offsetX, e.offsetY);

    if (this._pinch && this.latch.count >= 2) {
      // The pinch is the latch's OWN arithmetic — centroid and spread — rather
      // than a recognised gesture, and it is applied INCREMENTALLY so a step
      // that lands short cannot drag the whole gesture off.
      const m = this.latch.metrics();
      if (m && this._pinch.spread > 1) {
        const f = m.spread / this._pinch.spread;
        this.zoomAt(f, m.cx, m.cy);
        this.panBy(m.cx - this._pinch.cx, m.cy - this._pinch.cy);
      }
      this._pinch = { ...m, zoom: this.zoom };
      return;
    }

    this._pointerAt = [e.offsetX, e.offsetY];
    if (!this.active) {
      // a stylus in proximity: this is the hover trail the lead-in reads
      if (e.pointerType === "pen" && e.buttons === 0) {
        this.hoverTrail.push([dx, dy, e.timeStamp]);
        const cut = e.timeStamp - HOVER_TRAIL_MS;
        while (this.hoverTrail.length && this.hoverTrail[0][2] < cut) {
          this.hoverTrail.shift();
        }
      }
      return;
    }
    if (e.pointerId !== this.active.pointerId) return;

    const a = this.active;
    if (a.tool === "pan") {
      this.panBy(e.offsetX - a.lastView[0], e.offsetY - a.lastView[1]);
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }
    if (a.tool === "zoom") {
      a.lastView = [e.offsetX, e.offsetY];
      this.requestDraw();
      return;
    }
    if (a.tool === "text") {
      a.cur = [dx, dy];
      this._updateTextSelection(a);
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }
    if (a.tool === "anchor-move") {
      const { anchor, part } = a.grabbed;
      const x = dx - a.offset[0], y = dy - a.offset[1];
      const text = this.doc.notes.get(this.pageIndex);
      this._writeNotes(part === "dot" ? moveAnchor(text, anchor.line, x, y)
                                      : moveCallout(text, anchor, x, y));
      // the parse is by LINE, and the line has not moved, so the record stays
      // valid for the rest of the drag
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }
    if (a.tool === "vertex") {
      this._dragVertex(a, dx, dy);
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }
    if (a.tool === "move" || a.tool === "resize" || a.tool === "rotate") {
      this._transformSelection(a, dx, dy);
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }

    // THE PEN'S SAMPLES ARRIVE COMPRESSED (row 147). The browser delivers one
    // pointermove per frame and buffers the rest; `getCoalescedEvents()` is the
    // trail back. Measured on this hardware the panel reports at 133 Hz while a
    // frame-rate stream sees ~30 — 78% of every stroke discarded, which is the
    // whole of what the pipeline used to call "undersampling". Walk the
    // recovered samples in BEFORE the event's own point.
    // A stroke the dwell has already settled must not take more samples: they
    // would be appended to the clean shape, undoing the recognition a moment
    // after it fired. The pen drives the held control point from here on.
    if (this._straightMode && (a.tool === "pen" || a.tool === "highlighter")) {
      this._dragLiveVertex(a, dx, dy);
      a.lastView = [e.offsetX, e.offsetY];
      return;
    }

    const coalesced = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    const samples = coalesced.length ? coalesced : [e];
    const streamed = this.onInkStream ? [] : null;
    for (const s of samples) {
      const [sx, sy] = this.toDoc(s.offsetX, s.offsetY);
      if (a.tool === "eraser") {
        this._eraseAt(sx, sy);
      } else {
        a.pts.push([sx, sy]);
        a.press.push(this._pressureOf(s));
      }
      // the SAME samples the model just took, so the desktop's pipeline sees
      // what this one saw — including everything getCoalescedEvents recovered
      if (streamed) streamed.push([sx, sy, this._pressureOf(s)]);
    }
    if (streamed) this._stream(a, streamed);
    a.lastView = [e.offsetX, e.offsetY];

    // A HOLD IS NOT A FREEZE. Both dwells stay alive while the pen is merely
    // drifting; see HOLD_SLOP_PX for why they measure from different origins.
    if (Math.hypot(e.offsetX - a.startView[0], e.offsetY - a.startView[1])
        > HOLD_SLOP_PX) {
      this._cancelCircleLasso();     // this press went somewhere: a real stroke
    }
    if (a.tool === "pen" || a.tool === "highlighter") {
      const anchor = this._snapAnchor || a.startView;
      if (Math.hypot(e.offsetX - anchor[0], e.offsetY - anchor[1]) > HOLD_SLOP_PX) {
        // the pen is still travelling — start the clock again from here, rather
        // than on every event, which is what a shaking hand could never outrun
        this._snapAnchor = [e.offsetX, e.offsetY];
        this._armSnapTimer();
      }
    }
    this.requestDraw();
  }

  /** Re-armed on every motion, so it only fires when the pen has actually
   * rested. One hold time to learn, shared with circle-to-lasso. */
  _armSnapTimer() {
    this._cancelSnapTimer();
    if (this.pen.shape_snap === "off") return;
    this._snapTimer = setTimeout(() => this._snapToShape(), STRAIGHT_HOLD_MS);
  }

  _cancelSnapTimer() {
    if (this._snapTimer !== null) {
      clearTimeout(this._snapTimer);
      this._snapTimer = null;
    }
  }

  /** The cursor has rested mid-stroke: recognise the stroke so far as a clean
   * line, rectangle, ellipse or polygon and replace it in place. The LINE is
   * the fallback, so "lines only" and the classic straight snap are the same
   * code path — which is why turning recognition down can never regress it. */
  _snapToShape() {
    this._snapTimer = null;
    const a = this.active;
    if (!a || a.pts.length < 2) return;
    const rawEnd = a.pts[a.pts.length - 1];   // where the pen actually is
    let kind, pts;
    if (this.pen.shape_snap === "lines") {
      kind = "line";
      pts = [a.pts[0], a.pts[a.pts.length - 1]];
    } else {
      ({ kind, pts } = recognizeShape(a.pts));
    }
    if (kind === "line") {
      // a straight line inside a rectangle already on the page is a GRID
      // DIVIDER, not a line — the rectangle is found geometrically, so this
      // works on any box-like stroke and survives a reload
      const div = snapGridDivider(this.strokes, pts[0], pts[pts.length - 1]);
      if (div) ({ kind, pts } = div);
    }
    this._straightMode = true;
    this._snapKind = kind;
    this._snapLabel = SNAP_LABELS[kind];
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    this._snapAt = [Math.max(...xs), Math.min(...ys)];
    a.pts = pts;
    a.press = [];              // a settled shape has no pressure profile
    this._armLiveVertex(a, rawEnd);
    this.requestDraw();
  }

  /** After the dwell the pen KEEPS HOLD of the shape's last control point, and
   * every corner polyline already on the page becomes a live magnet for it — so
   * a fresh shape joins what is already drawn without ever lifting.
   *
   * Only the corner kinds get this: a line, a path, a polygon. There is no
   * single point to keep hold of on an ellipse, and a rectangle's corner would
   * have to skew it. For a closed ring the point is index 0, since that is the
   * one `moveShapeVertex` carries to both ends.
   *
   * The target sets are frozen HERE, at dwell time: rebuilding them on every
   * motion event over a page of handwriting is tens of thousands of segments.
   * The live shape offers its OWN points and edges too — the held point and the
   * two edges meeting it are excluded by `snapPoint`, or it would pin itself. */
  _armLiveVertex(a, rawEnd) {
    a.liveHeld = null;
    // ...except an ellipse, which has no vertex and so scales as a whole
    // instead (row 179). `a.pts` before this call is where the pen actually
    // is, which is what makes the first motion event a no-op.
    a.liveEllipse = this._snapKind === "ellipse"
      ? ellipseResizeState(a.pts, rawEnd) : null;
    if (!["line", "path", "polygon"].includes(this._snapKind)) return;
    if (!a.pts || a.pts.length < 2) return;
    const closed = polylineIsClosed(a.pts);
    a.liveHeld = closed ? 0 : a.pts.length - 1;
    a.liveSelf = { stroke: a, verts: a.pts };
    a.liveTargets = this._pageShapes();
    a.liveCurves = this._snapCurves();
  }

  /** A settled shape does not accept new samples — the pen is now moving its
   * held control point instead. This is also the classic straight snap: a
   * recognised LINE whose far end follows the nib is exactly a rubber band. */
  _dragLiveVertex(a, dx, dy) {
    if (a.liveEllipse) {
      a.pts = resizedEllipse(a.liveEllipse, dx, dy);
      this._snapAt = [Math.max(...a.pts.map((p) => p[0])),
                      Math.min(...a.pts.map((p) => p[1]))];
      this.requestDraw();
      return;
    }
    if (a.liveHeld === null || a.liveHeld === undefined) return;
    const hit = vertexSnapRadius(this.cssW, this.cssH) / this.zoom;
    const curves = curveSnapShapes(a.liveCurves || [], dx, dy, hit);
    const held = [{ stroke: a, index: a.liveHeld }];
    const shapes = (a.liveTargets || []).concat([a.liveSelf]);
    const snapped = snapPoint(shapes, dx, dy, hit, held, curves);
    const [x, y] = snapped || [dx, dy];
    this._snapAtPoint = snapped;
    a.pts = moveShapeVertex(a.pts, a.liveHeld, x, y);
    // the frozen entry describes an array that has just been replaced
    a.liveSelf.verts = a.pts;
    // the label names the shape you are about to be left with, so it rides the
    // shape's corner rather than staying where the dwell happened to fire
    const xs = a.pts.map((p) => p[0]), ys = a.pts.map((p) => p[1]);
    this._snapAt = [Math.max(...xs), Math.min(...ys)];
    this.requestDraw();
  }

  // ── circle to lasso (row 126) ──────────────────────────────────────────────

  /** Only the LAST stroke converts — anything else means a resting hand eats
   * ink into a selection. Deliberately NOT gated on `shape_snap`: that setting
   * governs the dwell, and this is an independent mechanism that has to keep
   * working with the snap off. The TOOL does not change either: the pen stays
   * in your hand, which is the entire point. */
  _circleLassoTarget(px, py) {
    const strokes = this.strokes;
    if (!strokes.length) return null;
    const last = strokes[strokes.length - 1];
    if (strokeHit(last.pts, px, py, eraseRadius(last.width))) return last;
    if (polylineIsClosed(last.pts) && pointInPolygon(px, py, last.pts)) return last;
    return null;
  }

  _armCircleLasso(px, py) {
    this._cancelCircleLasso();
    if (!this._circleLassoTarget(px, py)) return;
    this._circleTimer = setTimeout(() => this._circleLassoFire(), CIRCLE_LASSO_HOLD_MS);
  }

  _cancelCircleLasso() {
    if (this._circleTimer !== null) {
      clearTimeout(this._circleTimer);
      this._circleTimer = null;
    }
  }

  /** The hold landed: the stroke under the pen becomes the lasso path.
   *
   * Removing it and selecting its catch is ONE undo entry, so a mis-fire costs
   * exactly one Ctrl+Z. The rest of the still-held drag is dropped — the
   * gesture stopped being a stroke the moment this fired. */
  _circleLassoFire() {
    this._circleTimer = null;
    const strokes = this.strokes;
    if (!strokes.length) return;
    const loop = strokes[strokes.length - 1];
    this.active = null;              // the nascent stroke is abandoned
    this._clearSnap();
    const index = strokes.indexOf(loop);
    strokes.splice(index, 1);
    this._pushUndo({ type: "erase", page: this.pageIndex,
                     strokes: [{ stroke: loop, index }] });
    this.invalidateLayer();
    // fed through the ordinary lasso close, so a converted circle and a drawn
    // one select identically — and both keep their outline
    this._finishLasso(loop.pts, false);
    this.onChange();
    this.requestDraw();
  }

  _clearSnap() {
    this._cancelSnapTimer();
    this._straightMode = false;
    this._snapKind = null;
    this._snapLabel = null;
    this._snapAt = null;
    // the halo belongs to the point that was in hand; a stale one paints a
    // snap on a page where nothing is being dragged
    this._snapAtPoint = null;
  }

  _onUp(e, cancelled = false) {
    if (e.pointerType === "touch") {
      const left = this.latch.up(e.pointerId);
      if (left < 2) this._pinch = null;
      if (left === 0) this.requestDraw();
    }
    if (!this.active || e.pointerId !== this.active.pointerId) {
      // The gesture was abandoned somewhere other than here — converted to a
      // lasso, or dropped when a second finger started a pinch — so none of
      // the branches below will run. Close the mirrored stroke anyway.
      this._closeStream({ abort: true });
      return;
    }
    const a = this.active;
    this.active = null;
    this._cancelCircleLasso();
    this._updateCursor();

    // Abandoning at the touchdown is not enough — a press can be cancelled with
    // the tool still in hand — so the guard is also HERE, at the commit.
    //
    // It is armed by the CAPTURE DEVICE, never by the touch count: a palm
    // resting on the glass during a PEN stroke must arm nothing, or writing
    // with your hand down silently loses the stroke. Ink is revocable only for
    // the hand that drew it.
    if (cancelled || (a.device === "touch" && this.latch.multi)) {
      // the desktop is mid-stroke and has to drop it too, or it is left with
      // half a line and nothing coming to finish it
      this._closeStream({ abort: true });
      this.requestDraw();
      return;
    }

    if (a.tool === "pen" || a.tool === "highlighter") {
      this._closeStream({ end: true });
      this._commitStroke(a);
      this._clearSnap();
    } else if (a.tool === "eraser") {
      this._closeStream({ end: true });
      if (a.erased.length) {
        this._pushUndo({ type: "erase", page: this.pageIndex, strokes: a.erased });
      }
    } else if (a.tool === "zoom") {
      this._zoomToRegion(a.startView, [e.offsetX, e.offsetY]);
    } else if (a.tool === "text") {
      // a CLICK places the caret, it does not select — without this every
      // stray tap leaves a word highlighted behind it
      const moved = Math.hypot(e.offsetX - a.startView[0],
                               e.offsetY - a.startView[1]);
      if (moved < 3) this.clearTextSelection();
    } else if (a.tool === "lasso") {
      const { shift } = this._chordState(e);
      this._finishLasso(a.pts, shift);
    } else if (a.tool === "anchor") {
      // a drag places the callout where you let go; a click leaves a bare dot
      const moved = Math.hypot(e.offsetX - a.startView[0], e.offsetY - a.startView[1]);
      const [ex, ey] = this.toDoc(e.offsetX, e.offsetY);
      this._placeAnchor(a.pts[0][0], a.pts[0][1], moved > 12 ? [ex, ey] : null);
    } else if (a.tool === "anchor-move") {
      // nothing to commit: each motion already wrote the notes
    } else if (a.tool === "vertex") {
      this._snapAtPoint = null;
      this._commitVertex(a);
    } else if (a.tool === "move" || a.tool === "resize" || a.tool === "rotate") {
      this._commitTransform(a);
    }
    this.requestDraw();
  }

  _onLeave(e) {
    if (e.pointerType === "pen") this.hoverTrail.length = 0;
  }

  /** A second finger abandons the stroke the first FINGER began — it is taken
   * off the page rather than committed. A pen stroke is never abandoned: on a
   * convertible the palm lands before the tip does, so a touch arriving
   * mid-stroke is the hand you are writing with. */
  _abandon() {
    if (this.active && this.active.device === "touch") {
      this.active = null;
      this._updateCursor();
    }
  }

  // ── anchors and callouts ───────────────────────────────────────────────────

  /** The anchors on this page, parsed from its notes. Re-read rather than
   * cached: the notes are the truth, and they can change from the panel at any
   * time. */
  get anchors() {
    if (!this.doc) return [];
    return parsePageMarks(this.doc.notes.get(this.pageIndex));
  }

  _writeNotes(text) {
    this.doc.notes.set(this.pageIndex, text);
    this.onNotesChanged();
    this.onChange();
    this.requestDraw();
  }

  /** The anchor or callout under a screen point, if any. */
  _anchorAt(sx, sy) {
    const ctx = this.ctx;
    for (const a of this.anchors) {
      const screen = this.toView(a.x, a.y);
      const box = a.callout
        ? calloutBox(ctx, a.text, ...this.toView(a.callout[0], a.callout[1]))
        : null;
      // a dot under its own box is not drawn, so it is not there to grab
      if (!coveredBy(box, screen)
          && Math.hypot(sx - screen[0], sy - screen[1]) <= ANCHOR_R + 4) {
        return { anchor: a, part: "dot" };
      }
      if (coveredBy(box, [sx, sy])) return { anchor: a, part: "callout" };
    }
    return null;
  }

  /** Place an anchor. A DRAG places its callout where you let go, which is why
   * the tool is a drag and not a click. */
  _placeAnchor(dx, dy, callout = null) {
    if (!this.doc) return;
    this._writeNotes(addAnchor(this.doc.notes.get(this.pageIndex), dx, dy, callout));
  }

  _drawAnchors(ctx) {
    if (!this.doc) return;
    const accent = "rgb(53, 132, 228)";
    const dim = "rgba(34, 33, 29, 0.85)";
    for (const a of this.anchors) {
      const screen = this.toView(a.x, a.y);
      let box = null;
      if (a.callout) {
        const [cx, cy] = this.toView(a.callout[0], a.callout[1]);
        box = calloutBox(ctx, a.text, cx, cy);
      }
      drawAnchor(ctx, screen, box, accent, dim);
    }
  }

  // ── the caret ──────────────────────────────────────────────────────────────

  /** The page's words, extracted once. A page with no text layer (a scan)
   * yields none, and the caret then simply selects nothing rather than
   * pretending. */
  async _wordsFor(page) {
    if (this._words.has(page)) return this._words.get(page);
    let words = [];
    try {
      words = await pageWords(await this.doc.page(page), this.pageH);
    } catch { words = []; }
    this._words.set(page, words);
    this.requestDraw();
    return words;
  }

  get selectedText() { return selectionText(this.textSelection); }

  hasTextSelection() { return this.textSelection.length > 0; }

  clearTextSelection() {
    if (!this.textSelection.length) return;
    this.textSelection = [];
    this.requestDraw();
  }

  _updateTextSelection(a) {
    const words = this._words.get(this.pageIndex);
    if (!words || !words.length) return;
    if (this.textStyle === "rect") {
      this.textSelection = wordsInRect(words, a.start[0], a.start[1],
                                       a.cur[0], a.cur[1]);
    } else {
      const from = a.anchorWord ?? nearestWord(words, a.start[0], a.start[1]);
      a.anchorWord = from;
      const to = nearestWord(words, a.cur[0], a.cur[1]);
      this.textSelection = wordsBetween(words, from, to);
    }
    this.requestDraw();
  }

  // ── the lasso selection ────────────────────────────────────────────────────

  hasSelection() { return this.selected.length > 0; }

  /** ONE box, used by the frame AND the hit-tests, or they drift apart. */
  _selectionBbox() { return selectionBbox(this.selected); }

  _selectionScreenBox() {
    const b = this._selectionBbox();
    if (!b) return null;
    const [x0, y0] = this.toView(b[0], b[1]);
    const [x1, y1] = this.toView(b[2], b[3]);
    return [x0, y0, x1, y1];
  }

  /** The GRAB region: the LOOP in loop mode, the padded box otherwise. It has
   * to match what is painted — with the loop on screen, a press in the corner
   * of the box but outside the loop is not a grab, it is a new lasso. */
  _pointInSelection(px, py) {
    if (!this.selectionBoxed && this.selectionLoop) {
      return pointInPolygon(px, py, this.selectionLoop);
    }
    const b = this._selectionBbox();
    if (!b) return false;
    const pad = LASSO_PAD / this.zoom;
    return px >= b[0] - pad && px <= b[2] + pad
        && py >= b[1] - pad && py <= b[3] + pad;
  }

  _handleAt(sx, sy) {
    const box = this._selectionScreenBox();
    if (!box) return null;
    const pts = lassoHandlePoints(box[0], box[1], box[2], box[3], LASSO_PAD);
    for (let i = 0; i < pts.length; i++) {
      if (Math.abs(sx - pts[i][0]) <= HANDLE_HIT
          && Math.abs(sy - pts[i][1]) <= HANDLE_HIT) return i;
    }
    return null;
  }

  // ── control points ─────────────────────────────────────────────────────────

  /** Every selected stroke that HAS control points, with them. */
  _vertexShapes() {
    const out = [];
    for (const stroke of this.selected) {
      if (isImage(stroke)) continue;   // four handles on a photo would skew it
      const verts = shapeVertices(stroke.pts);
      if (verts.length) out.push({ stroke, verts });
    }
    return out;
  }

  /** The control point under a document point, as the WELD at that spot — two
   * points sharing a coordinate are one point and drag together. */
  _vertexAt(px, py) {
    const hit = VERTEX_HIT_PX / this.zoom;
    let best = null, bestD = hit;
    for (const { stroke, verts } of this._vertexShapes()) {
      verts.forEach((v, i) => {
        const d = Math.hypot(v[0] - px, v[1] - py);
        if (d <= bestD) { bestD = d; best = { stroke, index: i, at: v }; }
      });
    }
    if (!best) return null;
    // every point at that coordinate, across every shape on the page
    return weldedVertices(this._pageShapes(), best.at[0], best.at[1]);
  }

  /** Every corner polyline on the page — the magnets a dragged point snaps to.
   * Frozen per gesture; rebuilding them on each motion event over a page of
   * shapes is work nobody asked for. */
  _pageShapes() {
    const out = [];
    for (const stroke of this.strokes) {
      if (isImage(stroke)) continue;
      const verts = shapeVertices(stroke.pts);
      if (verts.length) out.push({ stroke, verts });
    }
    return out;
  }

  _snapTargets(held) {
    return this._pageShapes();
  }

  /** Freehand strokes as snap targets — their ENDS as points, their polyline as
   * edges. Bbox-filtered at drag time. */
  _snapCurves() {
    const out = [];
    for (const stroke of this.strokes) {
      if (isImage(stroke)) continue;
      if (shapeVertices(stroke.pts).length) continue;   // that is a corner shape
      if (stroke.pts.length >= 2) out.push({ stroke, verts: stroke.pts });
    }
    return out;
  }

  _dragVertex(a, dx, dy) {
    const hit = vertexSnapRadius(this.cssW, this.cssH) / this.zoom;
    const curves = curveSnapShapes(a.curves, dx, dy, hit);
    const snapped = snapPoint(a.targets, dx, dy, hit, a.held, curves);
    const [x, y] = snapped || [dx, dy];
    this._snapAtPoint = snapped;
    for (const { stroke, index } of a.held) {
      stroke.pts = moveShapeVertex(stroke.pts, index, x, y);
    }
    this.invalidateLayer();
    this.requestDraw();
  }

  _commitVertex(a) {
    const moved = a.before.some((rec) =>
      rec.pts.some((p, i) => p[0] !== rec.stroke.pts[i][0]
                          || p[1] !== rec.stroke.pts[i][1]));
    if (!moved) return;
    this._pushUndo({
      type: "reshape", page: this.pageIndex,
      records: a.before.map((rec) => ({ stroke: rec.stroke, pts: rec.pts,
                                        width: rec.stroke.width })),
      after: a.before.map((rec) => ({ pts: rec.stroke.pts.map((p) => p.slice()),
                                      width: rec.stroke.width })),
    });
    this.onChange();
  }

  /** A deep copy of the selected strokes' geometry, for the undo op. */
  _snapshotSelected() {
    return this.selected.map((s) => ({
      stroke: s,
      pts: s.pts.map((p) => [p[0], p[1]]),
      width: s.width,
      loop: this.selectionLoop ? this.selectionLoop.map((p) => [p[0], p[1]]) : null,
    }));
  }

  /** Chip, delete cross, resize handle or a grab — in that order. Returns true
   * when the press has been claimed. */
  _selectionPress(e, dx, dy) {
    const box = this._selectionScreenBox();
    if (!box) return false;
    const [chx, chy] = lassoChipCentre(box[0], box[1], LASSO_PAD);
    const [dlx, dly] = lassoDeleteCentre(box[0], box[1], LASSO_PAD);

    // ANY tap target on a canvas must kill the REST of the gesture, not just
    // consume the press: a pen tap always jitters, and the drawing branch is
    // the last one in the router, so a consumed press that forgets this leaves
    // a stray mark beside the button you pressed.
    if (lassoDeleteHit(dlx, dly, e.offsetX, e.offsetY)) {
      this.deleteSelected();
      return true;
    }
    if (lassoChipHit(chx, chy, e.offsetX, e.offsetY)) {
      this.selectionBoxed = !this.selectionBoxed;
      this.requestDraw();
      return true;
    }
    const base = {
      pointerId: e.pointerId, device: e.pointerType,
      start: [dx, dy], before: this._snapshotSelected(),
      lastView: [e.offsetX, e.offsetY], startView: [e.offsetX, e.offsetY],
      pts: [], press: [], erased: [],
    };
    if (this.selectionBoxed) {
      // A control point sits inside the box, on top of a resize handle, and
      // WINS there — the corner of a shape is what you were aiming at.
      const grabbed = this._vertexAt(dx, dy);
      if (grabbed) {
        this.active = {
          ...base, tool: "vertex",
          held: grabbed,
          before: grabbed.map(({ stroke }) => ({ stroke, pts: stroke.pts.map((p) => p.slice()) })),
          targets: this._snapTargets(grabbed),
          curves: this._snapCurves(),
        };
        return true;
      }
      const bbox = this._selectionBbox();
      const [kx, ky] = rotateKnobCentre(box[0], box[1], box[2]);
      if (rotateKnobHit(kx, ky, e.offsetX, e.offsetY)) {
        const cx = (bbox[0] + bbox[2]) / 2, cy = (bbox[1] + bbox[3]) / 2;
        this.active = { ...base, tool: "rotate", centre: [cx, cy],
                        startAngle: Math.atan2(dy - cy, dx - cx) };
        return true;
      }
      const handle = this._handleAt(e.offsetX, e.offsetY);
      if (handle !== null) {
        this.active = { ...base, tool: "resize", handle, bbox };
        return true;
      }
    }
    if (this._pointInSelection(dx, dy)) {
      this.active = { ...base, tool: "move" };
      this._updateCursor();
      return true;
    }
    return false;
  }

  /** Turn the drawn loop into a selection. Shift ADDS to what was already
   * selected — Shift+lasso is still the lasso, which is why this is an
   * exception AT the router rather than a fork of the binding table. */
  _finishLasso(loop, additive) {
    if (loop.length < 3) {
      // a click, not a loop: select what is under it, ink before images
      const hit = this._strokeAt(loop[0]);
      this._setSelected(hit ? [hit] : [], null);
      return;
    }
    const caught = this.strokes.filter((s) =>
      s.pts.some((p) => pointInPolygon(p[0], p[1], loop)));
    this._setSelected(additive ? mergeSelection(this.selected, caught) : caught, loop);
  }

  /** What a click selects: INK BEFORE IMAGES, because ink is painted on top of
   * them and so is what you were pointing at. */
  _strokeAt(pt) {
    for (let i = this.strokes.length - 1; i >= 0; i--) {
      const s = this.strokes[i];
      if (isImage(s)) continue;
      if (strokeHit(s.pts, pt[0], pt[1], Math.max(s.width, 6) / 2 + 3)) return s;
    }
    for (let i = this.strokes.length - 1; i >= 0; i--) {
      const s = this.strokes[i];
      if (isImage(s) && imageHit(s, pt[0], pt[1])) return s;
    }
    return null;
  }

  /** `loop` null means BOX mode — a click, a paste or an additive selection has
   * no loop to wear. `_finishLasso` is the only thing that puts one back. */
  _setSelected(strokes, loop) {
    this.selected = strokes;
    this.selectionLoop = strokes.length ? loop : null;
    if (!loop) this.selectionBoxed = strokes.length > 0;
    else this.selectionBoxed = false;
    this._updateCursor();
    this.requestDraw();
  }

  clearSelection() { this._setSelected([], null); }

  deleteSelected() {
    if (!this.hasSelection()) return;
    const removed = [];
    for (const s of this.selected) {
      const i = this.strokes.indexOf(s);
      if (i >= 0) { this.strokes.splice(i, 1); removed.push({ stroke: s, index: i }); }
    }
    if (removed.length) {
      this._pushUndo({ type: "erase", page: this.pageIndex, strokes: removed });
    }
    this.clearSelection();
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
  }

  /** Change the colour of every selected stroke — picking a colour with ink
   * lassoed recolours it, rather than only arming the next stroke.
   *
   * IMAGES would be skipped here: there is no pen colour on a photograph. */
  recolourSelected(color) {
    if (!this.hasSelection()) return false;
    // there is no pen colour on a photograph
    const before = this.selected.filter((s) => !isImage(s))
                                .map((s) => ({ stroke: s, color: s.color }));
    if (!before.length) return false;
    if (before.every((r) => r.color.every((c, i) => c === color[i]))) return false;
    for (const rec of before) rec.stroke.color = color.slice();
    this._pushUndo({ type: "recolour", page: this.pageIndex, records: before,
                     after: color.slice() });
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
    return true;
  }

  /** Copy the lasso selection. Wins over text copy when there is one — the
   * ink you have selected is what you meant. */
  async copySelected() {
    if (!this.hasSelection()) return null;
    return copySelection(this.selected);
  }

  /** Paste the held ink at a document point, and select it — so a fresh paste
   * drags immediately, with the pen or the caret still in hand. */
  pasteAt(px, py) {
    if (!hasCopy()) return false;
    const size = copyExtent() || [0, 0];
    // dropped so its CENTRE is under the pointer, which is where you are
    // looking when you press Ctrl+V
    const copies = takeCopy(px - size[0] / 2, py - size[1] / 2);
    if (!copies.length) return false;
    for (const c of copies) this.strokes.push(c);
    this._pushUndo({ type: "add", page: this.pageIndex, strokes: copies });
    this._setSelected(copies, null);
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
    return true;
  }

  /** Paste an image from the system clipboard, at a document point.
   *
   * It lands as an OBJECT — bytes and a frame — and comes back SELECTED, so it
   * can be dragged into place immediately with whatever is still in your hand.
   * Its size is `pasteScale`'s four caps, of which the visible-window one is
   * what stops a screenshot arriving several screens wide when you are zoomed
   * in. */
  async pasteImageAt(bytes, mime, px, py) {
    const image = await makeImage(bytes, mime);
    const [w, h] = pasteScale(image.natural, [this.pageW, this.pageH],
                              [this.cssW, this.cssH], this.zoom);
    if (!(w > 0 && h > 0)) return false;
    const obj = { image, pts: imageQuad(px - w / 2, py - h / 2, w, h) };
    this.strokes.push(obj);
    // the SAME "add" op ink uses: one list, one undo, nothing to keep in step
    this._pushUndo({ type: "add", page: this.pageIndex, strokes: [obj] });
    this._setSelected([obj], null);
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
    return true;
  }

  /** Where a paste lands: the POINTER when it is over the page, else the middle
   * of the view. Never the caret — with a pen or the lasso in hand there is no
   * useful caret. */
  pastePoint() {
    const p = this._pointerAt || [this.cssW / 2, this.cssH / 2];
    return this.toDoc(p[0], p[1]);
  }

  duplicateSelected() {
    if (!this.hasSelection()) return;
    const offset = DUPLICATE_OFFSET / this.zoom;
    const copies = this.selected.map((s) => ({
      ...s,
      pts: s.pts.map((p) => [p[0] + offset, p[1] + offset]),
      profile: s.profile ? s.profile.slice() : null,
    }));
    for (const c of copies) this.strokes.push(c);
    this._pushUndo({ type: "add", page: this.pageIndex, strokes: copies });
    // the copy comes back selected, so it drags immediately — with the pen or
    // the caret still in hand
    this._setSelected(copies, null);
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
  }

  /** Move or scale the live selection. One undo entry per GESTURE. */
  _transformSelection(a, dx, dy) {
    if (a.tool === "move") {
      const ox = dx - a.start[0], oy = dy - a.start[1];
      for (const rec of a.before) {
        rec.stroke.pts = rec.pts.map((p) => [p[0] + ox, p[1] + oy]);
      }
      if (a.before[0]?.loop) {
        this.selectionLoop = a.before[0].loop.map((p) => [p[0] + ox, p[1] + oy]);
      }
    } else if (a.tool === "rotate") {
      const [cx, cy] = a.centre;
      let angle = Math.atan2(dy - cy, dx - cx) - a.startAngle;
      if (this._heldMods.shift) {
        // Shift snaps the TOTAL angle, not the delta — snapping the delta would
        // make the result depend on where you grabbed the knob
        const step = (ROTATE_SNAP_DEG * Math.PI) / 180;
        angle = Math.round(angle / step) * step;
      }
      for (const rec of a.before) {
        rec.stroke.pts = rec.pts.map((p) => rotatePoint(p, angle, cx, cy));
      }
      if (a.before[0]?.loop) {
        this.selectionLoop = a.before[0].loop.map((p) => rotatePoint(p, angle, cx, cy));
      }
    } else {
      const { mode, anchor } = lassoHandleAnchor(a.handle, a.bbox);
      const [fx, fy] = lassoScaleFactors(mode, anchor, a.start, [dx, dy]);
      for (const rec of a.before) {
        rec.stroke.pts = rec.pts.map((p) => scalePoint(p, fx, fy, anchor[0], anchor[1]));
        // a stroke's width scales with the area, so a uniform resize keeps it
        // looking like the same pen
        rec.stroke.width = rec.width * Math.sqrt(Math.abs(fx * fy));
      }
      if (a.before[0]?.loop) {
        this.selectionLoop = a.before[0].loop
          .map((p) => scalePoint(p, fx, fy, anchor[0], anchor[1]));
      }
    }
    this.invalidateLayer();
    this.requestDraw();
  }

  _commitTransform(a) {
    const moved = a.before.some((rec, i) =>
      rec.pts.length !== rec.stroke.pts.length
      || rec.pts.some((p, j) => p[0] !== rec.stroke.pts[j][0]
                             || p[1] !== rec.stroke.pts[j][1]));
    if (!moved) return;
    this._pushUndo({ type: "reshape", page: this.pageIndex, records: a.before,
                     after: a.before.map((rec) => ({
                       pts: rec.stroke.pts.map((p) => [p[0], p[1]]),
                       width: rec.stroke.width,
                     })) });
    this.onChange();
  }

  // ── committing ─────────────────────────────────────────────────────────────

  /** Mirror one batch of raw samples to a live desktop (LIVE mode only). */
  _stream(a, batch) {
    if (!this.onInkStream) return;
    if (a.tool !== "pen" && a.tool !== "eraser") return;
    if (this._streamOpen === null) {
      // nothing is sent until there is something to draw, so a bare tap that
      // never moves opens no gesture on the desktop either
      if (!batch.length) return;
      this._streamOpen = ++this._streamSeq;
      this._streamTool = a.tool;
    }
    this.onInkStream({ t: "ink", id: this._streamOpen, tool: this._streamTool,
                       page: this.pageIndex, pts: batch });
  }

  /** End the mirrored gesture, committing it on the desktop or dropping it.
   *
   * Must be reachable from EVERY way a gesture can end, including the ones
   * that never run _onUp's tool branches. The desktop guards against a
   * missing close as well, because this side cannot promise one. */
  _closeStream(extra) {
    if (this._streamOpen === null || !this.onInkStream) return;
    this.onInkStream({ t: "ink", id: this._streamOpen, tool: this._streamTool,
                       page: this.pageIndex, pts: [], ...extra });
    this._streamOpen = null;
  }

  _commitStroke(a) {
    const flat = a.tool === "highlighter";
    const press = this._hasPressureFor(a) ? a.press : [];
    const { pts, profile } = finishInkStroke(a.pts, press, this.pen.smoothing, {
      flat,
      minPressure: this.pen.min_pressure,
      wasStraight: this._straightMode,
    });
    if (pts.length < 1) return;
    const stroke = {
      pts,
      profile,
      width: flat ? this.pen.hl_width : this.pen.pen_width,
      color: flat ? this.pen.hl_color : this.pen.pen_color,
      opacity: flat ? this.pen.hl_opacity : 1.0,
      flat,
    };
    this.strokes.push(stroke);
    if (this._snapKind === "vdiv" || this._snapKind === "hdiv") {
      // committing a divider re-spaces its siblings to equal cells, and the
      // whole gesture is ONE undo entry: remove the new divider, restore the
      // siblings to where they were
      const sibs = respaceDividers(this.strokes, stroke, this._snapKind === "vdiv");
      if (sibs && sibs.length) {
        this._pushUndo({ type: "grid", page: this.pageIndex, stroke, siblings: sibs });
        this._appendToLayer(stroke);
        this.invalidateLayer();      // the siblings moved, so the layer is stale
        this.onChange();
        return;
      }
    }
    this._pushUndo({ type: "draw", page: this.pageIndex, stroke });
    this._appendToLayer(stroke);
    this.onChange();
    if (this.onStrokeDone) this.onStrokeDone(stroke);
  }

  _hasPressureFor(a) { return a.device === "pen"; }

  _eraseAt(x, y) {
    // The eraser deletes a whole stroke on contact — this is not a brush size,
    // it is "did I touch the ink", which is why the radius tracks the stroke's
    // own width. One undo entry per drag, not per stroke removed.
    const a = this.active;
    for (let i = this.strokes.length - 1; i >= 0; i--) {
      const s = this.strokes[i];
      // THE ERASER IGNORES IMAGES. Rubbing out a note written ON a picture must
      // not take the picture with it; the lasso and Del are how an image goes.
      if (isImage(s)) continue;
      if (strokeHit(s.pts, x, y, eraseRadius(s.width))) {
        this.strokes.splice(i, 1);
        a.erased.push({ stroke: s, index: i });
        this.invalidateLayer();
        this.onChange();
      }
    }
  }

  _zoomToRegion(from, to) {
    const x = Math.min(from[0], to[0]), y = Math.min(from[1], to[1]);
    const w = Math.abs(to[0] - from[0]), h = Math.abs(to[1] - from[1]);
    if (w < 8 || h < 8) {
      // A CLICK, not a region — so it means the other useful thing the zoom
      // tool can do: back to the whole page. Dragging picks a region, clicking
      // undoes every region you have picked, which is the only way back
      // without a second binding.
      this.fit();
      this.requestDraw();
      return;
    }
    const f = Math.min(this.cssW / w, this.cssH / h);
    const cx = x + w / 2, cy = y + h / 2;
    this.zoomAt(f, cx, cy);                            // the centre stays put…
    this.panBy(this.cssW / 2 - cx, this.cssH / 2 - cy);  // …then moves to middle
  }

  // ── undo ───────────────────────────────────────────────────────────────────

  _pushUndo(op) {
    this.undoStack.push(op);
    this.redoStack.length = 0;
  }

  /** The page an undo op belongs to — an op carries its own, so undoing a
   * stroke drawn three pages back edits THAT page rather than whatever is in
   * front now. Undo also brings you to the page it changed: a Ctrl+Z whose
   * effect you cannot see reads as nothing having happened. */
  _opStrokes(op) {
    if (op.type === "notes") return null;   // a model op, not a stroke op
    if (!this.doc) return this.strokes;
    if (op.page !== this.pageIndex) this.setPage(op.page);
    return this.doc.strokesFor(op.page);
  }

  undo() {
    const op = this.undoStack.pop();
    if (!op) return;
    const strokes = this._opStrokes(op);
    if (op.type === "notes") {
      this.doc.notes.restore(op.before);
      this.redoStack.push(op);
      this.onNotesRestored();
      this.onChange();
      return;
    }
    if (op.type === "draw") {
      const i = strokes.lastIndexOf(op.stroke);
      if (i >= 0) strokes.splice(i, 1);
    } else if (op.type === "erase") {
      for (const { stroke, index } of op.strokes) {
        strokes.splice(Math.min(index, strokes.length), 0, stroke);
      }
    } else if (op.type === "add") {
      for (const stroke of op.strokes) {
        const i = strokes.lastIndexOf(stroke);
        if (i >= 0) strokes.splice(i, 1);
      }
    } else if (op.type === "reshape") {
      for (const rec of op.records) {
        rec.stroke.pts = rec.pts.map((p) => [p[0], p[1]]);
        rec.stroke.width = rec.width;
      }
    } else if (op.type === "grid") {
      const i = strokes.lastIndexOf(op.stroke);
      if (i >= 0) strokes.splice(i, 1);
      for (const rec of op.siblings) rec.stroke.pts = rec.before.map((p) => p.slice());
    } else if (op.type === "recolour") {
      for (const rec of op.records) rec.stroke.color = rec.color;
    }
    // a stale loop after an undone move is impossible only because undo clears
    // the selection — do not remove this
    this.clearSelection();
    this.redoStack.push(op);
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
  }

  redo() {
    const op = this.redoStack.pop();
    if (!op) return;
    const strokes = this._opStrokes(op);
    if (op.type === "notes") {
      this.doc.notes.restore(op.after);
      this.undoStack.push(op);
      this.onNotesRestored();
      this.onChange();
      return;
    }
    if (op.type === "draw") {
      strokes.push(op.stroke);
    } else if (op.type === "erase") {
      for (const { stroke } of op.strokes) {
        const i = strokes.lastIndexOf(stroke);
        if (i >= 0) strokes.splice(i, 1);
      }
    } else if (op.type === "add") {
      for (const stroke of op.strokes) strokes.push(stroke);
    } else if (op.type === "reshape") {
      op.records.forEach((rec, i) => {
        rec.stroke.pts = op.after[i].pts.map((p) => [p[0], p[1]]);
        rec.stroke.width = op.after[i].width;
      });
    } else if (op.type === "grid") {
      strokes.push(op.stroke);
      for (const rec of op.siblings) rec.stroke.pts = rec.after.map((p) => p.slice());
    } else if (op.type === "recolour") {
      for (const rec of op.records) rec.stroke.color = op.after.slice();
    }
    this.clearSelection();
    this.undoStack.push(op);
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
  }

  clear() {
    const strokes = this.strokes;
    if (!strokes.length) return;
    this._pushUndo({ type: "erase", page: this.pageIndex,
                     strokes: strokes.map((s, i) => ({ stroke: s, index: i })) });
    strokes.length = 0;          // in place: `strokes` is the page's own array
    this.invalidateLayer();
    this.onChange();
    this.requestDraw();
  }

  // ── painting ───────────────────────────────────────────────────────────────

  requestDraw() {
    if (this._frame) return;
    this._frame = requestAnimationFrame(() => {
      this._frame = null;
      this.draw();
    });
  }

  invalidateLayer() { this._layerKey = null; }

  /** Committed ink is cached into its own layer and blitted, so a page of
   * handwriting costs one image draw per frame instead of re-outlining every
   * stroke (row 147: flat ~1.5 ms/frame against 66 ms at 400 strokes). An
   * append is painted onto the existing layer rather than rebuilding it. */
  _ensureLayer() {
    const dpr = window.devicePixelRatio || 1;
    const key = `${this.cssW}x${this.cssH}@${dpr}:${this.zoom}:${this.offX}:${this.offY}:${this.strokes.length}`;
    if (this._layerKey === key) return;
    if (!this._layer) this._layer = document.createElement("canvas");
    this._layer.width = Math.max(1, Math.round(this.cssW * dpr));
    this._layer.height = Math.max(1, Math.round(this.cssH * dpr));
    const lc = this._layer.getContext("2d");
    lc.setTransform(dpr, 0, 0, dpr, 0, 0);
    lc.clearRect(0, 0, this.cssW, this.cssH);
    // INK DRAWS ON TOP OF IMAGES, in two passes over one list. They share a
    // list so that selecting, transforming and undoing cannot tell them apart;
    // they need two passes because a picture pasted after a note must not bury
    // the note.
    for (const s of this.strokes) if (isImage(s)) this._paintImage(lc, s);
    for (const s of this.strokes) if (!isImage(s)) this._paintStroke(lc, s);
    this._layerKey = key;
  }

  _appendToLayer(stroke) {
    if (this._layerKey === null || !this._layer) return;   // nothing to append to
    const dpr = window.devicePixelRatio || 1;
    const lc = this._layer.getContext("2d");
    lc.setTransform(dpr, 0, 0, dpr, 0, 0);
    // ceiling: an image appended this way lands ON TOP of existing ink for one
    // frame, until the next full repaint puts it back underneath. Invalidating
    // instead would re-paint every stroke on the page for one paste.
    if (isImage(stroke)) this._paintImage(lc, stroke);
    else this._paintStroke(lc, stroke);
    this._layerKey = `${this.cssW}x${this.cssH}@${dpr}:${this.zoom}:${this.offX}:${this.offY}:${this.strokes.length}`;
  }

  /** The same view transform the ink gets, so an image and the notes drawn on
   * it move together and stay in register at every zoom. */
  _paintImage(ctx, obj) {
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    drawImageObject(ctx, obj);
    ctx.restore();
  }

  _paintStroke(ctx, s) {
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    ctx.fillStyle = rgbCss(s.color, s.opacity);
    ctx.strokeStyle = rgbCss(s.color, s.opacity);
    drawInkStroke(ctx, s.pts, s.width, s.profile);
    ctx.restore();
  }

  draw() {
    const dpr = window.devicePixelRatio || 1;
    const ctx = this.ctx;
    // The backing store has to be a WHOLE number of device pixels AND the CSS
    // box has to be exactly that many device pixels back. Sized from the parent
    // and rounded independently, the two disagree by up to half a pixel at a
    // fractional dpr — 1488x1430 against the 1429.5 actually needed — and the
    // browser then rescales the entire canvas on every paint, so nothing on it
    // is ever 1:1.
    const host = this.el.parentElement || this.el;
    const devW = Math.max(1, Math.round(host.clientWidth * dpr));
    const devH = Math.max(1, Math.round(host.clientHeight * dpr));
    if (this.el.width !== devW || this.el.height !== devH) {
      this.el.width = devW;
      this.el.height = devH;
      this.el.style.width = `${devW / dpr}px`;
      this.el.style.height = `${devH / dpr}px`;
      this.invalidateLayer();
    }
    // AFTER the resize above, because `fit` measures the CANVAS and it still
    // carried the old size a moment ago — but OUTSIDE it, because a fit can be
    // owed while the canvas is already the right size, and there it was never
    // paid: the flag stayed set and the view kept whatever `fit` had made of a
    // container that did not exist yet. `fit` sets the flag again if there is
    // still no box, so this cannot spin.
    if (this._fitPending) { this._fitPending = false; this.fit(); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.cssW, this.cssH);

    // the page itself: the rendered PDF, or white paper before one is open.
    // The bitmap is drawn to the page's document-unit rect whatever scale it
    // was rendered at, so a zoom shows a stretched page for one beat rather
    // than blocking on pdf.js — the re-render lands behind it.
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    ctx.fillStyle = "#ffffff";
    ctx.shadowColor = "rgba(0,0,0,0.28)";
    ctx.shadowBlur = 12 / this.zoom;
    ctx.shadowOffsetY = 2 / this.zoom;
    ctx.fillRect(0, 0, this.pageW, this.pageH);
    ctx.restore();
    if (this._pageKey && this._pageCanvas.width > 1) {
      ctx.save();
      ctx.translate(this.offX, this.offY);
      ctx.scale(this.zoom, this.zoom);
      // the bitmap is larger than the space it lands in; ask for the good
      // downscale rather than the fast one, which is where the sharpness is
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(this._pageCanvas, 0, 0, this.pageW, this.pageH);
      ctx.restore();
    }

    this._drawSearchHits(ctx);
    this._drawTextSelection(ctx);
    this._drawAnchors(ctx);
    this._ensureLayer();
    ctx.drawImage(this._layer, 0, 0, this.cssW, this.cssH);

    this.onLiveDraw();
    this._drawLive(ctx);
    this._drawLiveVertices(ctx);
    this._drawSnapLabel(ctx);
    this._drawLassoPath(ctx);
    this._drawSelection(ctx);
    this._drawZoomMarquee(ctx);
  }

  /** The caret's selection, one band per LINE — a row of per-word stamps reads
   * as a list rather than as a passage of text. */
  _drawTextSelection(ctx) {
    if (!this.textSelection.length) return;
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    ctx.fillStyle = "rgba(53, 132, 228, 0.30)";
    for (const [x, y, w, h] of selectionRects(this.textSelection)) {
      ctx.fillRect(x, y, w, h);
    }
    ctx.restore();
  }

  /** Search highlights sit UNDER the ink: they mark what the page says, and
   * covering your own annotation with a yellow box would hide the thing you
   * were most likely looking for. */
  _drawSearchHits(ctx) {
    if (!this.searchRects) return;
    const hits = this.searchRects(this.pageIndex);
    if (!hits || !hits.length) return;
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    for (const { rect, current } of hits) {
      const [x, y, w, h] = rect;
      // PDF user space is y-UP from the bottom-left; document units are y-down
      ctx.fillStyle = current ? "rgba(255, 163, 72, 0.55)"
                              : "rgba(255, 214, 102, 0.42)";
      ctx.fillRect(x, this.pageH - y - h, w, h);
    }
    ctx.restore();
  }

  /** The DESKTOP's in-progress stroke (LIVE mode), painted on top of the
   * cached layer exactly as our own is — no layer invalidation, so mirroring
   * somebody else's pen costs a repaint and not a re-render of the page. It
   * is replaced by real ink the moment they lift, when the delta arrives. */
  _drawRemoteLive(ctx) {
    const r = this.remoteLive;
    if (!r || !r.pts || r.pts.length < 1 || r.page !== this.pageIndex) return;
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    ctx.fillStyle = rgbCss(r.color, r.opacity);
    ctx.strokeStyle = rgbCss(r.color, r.opacity);
    drawInkStroke(ctx, r.pts, r.width, null);
    ctx.restore();
  }

  _drawLive(ctx) {
    this._drawRemoteLive(ctx);
    const a = this.active;
    if (!a || (a.tool !== "pen" && a.tool !== "highlighter")) return;
    const flat = a.tool === "highlighter";
    const press = this._hasPressureFor(a) ? a.press : [];
    let pts = a.pts, profile = null;
    if (this._straightMode) {
      // a settled shape is drawn as it is — denoising a recognised rectangle
      // would round the corners the dwell just gave it
    } else if (this.pen.live_smooth) {
      // The same three jobs run LIVE, so the line under the nib is the line you
      // are left with. Its cost is the TAIL re-settling on each report — which
      // is why this is a switch and not a default nobody chose.
      const out = liveInkStroke(a.pts, press, this.pen.smoothing, { flat });
      pts = out.pts;
      profile = out.profile;
    } else if (!flat && press.length) {
      // off, the raw polyline never moves once drawn, and re-forms on release
      pts = a.pts;
    }
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    const color = flat ? this.pen.hl_color : this.pen.pen_color;
    const width = flat ? this.pen.hl_width : this.pen.pen_width;
    ctx.fillStyle = rgbCss(color, flat ? this.pen.hl_opacity : 1.0);
    ctx.strokeStyle = rgbCss(color, flat ? this.pen.hl_opacity : 1.0);
    drawInkStroke(ctx, pts, width, profile);
    ctx.restore();
  }

  /** A frame for the presenter mirror: the current page with its ink, fitted to
   * `w`x`h` device pixels on black.
   *
   * It keeps its OWN fit rather than copying the editor's view — that is the
   * whole point of a mirror: you can zoom in to work on a corner of a slide
   * while the audience still sees it whole. The live stroke is included, so ink
   * appears there as it is laid down rather than when the pen lifts. */
  async mirrorFrame(w, h) {
    if (!this.doc || w < 2 || h < 2) return null;
    const c = new OffscreenCanvas(w, h);
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#000";               // black surround, for a projector
    ctx.fillRect(0, 0, w, h);

    const scale = Math.min(w / this.pageW, h / this.pageH);
    const ox = (w - this.pageW * scale) / 2;
    const oy = (h - this.pageH * scale) / 2;

    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(scale, scale);
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, this.pageW, this.pageH);
    ctx.restore();

    // the page is re-rendered at the mirror's own scale rather than reusing the
    // editor's bitmap, which is fitted to a different window
    try {
      const page = new OffscreenCanvas(Math.max(1, Math.round(this.pageW * scale)),
                                       Math.max(1, Math.round(this.pageH * scale)));
      await this.doc.render(this.pageIndex, scale, page);
      ctx.drawImage(page, ox, oy);
    } catch { /* a page that will not render still shows its ink */ }

    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(scale, scale);
    for (const st of this.strokes) {
      ctx.fillStyle = rgbCss(st.color, st.opacity);
      ctx.strokeStyle = rgbCss(st.color, st.opacity);
      drawInkStroke(ctx, st.pts, st.width, st.profile);
    }
    const a = this.active;
    if (a && (a.tool === "pen" || a.tool === "highlighter")) {
      const flat = a.tool === "highlighter";
      const press = this._hasPressureFor(a) ? a.press : [];
      const live = this._straightMode ? { pts: a.pts, profile: null }
        : liveInkStroke(a.pts, press, this.pen.smoothing, { flat });
      const color = flat ? this.pen.hl_color : this.pen.pen_color;
      ctx.fillStyle = rgbCss(color, flat ? this.pen.hl_opacity : 1.0);
      ctx.strokeStyle = ctx.fillStyle;
      drawInkStroke(ctx, live.pts, flat ? this.pen.hl_width : this.pen.pen_width,
                    live.profile);
    }
    ctx.restore();
    return c.transferToImageBitmap();
  }

  /** The loop being drawn, in flight. */
  _drawLassoPath(ctx) {
    const a = this.active;
    if (!a || a.tool !== "lasso" || a.pts.length < 2) return;
    ctx.save();
    ctx.translate(this.offX, this.offY);
    ctx.scale(this.zoom, this.zoom);
    ctx.strokeStyle = "rgba(53, 132, 228, 0.9)";
    ctx.lineWidth = 1.2 / this.zoom;
    ctx.setLineDash([5 / this.zoom, 4 / this.zoom]);
    ctx.beginPath();
    ctx.moveTo(a.pts[0][0], a.pts[0][1]);
    for (const p of a.pts.slice(1)) ctx.lineTo(p[0], p[1]);
    ctx.closePath();
    ctx.stroke();
    ctx.restore();
  }

  /** The selection frame. A selection wears the LOOP it was drawn with; the
   * chip switches to the box, and the resize handles exist ONLY there — a
   * hit-test that outlives its painter is exactly how a frame drifts from what
   * a grab catches. */
  _drawSelection(ctx) {
    if (!this.hasSelection()) return;
    const box = this._selectionScreenBox();
    if (!box) return;
    const accent = "rgb(53, 132, 228)";
    ctx.save();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.2;

    if (!this.selectionBoxed && this.selectionLoop) {
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      const first = this.toView(this.selectionLoop[0][0], this.selectionLoop[0][1]);
      ctx.moveTo(first[0], first[1]);
      for (const p of this.selectionLoop.slice(1)) {
        const v = this.toView(p[0], p[1]);
        ctx.lineTo(v[0], v[1]);
      }
      ctx.closePath();
      ctx.stroke();
    } else {
      const [x0, y0, x1, y1] = box;
      const p = LASSO_PAD;
      ctx.setLineDash([4, 3]);
      ctx.strokeRect(x0 - p, y0 - p, (x1 - x0) + p * 2, (y1 - y0) + p * 2);
      ctx.setLineDash([]);
      ctx.fillStyle = "#ffffff";
      for (const [hx, hy] of lassoHandlePoints(x0, y0, x1, y1, p)) {
        ctx.beginPath();
        ctx.rect(hx - 4, hy - 4, 8, 8);
        ctx.fill();
        ctx.stroke();
      }
      drawRotateKnob(ctx, x0, y0, x1, accent);
      // …and the control points on top, since one sits over a resize handle
      const verts = [];
      for (const { verts: vs } of this._vertexShapes()) {
        for (const v of vs) verts.push(this.toView(v[0], v[1]));
      }
      if (verts.length) {
        const snapped = this._snapAtPoint
          ? this.toView(this._snapAtPoint[0], this._snapAtPoint[1]) : null;
        drawShapeVertices(ctx, verts, accent, snapped);
      }
    }
    ctx.restore();

    const [chx, chy] = lassoChipCentre(box[0], box[1], LASSO_PAD);
    drawLassoChip(ctx, chx, chy, this.selectionBoxed, accent);
    const [dlx, dly] = lassoDeleteCentre(box[0], box[1], LASSO_PAD);
    drawLassoDelete(ctx, dlx, dly);
  }

  /** The control points of the shape still under the pen, and the magnets
   * within reach of it.
   *
   * Only the targets IN REACH are painted. Every corner on the page lighting up
   * at once says "these are all in play", when what is true is that one of them
   * is about to catch the point in your hand. */
  _drawLiveVertices(ctx) {
    const a = this.active;
    if (!a || !this._straightMode || a.liveHeld === null
        || a.liveHeld === undefined) return;
    const accent = "rgba(53, 132, 228, 0.95)";
    const [hx, hy] = a.pts[a.liveHeld];
    const hit = vertexSnapRadius(this.cssW, this.cssH) / this.zoom;
    const near = [];
    for (const { verts } of (a.liveTargets || [])) {
      for (const v of verts) {
        if (Math.hypot(v[0] - hx, v[1] - hy) <= hit) near.push(this.toView(v[0], v[1]));
      }
    }
    if (near.length) {
      ctx.save();
      ctx.globalAlpha = 0.45;    // a magnet is a candidate, not a handle
      drawShapeVertices(ctx, near, accent);
      ctx.restore();
    }
    const own = shapeVertices(a.pts).map((v) => this.toView(v[0], v[1]));
    const snapped = this._snapAtPoint
      ? this.toView(this._snapAtPoint[0], this._snapAtPoint[1]) : null;
    if (own.length) drawShapeVertices(ctx, own, accent, snapped);
  }

  /** The glyph naming what the dwell recognised, at the shape's top-right —
   * so you can see what you are about to be left with before you lift. */
  _drawSnapLabel(ctx) {
    if (!this._snapLabel || !this._snapAt || !this.active) return;
    const [vx, vy] = this.toView(this._snapAt[0], this._snapAt[1]);
    ctx.save();
    ctx.font = "500 12px Cantarell, system-ui, sans-serif";
    const text = this._snapLabel;
    const w = ctx.measureText(text).width;
    const x = vx + 10, y = vy - 10;
    ctx.fillStyle = "rgba(53, 132, 228, 0.92)";
    ctx.beginPath();
    ctx.roundRect(x, y - 14, w + 12, 20, 10);
    ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.fillText(text, x + 6, y);
    // cairo's save/restore does not save the PATH, and neither does this one
    // in spirit: end a shared painter with a fresh path so the next arc cannot
    // join onto a stale current point
    ctx.beginPath();
    ctx.restore();
  }

  _drawZoomMarquee(ctx) {
    const a = this.active;
    if (!a || a.tool !== "zoom") return;
    const [x0, y0] = a.startView, [x1, y1] = a.lastView;
    ctx.save();
    ctx.strokeStyle = "rgba(53, 132, 228, 0.9)";
    ctx.fillStyle = "rgba(53, 132, 228, 0.12)";
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 4]);
    const x = Math.min(x0, x1), y = Math.min(y0, y1);
    const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x + 0.5, y + 0.5, w, h);
    ctx.restore();
  }

  /** Under a stylus the POINTER is hidden for drawing tools — an arrow trailing
   * the nib is what gives the lag away — but never under a mouse, where the
   * pointer is all the hand has. */
  _updateCursor() {
    const a = this.active;
    const drawing = a && (a.tool === "pen" || a.tool === "highlighter");
    if (drawing && a.device === "pen") this.el.style.cursor = "none";
    else if (a && a.tool === "pan") this.el.style.cursor = "grabbing";
    else if (a && a.tool === "move") this.el.style.cursor = "grabbing";
    else if (a && a.tool === "rotate") this.el.style.cursor = "grabbing";
    else if (a && a.tool === "text") this.el.style.cursor = "text";
    else this.el.style.cursor = "crosshair";
  }
}

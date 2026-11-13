# The ink pipeline

> What the pen writes, from sample to stored stroke.

> Split out of `CLAUDE.md`, which is the starting point and links here.
>
> **This is not a changelog, and it must not become one.** Keep only what is
> TRUE NOW and can be broken by accident: an invariant, a constraint the
> platform imposes, a trap. When behaviour changes, REPLACE the old text —
> never append the new alongside it. Delete anything that has stopped being
> load-bearing.
>
> The *why* — what was tried, what was measured, what was rejected — belongs
> in `ideas.csv`, one row per feature. Link to the row instead of retelling
> it here. Present tense about how it behaves, not past tense about how it
> got here.

- **The ink pipeline (row 139)** — what the pen writes goes through THREE jobs
  on commit, and the whole design is that they stay apart. Conflating two of
  them is what made fast handwriting shrink. `finish_ink_stroke()` is the one
  entry point and also holds the policy (a snapped shape and the highlighter
  opt out); both canvases call it, nothing else does the steps by hand.
  1. **`resample_ink` INTERPOLATES** — centripetal Catmull-Rom, walked at fixed
     arc length. Catmull-Rom because it *interpolates*: the curve passes
     through every point the pen reported, so filling a gap can never move the
     line off where you drew it. The walk also thins a slow stroke's clusters,
     so spacing stops depending on pen speed. **It always runs** — the
     "Smoothing" slider is the denoiser only, and turning interpolation off
     would just bring back the facets on a fast stroke.
  2. **`taubin_smooth` DENOISES** — λ shrink alternating with a μ inflate pass.
     It replaced a plain Laplacian, which is a *diffusion* and so destroys
     curvature: a loop of N samples lost `1 − 2f(1 − cos(2π/N))` of its radius
     per pass, i.e. −19% at 12 samples but −2% at 40, so the damage grew with
     how fast you wrote. **The Laplacian must be the MIDPOINT form**
     `c + f·((a+b)/2 − c)`, never `c + f·(a+b−2c)`: the 2× difference puts the
     eigenvalues in [0,2] instead of [0,4], and Taubin's λ/μ are only a
     low-pass filter on the former — with the doubled form the μ pass
     *amplifies* Nyquist. It passes the circle test while broken. λ is capped
     at 0.5 by stability, so `INK_SMOOTH_PAIRS` is the only knob that deepens
     the stopband. **A big zigzag is not jitter** — resampled it is a
     long-wavelength shape, and preserving it is the same property that keeps a
     fast "o" round, so test denoising against a smooth arc plus per-sample
     noise, never a zigzag.
  - **An assistant's drawing (`svg_to_strokes`, row 217) is the one ink that
    skips this pipeline, on purpose**: SVG geometry is exact, so there is no
    noise to denoise, and Catmull-Rom would round a rectangle's corners.
    `even_polyline` re-walks it at `SVG_STEP` keeping every vertex, and
    `width_profile` gives it the same taper the pen gets — the PRESS list
    means the same thing either way. `hand_wobble` is pinned at both ends,
    so a line aimed at a word still starts and ends on it.
  - **`InkPlayback` commits each stroke the moment it is drawn.** Nothing in
    storage or in the cached ink layer knows about the animation; only the
    stroke in flight is painted, on the path the pen's live stroke takes
    (`_playback` on both canvases). **Undo and redo call
    `finish_playbacks` first**, so Ctrl+Z partway through takes the whole
    drawing back, never the half already committed.
  - **The same three jobs run LIVE** (`live_ink_stroke`, row 143), so the line
    under the nib is the line you are left with. It skips exactly two steps of
    `finish_ink_stroke`, both because the stroke is not over: the raw capture
    (that file is one record per STROKE, so routing the live path through
    `finish_ink_stroke` spams it every frame) and `trim_light_tail` (mid-stroke
    the falling edge is just where the pen IS). The tail is the only place live
    and committed differ. Three things hold it up: a **snapped** stroke is
    exempt live exactly as at commit (`_straight_mode` — denoising a recognised
    rectangle rounds the corners the dwell just gave it); the **predicted tip
    is appended AFTER smoothing** (`lead=`), because `taubin_smooth` pins its
    endpoints and a guess run through the filter drags the last real samples
    onto it; and past `LIVE_SMOOTH_MAX_PTS` only the **tail** is re-shaped,
    since the pipeline is O(n) per motion event and `INK_MAX_POINTS` is 3000
    (the join needs no blending — resampling starts at the first point and
    denoising holds both endpoints, so head and tail meet at the sample they
    were split on). **Do not "fix" the re-indexing**: a new sample re-indexes
    nearly every resampled point while the *path* stays put, so a live-smoothing
    test must compare SHAPE — an index-aligned comparison fails against correct
    behaviour. `_live_stroke` is shared by class assignment (the sheet borrows
    the canvas's), so both modes get this at once; `_smoothing_now()` is the
    adapter for the one setting they hold differently.
    - **Its cost is the TAIL, and that is why there is a switch.** The line
      does not lag (the drawn tip sits exactly on the pen's latest sample) but
      the last stretch re-settles on every report — 0.21% of an x-height per
      sample at the median, 6.6% at worst, against exactly 0 for a raw
      polyline, which cannot move because appending a point never disturbs the
      points before it. At ~3 samples per x-height writing small, that reads as
      a wriggling tip. So the two modes fail in opposite directions and only
      the hand can choose: `live_smooth` ("Smooth while drawing") keeps both
      available, and the COMMITTED ink is identical either way. **Measuring the
      settled body is not measuring what the hand watches** — the pre-ship
      numbers excluded the tail as "expected to move", which is exactly where
      the user was looking.
  - **Every length here SCALES with the writing** (`ink_feature_size`,
    `adaptive_spacing`). They are all really "a fraction of a letter" and only
    looked like constants because they were tuned at one size: a fixed spacing
    is a fixed smoothing radius, which is a small share of large writing and a
    quarter of an x-height when writing small — so small writing was the only
    thing being averaged into mush. The measure is the **short side** of the
    bounding box (a cursive run's x-height is what must survive; its diagonal
    is just how long the word is), with a deliberately *small* diagonal
    fallback — at 0.15 it would win above ~6.6:1, which ordinary cursive
    exceeds, and the measure would then grow with word length.
  - **A DOT IS NOT A SHORT STROKE (row 144).** Two rules, both learned from
    captured taps after three futile rises of `INK_DOT_BOOST` — the multiplier
    was never the wrong *value*, it was the wrong knob. **The taper must not
    touch it**: a dot is nothing but endpoints, so `INK_TAPER_MIN` scales all
    of it, and capping the ramp's LENGTH (`INK_TAPER_FRAC`) does not help. That
    bit only the ≥3-sample path, because the too-short-to-resample branch
    passes `taper=False` — so the same tap painted **2.4× differently
    depending on how many samples the digitiser emitted**. **And its width must
    be CONSTANT along it**: the last sample of a tap reads ~0 pressure (the pen
    leaving the glass, not the shape), so per-point widths drew one end at the
    floor and the other at full boost, and on a mark of near-zero length an
    outline with two radii is a crescent with a bite out of it — a pac-man, not
    a dot. The profile is flattened to its PEAK. `trim_light_tail` cannot do
    this: a tap returns before it runs, and trimming two samples leaves nothing
    to draw. *ceiling: `INK_DOT_LEN` is FIXED at 5.0 units — the one length
    here not scaled to the writing — so a dot that slid past it gets nothing;
    unfixed because an i-dot that slid cannot be told from a t-crossbar by
    geometry.*
  3. **`width_profile` SHAPES** it. The end taper is capped at
     `INK_TAPER_FRAC` of the stroke as well as `INK_TAPER_LEN`: a ramp of fixed
     length is the *whole* of a short mark, which is why the dot on an "i" once
     came out at half width. A stroke's **`press` list is not raw
     pressure**: it is the finished per-point width factor in 0..1 with the end
     taper already folded in, painted as `width * factor`. One concept, not
     two — it lets a mouse stroke taper without a second flag, and makes "has a
     profile" mean exactly "is freehand" at render time. *ceiling: the taper is
     baked at commit, so changing `INK_TAPER_*` does not restyle existing ink.*
  - **`draw_ink_stroke` is THE ink painter** — all seven painters route through
    it (page render, live stroke, presenter mirror, both copy-render PNGs, both
    lasso glows, the text-page PDF export). With a profile it builds a closed
    OUTLINE and fills it once; per-segment strokes would double-darken where
    they overlap. `grow=` is how the lasso glow haloes a tapered stroke instead
    of wrapping a flat sausage round a thin tip.
  - **Pressure persists in the annot's `/Contents`** (`INK_PROFILE_TAG`),
    because a PDF ink annotation has ONE width — the old row 26 blocker.
    Sidemark reloads the taper; other readers see constant-width ink. Splitting
    a stroke into per-width-band annots was rejected: it costs the lasso, the
    eraser and the control points. Text sheet: a `press` key in the sidecar.
    Both guarded by a LENGTH match, so a mismatch loses the taper rather than
    shifting every width along the stroke.
  - **The smear trim is ASYMMETRIC, and that is the point.** `trim_light_tail`
    cuts the falling edge only. The two ends are opposite problems: the END is
    a real smear (the pen unloads before leaving the glass and trails into the
    next letter), while the START is already CLIPPED — the digitiser reports no
    contact until its own threshold is crossed, so the first sample already
    carries real pressure and the ink before it was never captured. A symmetric
    gate makes "the stroke starts too late" strictly worse. Never re-add a live
    per-sample gate.
  - **The pen's samples arrive COMPRESSED, and `motion_history()` is how the
    stroke gets them back (row 147).** GTK compresses POINTER motion to one
    event per frame, and a stylus is delivered as the logical pointer (row
    135), so the pen rides that path while a finger does not. Measured: the
    panel reports the pen at **133 Hz** and the canvas was seeing **30 Hz** —
    78% of every stroke discarded, which is the whole of what the pipeline
    called "undersampling" (spacing/feature 0.337 for the pen against 0.08 for
    touch). Both drawing routers walk the recovered trail into
    `current_stroke` *before* the event's own point. Two traps, both silent:
    the axes are **surface** coords while a gesture reports **widget** ones
    (offset taken from the event's own position, which is known in both), and
    `coord.time` is the **event** clock, not `GLib.get_monotonic_time()` — only
    the difference from the current event means anything, so `_note_sample`
    takes an `age_ms` and a frame's worth of samples never lands on one
    instant. It is guarded like the ink capture: extra samples are a bonus,
    and failing to get them must never cost the stroke. *This fixes SHAPE, not
    latency* — the newest sample is still only as fresh as the frame it came
    on, and 33.4 ms is two of them, so the canvas is also running at ~30 fps
    mid-stroke (an empty page manages 16.5 ms). Don't sell one as the other.
    `extras/device_rate.py` re-measures the raw rate below GTK.
  - **Latency: one recovery and one guess, kept apart.** `hover_lead_in` is
    free REAL data — a stylus is tracked in proximity, so the positions from
    just before contact are the ink that was otherwise lost; it walks backwards
    and stops at the first gap, or a pen swooping in from across the page draws
    its approach. `predict_point` is a GUESS and is confined to the screen:
    `_live_stroke()` adds the predicted tip, the commit path reads
    `current_stroke` which never contains it, so a bad guess flickers for one
    frame and can never reach the file. It extrapolates along an **arc**, not a
    tangent — out of the curve of an "o" a linear guess leaves the letter and
    is yanked back (8.6× the error at 40 ms). The guess is also damped across
    frames (`PREDICT_SMOOTH`), because it is rebuilt from scratch every motion
    event and consecutive guesses disagree by more than the pen moved; the
    **offset** is what gets damped, never the anchor, or the lag comes back.
    Both default OFF, and **prediction is settled: it cannot be the answer
    here (row 147).** The pen's end-to-end lag on this hardware is ~110 ms,
    measured two independent ways and shown to be UPSTREAM of the compositor
    (a hardware cursor plane lags the nib just as much), so it is not
    Sidemark's or Hyprland's to recover. Graded on 133 Hz ink, prediction
    recovers ~10% of the lag error at a 10–20 ms lead, ~0 at 40, and is
    NEGATIVE beyond — it makes things worse on a third of samples at best.
    Predicting 110 ms ahead means guessing the second half of a letter, which
    kinematics cannot know; a Kalman or learned model fits more parameters to a
    future that is not in the data. Don't build one. `PREDICT_SMOOTH_MS` is a
    TIME constant, never a per-event weight — as a weight it silently meant
    ~92 ms at 30 Hz and ~21 ms at 133, which is most of why prediction once
    measured as useless.
    Under a stylus the POINTER is hidden for drawing tools
    (`_hide_pointer`) — an arrow trailing the nib is what gives the lag away —
    but never under a mouse, where the pointer is all the hand has.
  - **Tune it on real ink, not on synthetic curves.**
    `SIDEMARK_CAPTURE_INK=<path>` appends every finished stroke's RAW samples
    (pre-interpolation) to a JSON-lines file, and `extras/ink_replay.py`
    replays, measures, sweeps and re-renders them. A record carries `pts`
    (document units, untimed) **and `samples`** — the same stroke in screen
    coords with a timestamp each, which is what `predict_point` sees live.
    Prediction is the one part of the pipeline that reads the CLOCK, so an
    untimed capture cannot grade it: the 41 strokes in `notes/` predate
    `samples` and can say nothing about the lead. `--predict` grades it, walking
    each stroke with the same window and EMA damping as the live path and
    scoring the arc against the no-prediction lag and against a linear
    extrapolation, broken down by curvature. Its ground truth is the
    digitiser's *later report*, so it measures how well the guess tracks the
    pen's path, **not** perceived latency — a prediction can score well here
    and still feel late. Its key statistic is
    **sample spacing ÷ feature size**: above ~0.25 the hardware is
    undersampling the writing and interpolation is carrying the result (write
    bigger); below ~0.06 the pen is oversampling and any roughness left is
    tremor, so denoising is the lever. That ratio is how to answer "is this
    fixable in software or is the hardware just too coarse?" — measure, don't
    guess. **Measured twice, and the second reading retired the first.**
    Before `motion_history` (row 147) the ratio was 0.337 — UNDERSAMPLED, ~3
    samples per x-height writing small — and the conclusion drawn was that
    small writing is an information limit and the denoiser must never be
    strengthened for it. That was a fact about the 30 Hz the canvas was
    *receiving*, not about the pen. At the pen's real rate the same hand
    writing SMALLER measures 0.091, "reasonable sampling", so **small writing
    is no longer information-limited** and that rule is void. What survives is
    the method: measure the ratio, don't guess. (Writing physically bigger
    still helps; zooming in still does not — the digitiser samples in physical
    space, so letter and spacing shrink together.)
  - **The denoiser is now near-inert, and that is expected.** Across its whole
    range the Taubin passes move the committed ink 0.11%→0.16% of an x-height
    (it was 0.44%→0.48% at 30 Hz): 0.0 and 1.0 differ by 0.05% of an x-height,
    which is nothing. It was never mainly removing tremor — it was cleaning up
    what INTERPOLATION invented between sparse samples, and there is little
    left to invent. The shaping is now done by `resample_ink`. Do not "fix"
    the slider by widening its range; if a stroke ever needs more filtering,
    the question is what changed about the sampling.

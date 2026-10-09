// The live-Markdown maths grammar, ported from sidemark.py.
//
// `\alpha` → α, `x^2` lifts, `a_i` drops, and the SOURCE text stays intact
// underneath — this is display only. Two spans render verbatim, with no
// symbols, scripts or emphasis inside them: inline `code` (as any Markdown
// viewer does) and `[[wiki links]]` (a link target must never be mangled).
//
// One thing the desktop version needs and this one does not: an index map.
// There, the rendered string replaces the source in a GtkTextBuffer, so a click
// has to be mapped back through `_symbolize_map` to land on the symbol you
// aimed at, and an edit spliced back onto the `\command` it came from. Here the
// source stays in the document and CodeMirror maps positions through its own
// decorations, so the map has no job. That is the single biggest simplification
// the substrate buys.

export const MD_SYMBOLS = {
  "\\sum": "Σ", "\\prod": "Π", "\\int": "∫",
  "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ",
  "\\epsilon": "ε", "\\zeta": "ζ", "\\eta": "η", "\\theta": "θ",
  "\\iota": "ι", "\\kappa": "κ", "\\lambda": "λ", "\\mu": "μ",
  "\\nu": "ν", "\\xi": "ξ", "\\pi": "π", "\\rho": "ρ",
  "\\sigma": "σ", "\\tau": "τ", "\\upsilon": "υ", "\\phi": "φ",
  "\\chi": "χ", "\\psi": "ψ", "\\omega": "ω",
  "\\Gamma": "Γ", "\\Delta": "Δ", "\\Theta": "Θ", "\\Lambda": "Λ",
  "\\Xi": "Ξ", "\\Pi": "Π", "\\Sigma": "Σ", "\\Phi": "Φ",
  "\\Psi": "Ψ", "\\Omega": "Ω",
  // ceiling: the RADICAL SIGN alone — no overbar, so `\sqrt x + 1` cannot say
  // where the root ends. A bar would have to be drawn; a glyph that shows you
  // a square root beats no glyph at all.
  "\\sqrt": "√",
  "\\infty": "∞", "\\approx": "≈", "\\neq": "≠", "\\propto": "∝",
  "\\leq": "≤", "\\geq": "≥", "\\le": "≤", "\\ge": "≥",
  "\\pm": "±", "\\times": "×",
  "\\div": "÷", "\\cdot": "·", "\\to": "→", "\\gets": "←", "\\mapsto": "↦",
  "\\in": "∈", "\\notin": "∉", "\\subset": "⊂", "\\supset": "⊃",
  "\\cup": "∪", "\\cap": "∩", "\\emptyset": "∅",
  "\\forall": "∀", "\\exists": "∃",
  "\\partial": "∂", "\\nabla": "∇",
  // Number sets, double-struck. Both spellings of each: the single letter is
  // what you reach for mid-sentence, the long name is what you can still find
  // when `\R` has slipped your mind. They are the only single-LETTER commands
  // in the table, and they are safe because a command runs to the first
  // non-letter: `\Real` is not `\R` followed by "eal".
  "\\R": "ℝ", "\\realnum": "ℝ",
  "\\N": "ℕ", "\\natnum": "ℕ",
  "\\Q": "ℚ", "\\ratnum": "ℚ",
  "\\Z": "ℤ", "\\intnum": "ℤ",
  "\\C": "ℂ", "\\compnum": "ℂ",
};

// LaTeX accents over a base symbol, rendered with Unicode combining marks:
// \hat{x} → x̂, \bar{x} → x̄, \tilde{x} → x̃, \vec{x} → x⃗.
export const MD_ACCENTS = {
  hat: "̂", bar: "̄", tilde: "̃", vec: "⃗",
  dot: "̇", ddot: "̈",
};

// Dirac notation takes an argument exactly as an accent does — `\ket{\psi}`,
// `\bra\phi`, `\ket 0` — so it rides the same regex; only what is put AROUND
// the argument differs. U+27E8/9 are the mathematical angle brackets.
export const MD_BRAKETS = { bra: ["⟨", "|"], ket: ["|", "⟩"] };

// THE SPACE THAT ENDS A COMMAND IS A TERMINATOR, NOT A GAP: you have to write
// `\alpha x` because `\alphax` is a different command, so rendering it puts a
// hole in the middle of "αx". Exactly one such space is eaten with the command
// it ended — a second one is a real space and survives, which is how you ask
// for one. It is eaten before ANY character, end of line included: the rule
// used to depend on what followed, so `\alpha a` closed up while `\alpha 1`
// and `\alpha +` did not — three lines that look alike rendering three ways,
// for a reason only the grammar could see. The cost is `\alpha + \beta`
// reading "α+ β", and it was accepted with eyes open (root ideas.csv row 164).
const SYMBOL_RE = /\\([A-Za-z]+)( )?/g;
// The command WITHOUT its terminating space. This is the span the caret has to
// be in for the expression to stay open: the space is not part of what you are
// writing any more, so the caret standing after it means you are done and the
// glyph belongs on screen.
export const COMMAND_RE = /\\[A-Za-z]+/g;


const CODE_SPAN_RE = /`[^`\n]+?`/;
const LINK_RE = /(?<!!)\[\[([^\[\]\n]+?)\]\]/;
const SPAN_RE = new RegExp(CODE_SPAN_RE.source + "|" + LINK_RE.source, "g");

// Super/subscript: ^{content} or ^x  /  _{content} or _x.
// An unbraced script runs to the first NON-alphanumeric character (a leading
// sign is part of it, so `x^-1` reads as an exponent). The stop set
// deliberately includes `^` and `_` themselves: adjacent scripts (`a_i^2`,
// `a^t_i`) are two matches, each ending where the other begins. Group 4 is the
// terminating space of an UNBRACED script — you were forced to type it (`a_ib`
// subscripts "ib"), so it is eaten by exactly the rule a command's is: one
// space, whatever follows. A brace terminates by itself, so the braced form
// never claims one.
// The body is alphanumerics AND the glyphs a \command renders to: by the time
// this runs `x^\alpha` is already `x^α`, and α is not [A-Za-z0-9] — so the
// superscript silently did not happen. Every value in the symbol table is
// non-ASCII, so nothing Markdown or this grammar means by a character can end
// up in the class by accident. The terminating space is group 4's job, not the
// body's.
const SCRIPT_BODY = "[A-Za-z0-9"
  + Object.values(MD_SYMBOLS).sort().join("").replace(/[\\\]^-]/g, "\\$&")
  + "]";
const SCRIPT_RE = new RegExp(
  "(\\^|_)(?:\\{([^}]*)\\}|([+-]?" + SCRIPT_BODY + "+)( )?)", "g");

// An argument-taking command — the accents and `\bra`/`\ket`. Its name must
// END at a non-letter, as every command does: without the lookahead `\braket`
// read as `\bra` applied to "k", and `\hatch` as ĉh.
// AN UNBRACED ARGUMENT IS A SCRIPT BODY: the same class and the same eaten
// space (group 4) as an unbraced `^`/`_`, so `\ket 11` is |11⟩ and `\ket 0, x`
// is |0⟩, x. Matched on SYMBOLISED text, like a script: `\bra\phi A` is ⟨φA|,
// as `x^\phi A` superscripts both.
const ACCENT_NAMES = "\\\\(" + [...Object.keys(MD_ACCENTS), ...Object.keys(MD_BRAKETS)]
  .join("|") + ")(?![A-Za-z])\\s*";
const ACCENT_RE = new RegExp(
  ACCENT_NAMES + "(?:\\{([^}]*)\\}|([+-]?" + SCRIPT_BODY + "+)( )?)", "g");
// The same expression found in SOURCE, where an argument character may still
// be a known `\command` — with the space that terminates it, since by the time
// the desktop matches, that space is gone.
const KNOWN_COMMAND = "\\\\(?:" + Object.keys(MD_SYMBOLS).map((k) => k.slice(1))
  .sort((a, b) => b.length - a.length).join("|") + ")(?![A-Za-z]) ?";
const ACCENT_SRC_RE = new RegExp(ACCENT_NAMES + "(?:\\{([^}]*)\\}|([+-]?(?:"
  + SCRIPT_BODY + "|" + KNOWN_COMMAND + ")+)( )?)", "g");

export const MAX_SCRIPT_DEPTH = 3;   // beyond this the glyphs are unreadable
export const SCRIPT_SCALE = 0.65;    // each level shrinks by this much

// Anything that renders as something other than itself. Used to decide whether
// moving the caret along a line can change what is rendered on it — a line of
// plain prose cannot, so it costs no re-render.
export const RENDERABLE_RE = /[\\^_*`#]|\[\[|<!--/;

/** Split text into `{text, kind}` runs — kind in {text, code, link}. */
export function splitMarkup(text) {
  const out = [];
  let i = 0;
  SPAN_RE.lastIndex = 0;
  for (const m of text.matchAll(SPAN_RE)) {
    if (m.index > i) out.push({ text: text.slice(i, m.index), kind: "text", at: i });
    out.push({ text: m[0], kind: m[0][0] === "`" ? "code" : "link", at: m.index });
    i = m.index + m[0].length;
  }
  if (i < text.length || !out.length) {
    out.push({ text: text.slice(i), kind: "text", at: i });
  }
  return out;
}

/** One `\command` → its glyph, eating the space that terminated it.
 *
 * EVERY symbol, operators included: `\cdot a` is "·a" and `a \le b` is "a ≤b".
 * An operator was once given its LaTeX-style spacing back, and that was wrong —
 * you never chose that space either, and half the table behaving differently
 * from the other half is not a rule anybody can hold while writing. */
function subSymbol(whole, name) {
  const sym = MD_SYMBOLS["\\" + name];
  return sym === undefined ? whole : sym;   // unknown command — leave it alone
}

function subAccent(whole, name, braced, bare) {
  const base = braced !== undefined ? braced : (bare || "");
  if (name in MD_BRAKETS) return MD_BRAKETS[name][0] + base + MD_BRAKETS[name][1];
  const mark = MD_ACCENTS[name];
  if (!base) return mark;
  return base[0] + mark + base.slice(1);
}

/** Replace LaTeX-style `\commands` with their Unicode symbols (display only),
 * leaving the contents of `code` spans and `[[wiki links]]` untouched. */
export function symbolize(text) {
  let out = "";
  splitMarkup(text).forEach((seg) => {
    if (seg.kind !== "text") { out += seg.text; return; }
    const sym = seg.text.replace(new RegExp(SYMBOL_RE.source, "g"), subSymbol);
    // Accents run AFTER symbol substitution, so `\hat{\alpha}` → α̂: the inner
    // \alpha is already α by the time the accent is placed on it.
    out += sym.replace(new RegExp(ACCENT_RE.source, "g"), subAccent);
  });
  return out;
}

/** Where a script's content ends — the match end without the space it ate. The
 * next script nests only if it begins exactly here. */
export function scriptBodyEnd(m) {
  return m.index + m[0].length - (m[4] ? m[4].length : 0);
}

/** The text a script renders — braced or not. */
export function scriptContent(m) {
  return m[2] !== undefined ? m[2] : m[3];
}

/** Every `^`/`_` script in `text`, with the chain of kinds from the outermost
 * enclosing script down to this one.
 *
 * A script that starts exactly where the previous one's content ended is a
 * script OF that script — `a_i_j` is "j indexing i", not two indices of `a`
 * side by side, and `a_i^2` puts the 2 above the i. Anything between them ends
 * the chain, which is also how you write two scripts of the same base. */
export function iterScripts(text) {
  const re = new RegExp(SCRIPT_RE.source, "g");
  const out = [];
  let chain = [];
  let prevEnd = null;
  for (const m of text.matchAll(re)) {
    const kind = m[1] === "^" ? "sup" : "sub";
    if (prevEnd !== null && m.index === prevEnd && chain.length < MAX_SCRIPT_DEPTH) {
      chain = chain.concat([kind]);
    } else {
      chain = [kind];
    }
    prevEnd = scriptBodyEnd(m);
    out.push({ match: m, chain: chain.slice(), from: m.index,
               to: m.index + m[0].length, body: scriptContent(m) });
  }
  return out;
}

// How far a script is lifted or dropped, as a fraction of the base em.
//
// The desktop expresses this in Pango units (sup +4000, sub −2000) which are
// absolute, not relative to the font. CSS has no equivalent, so these are the
// same values converted at the font size they were chosen against (~11pt, 1024
// Pango units to the point): 4000/1024/11 ≈ 0.355 em and −2000/1024/11 ≈ −0.177
// em. The RATIO is what was actually tuned — a superscript rises twice as far
// as a subscript drops — and that survives exactly.
export const SUP_RISE_EM = 0.355;
export const SUB_RISE_EM = -0.177;

/** The (rise in em, font scale) for a nesting chain. Each level is placed
 * relative to the one it sits on and shrinks with it, so the `2` of `a_i^2`
 * lands at the top of the i rather than at the top of the a. */
export function scriptStyle(chain) {
  let rise = 0, scale = 1;
  for (const kind of chain) {
    rise += (kind === "sup" ? SUP_RISE_EM : SUB_RISE_EM) * scale;
    scale *= SCRIPT_SCALE;
  }
  return { rise, scale };
}

/** The font scale for a nesting chain. */
export function scriptScale(chain) {
  return Math.pow(SCRIPT_SCALE, chain.length);
}

/** The spans of a line that render as something other than themselves, as
 * `{from, to, caretTo, kind, ...}` in SOURCE coordinates — what the editor
 * turns into replace-decorations.
 *
 * Scripts are found first and their spans claimed, because a script's body is
 * ordinary text that must not also be symbol-substituted in place: `x^\alpha`
 * is one script whose rendered body is α.
 *
 * TWO ends, and they are not the same end. `to` is what gets REPLACED and
 * includes the space the expression ate; `caretTo` is what the editor tests
 * the caret against and does NOT. That space is not part of what you are
 * writing any more — you were forced to type it (`\alphax` is another command,
 * `x^2b` superscripts "2b"), so typing it means you are DONE. Test the caret
 * against `to` and the terminator you just typed holds the expression open
 * underneath it, and a second space is needed to let go of something already
 * finished. An unbraced accent argument eats one too, by the same rule. */
export function renderSpans(line) {
  const spans = [];
  const claimed = [];
  const overlaps = (a, b) => claimed.some(([x, y]) => a < y && b > x);

  for (const seg of splitMarkup(line)) {
    if (seg.kind !== "text") {
      // verbatim: no symbols, scripts or emphasis inside
      claimed.push([seg.at, seg.at + seg.text.length]);
    }
  }

  for (const s of iterScripts(line)) {
    if (overlaps(s.from, s.to)) continue;
    spans.push({
      from: s.from, to: s.to, caretTo: scriptBodyEnd(s.match),
      kind: "script", chain: s.chain,
      // the body renders too — `x^\alpha` lifts an α, not the word "\alpha"
      text: symbolize(s.body ?? ""),
    });
    claimed.push([s.from, s.to]);
  }

  // Accents before symbols: the argument is often a command itself
  // (`\ket{\psi}`, `\hat\alpha`), and a symbol claimed first would leave the
  // accent overlapping it and unrendered. The whole expression renders as one
  // span, its argument symbolised, exactly as the desktop composes the two.
  const accRe = new RegExp(ACCENT_SRC_RE.source, "g");
  for (const m of line.matchAll(accRe)) {
    const from = m.index, to = m.index + m[0].length;
    if (overlaps(from, to)) continue;
    // the caret end never includes the space that ended the argument — its
    // own, or the one its last \command ate
    spans.push({ from, to, caretTo: to - (m[2] === undefined && m[0].endsWith(" ") ? 1 : 0),
                 kind: "accent", text: symbolize(m[0]) });
    claimed.push([from, to]);
  }

  const symRe = new RegExp(SYMBOL_RE.source, "g");
  for (const m of line.matchAll(symRe)) {
    const from = m.index, to = m.index + m[0].length;
    if (overlaps(from, to)) continue;
    const glyph = MD_SYMBOLS["\\" + m[1]];
    if (glyph === undefined) continue;     // unknown command — leave it alone
    // the backslash and the letters, never the space m[2] may have eaten
    spans.push({ from, to, caretTo: from + 1 + m[1].length, kind: "symbol",
                 text: glyph });
    claimed.push([from, to]);
  }

  return spans.sort((a, b) => a.from - b.from);
}

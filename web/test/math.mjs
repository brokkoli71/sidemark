// Check the maths grammar port against vectors generated from sidemark.py.
//
//   extras/export_math_vectors.py > web/test/math-vectors.json
//   node web/test/math.mjs

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  MD_SYMBOLS, MD_ACCENTS, MD_BRAKETS, MAX_SCRIPT_DEPTH, SCRIPT_SCALE,
  symbolize, splitMarkup, iterScripts, scriptBodyEnd, scriptContent,
  renderSpans, RENDERABLE_RE,
} from "../src/mathrender.js";

const here = dirname(fileURLToPath(import.meta.url));
const V = JSON.parse(readFileSync(join(here, "math-vectors.json"), "utf8"));

let checks = 0, failures = 0;
function check(name, got, want) {
  checks++;
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) {
    failures++;
    if (failures <= 20) console.error(`  ✗ ${name}\n      got  ${g}\n      want ${w}`);
  }
}

check("symbol table", MD_SYMBOLS, V.symbols);
check("accent table", MD_ACCENTS, V.accents);
check("bra-ket table", MD_BRAKETS, V.brakets);
check("MAX_SCRIPT_DEPTH", MAX_SCRIPT_DEPTH, V.max_depth);
check("SCRIPT_SCALE", SCRIPT_SCALE, V.scale);

for (const c of V.cases) {
  const label = JSON.stringify(c.raw);
  check(`${label} symbolize`, symbolize(c.raw), c.symbolize);
  check(`${label} splitMarkup`,
    splitMarkup(c.raw).map((s) => [s.text, s.kind]), c.split);
  check(`${label} scripts`,
    iterScripts(c.raw).map((s) => ({
      from: s.from, to: s.to, body_end: scriptBodyEnd(s.match),
      content: scriptContent(s.match) ?? null, chain: s.chain,
    })),
    c.scripts.map((s) => ({ ...s, content: s.content ?? null })));
  check(`${label} renderable`, RENDERABLE_RE.test(c.raw), c.renderable);

  // A rendered span has TWO ends: `to` is replaced, `caretTo` is what holds
  // the source open under the caret. What separates them can only ever be the
  // one space the expression ate — anything else means the caret keeps hold of
  // text it is not touching, which is the bug where `x^2 ` needed a SECOND
  // space before it would render.
  const spans = renderSpans(c.raw);
  const bodyEnd = new Map(c.scripts.map((s) => [s.from, s.body_end]));
  for (const s of spans) {
    const gap = c.raw.slice(s.caretTo ?? s.to, s.to);
    check(`${label} @${s.from} caret end eats only a space`,
          gap === "" || gap === " ", true);
    // for scripts the oracle publishes the answer, so take it from there
    if (s.kind === "script" && bodyEnd.has(s.from)) {
      check(`${label} @${s.from} caret end`, s.caretTo, bodyEnd.get(s.from));
    }
  }
}

// The reported symptom, stated as behaviour: one space after an expression
// finishes it. `pos` is where the caret sits; a span is revealed as source only
// while the caret is within [from, caretTo].
const reveals = (line, pos) =>
  renderSpans(line).some((s) => pos >= s.from && pos <= (s.caretTo ?? s.to));
check("x^2 with the caret still on it", reveals("x^2", 3), true);
check("x^2 after ONE space", reveals("x^2 ", 4), false);
check("\\alpha after ONE space", reveals("\\alpha ", 7), false);
check("\\alpha with the caret still on it", reveals("\\alpha", 6), true);
// an accent eats nothing, so its two ends agree and the rule above is a no-op
check("\\hat{x} after ONE space", reveals("\\hat{x} ", 8), false);
check("\\hat{x} with the caret still on it", reveals("\\hat{x}", 7), true);
// the argument is often a command, and must not be claimed as a symbol first
check("\\ket{\\psi} renders as ONE span",
      renderSpans("\\ket{\\psi}").map((s) => s.text), ["|ψ⟩"]);
check("\\bra\\phi A after ONE space", reveals("\\bra\\phi A ", 11), false);
check("\\ket 11 with the caret still on it", reveals("\\ket 11", 7), true);
check("\\ket 11 after ONE space", reveals("\\ket 11 ", 8), false);

// what the editor draws is what the desktop renders, span by span
for (const c of V.cases) {
  const spans = renderSpans(c.raw);
  if (!spans.length || splitMarkup(c.raw).some((s) => s.kind !== "text")) continue;
  if (spans.some((s) => s.kind === "script")) continue;   // drawn, not text
  let out = "", at = 0;
  for (const s of spans) { out += c.raw.slice(at, s.from) + s.text; at = s.to; }
  out += c.raw.slice(at);
  check(`${JSON.stringify(c.raw)} spans compose to symbolize`, out, c.symbolize);
}

if (failures) {
  console.error(`\n✗ ${failures} of ${checks} maths checks failed.`);
  process.exit(1);
}
console.log(`✓ ${checks} maths checks passed over ${V.cases.length} lines.`);

// Anchors, callouts, and the legacy text boxes read as callouts.
//
// A `<!-- textbox:X:Y -->` written by an older desktop must still show and
// still move — as a callout sitting on its own anchor, rewriting its OWN
// marker so the older desktop can still read the file.
//
//   node web/test/anchors.mjs

import { parsePageMarks, moveCallout, addAnchor, coveredBy } from "../src/anchors.js";

let checks = 0, failures = 0;
function check(name, got, want) {
  checks++;
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) { failures++; console.error(`  ✗ ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
}

const notes = "<!-- textbox:120:300 -->\nHello\n\n<!-- anchor:10:10 -->\nA";
const marks = parsePageMarks(notes);
check("anchors first, legacy boxes after", marks.map((m) => m.textbox ?? null), [null, 0]);
check("an anchor keeps its own text", marks[0].text, "A");
const box = marks[1];
check("a legacy box is a callout on its anchor", box.callout, [box.x, box.y]);
check("a legacy box keeps its paragraph", box.text, "Hello");

const moved = moveCallout(notes, box, 150, 260);
check("moving a legacy box rewrites its own marker",
  moved.includes("<!-- textbox:150:260 -->"), true);
check("…and adds no callout marker", moved.includes("callout:"), false);
check("…and leaves the anchor alone", moved.includes("<!-- anchor:10:10 -->"), true);

const placed = parsePageMarks(addAnchor("", 50, 60, [80, 90]));
check("a new callout has text to type over", placed[0].text, "Text");
check("a bare anchor gets none", parsePageMarks(addAnchor("", 50, 60)).map((m) => m.text), [""]);

const rect = { x: 10, y: 10, w: 40, h: 20 };
check("an anchor inside its box is covered", coveredBy(rect, [20, 15]), true);
check("an anchor outside its box is not", coveredBy(rect, [5, 5]), false);
check("no box covers nothing", coveredBy(null, [20, 15]), false);

if (failures) {
  console.error(`✗ ${failures} of ${checks} anchor checks failed.`);
  process.exit(1);
}
console.log(`✓ ${checks} anchor checks passed (legacy text boxes, placeholders, covered dots).`);

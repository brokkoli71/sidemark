#!/usr/bin/env /usr/bin/python3
"""Dump the live-Markdown maths grammar as JSON conformance vectors.

What is rendered on screen is the whole feature, and the rules are full of
edges that look like details and are not: which space gets eaten, where an
unbraced script stops, when a script nests into the one before it, and the fact
that the end of a LINE counts as a terminator while the end of a code-span
segment does not. Each of those was a bug report once.

    extras/export_math_vectors.py > web/test/math-vectors.json
    node web/test/math.mjs
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SIDEMARK_TEST", "1")

import sidemark as S  # noqa: E402


CASES = [
    # plain prose renders as itself
    "Nothing to render here.",
    # the basic substitution, and the terminating space
    r"\alpha",
    r"\alpha x",
    r"\alpha  x",          # two spaces is how you ask for one
    r"\alpha + \beta",     # not a terminator: keeps its spacing
    r"\cdot a",            # an operator's space is eaten too
    r"a \le b",
    r"\alpha",             # at the end of a line
    r"\notacommand",       # unknown: left alone
    r"\R and \realnum",    # single-letter commands
    r"\Real",              # …which run to the first non-letter
    # scripts
    "x^2",
    "a_i",
    "a_i, b_j",            # stops at the first non-alphanumeric
    "a_ib",                # …so the space in `a_i b` was forced on you
    "a_i b",
    "x^-1",                # a leading sign is part of the script
    "x^{n+1}",
    "a_i^2",               # the 2 sits on the i
    "a_i_j",               # j indexes i
    "a^t_i",
    "a_i {}^2",            # how you write two scripts of one base
    # scripts and symbols together: the glyph a command rendered to IS a
    # script body, so `x^\alpha` superscripts the α it became
    r"x^\alpha",
    r"e^\lambda t",
    r"x_\mu",
    r"a_\alpha_j",         # …and chains like any other body
    r"x_i \alpha",         # a space you were never forced to type stays
    r"\sum_{i=0}^{n} x_i",
    # accents
    r"\hat{x}",
    r"\bar{x}",
    r"\vec{A}",
    r"\hat{\alpha}",       # accents run after substitution
    r"\dot x",
    r"\hatch",            # a command runs to the first non-letter
    # Dirac notation: an argument, like an accent, put between brackets
    r"\ket{\psi}",
    r"\bra{\phi} A \ket{\psi}",
    r"\ket 0",
    r"\ket 11",           # an unbraced argument is a script body…
    r"\ket 11 x",         # …eating its terminating space…
    r"\ket 0, x",         # …and stopping at punctuation
    r"\ket 11  x",        # two spaces is how you ask for one
    r"\hat xy",           # the mark sits on the first character
    r"\bra\phi A",         # \phi's space is gone first, so both are inside
    r"\braket{a|b}",      # not \bra applied to "k"
    # verbatim spans: nothing renders inside them
    r"`\alpha` and \beta",
    r"[[\alpha]] and \beta",
    r"[[file.pdf#page=3|the proof]]",
    r"![[lecture.pdf]]",   # the embed line is left alone
    # a code span makes the text before it a FRAGMENT — nothing terminated a
    # command at its end
    r"\alpha `code` \beta",
    # emphasis: the maths grammar wins, `_` is a subscript
    "*italic* and **bold**",
    "snake_case_name",
    r"\sqrt 2",            # the radical sign alone: no overbar to render
    r"\sqrt{x+1}",
    # comments
    "<!-- page:3 -->",
]


def main():
    out = []
    for raw in CASES:
        entry = {
            "raw": raw,
            "symbolize": S._symbolize(raw),
            "split": [[seg, kind] for seg, kind in S._split_markup(raw)],
            "scripts": [
                {
                    "from": m.start(),
                    "to": m.end(),
                    "body_end": S.script_body_end(m),
                    "content": S.script_content(m),
                    "chain": list(chain),
                }
                for m, chain in S.iter_scripts(raw)
            ],
            "renderable": bool(S._MD_RENDERABLE_RE.search(raw)),
        }
        out.append(entry)
    json.dump({"cases": out,
               "symbols": S._MD_SYMBOLS,
               "accents": S._MD_ACCENTS,
               "brakets": {k: list(v) for k, v in S._MD_BRAKETS.items()},
               "max_depth": S.MAX_SCRIPT_DEPTH,
               "scale": S.SCRIPT_SCALE},
              sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()

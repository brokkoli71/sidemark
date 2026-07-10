"""Auto-tier the test suite so `./run_tests.sh --fast` can skip the slow part.

Building a real window (PDFEditorWindow / Adw.Application / PresenterWindow)
dominates the suite's runtime; pure-logic and single-widget tests run in
seconds. Rather than hand-marking ~75 classes (which would rot), a test class
is marked `window` when its source references one of the window types.

Misclassification is harmless for *correctness* — every test still runs under
the headless compositor, so a "fast" test that turns out to need a window
still passes; it only lands in the wrong speed tier. The full suite
(no -m filter) is unaffected, as is CI's `python3 test_pdfeditor.py`.
"""
import inspect
import re

import pytest

_WINDOW_RE = re.compile(r"PDFEditorWindow|PresenterWindow|Adw\.Application")
_seen = {}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "window: builds real windows/apps — the slow tier, skipped by "
        "./run_tests.sh --fast (-m 'not window')")


def pytest_collection_modifyitems(config, items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None:
            continue
        if cls not in _seen:
            try:
                src = inspect.getsource(cls)
            except (OSError, TypeError):
                src = ""
            _seen[cls] = bool(_WINDOW_RE.search(src))
        if _seen[cls]:
            item.add_marker(pytest.mark.window)

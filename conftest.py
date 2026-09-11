"""Auto-tier the test suite so the default `./run_tests.sh` can skip the slow part.

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
import os
import re

import pytest

_WINDOW_RE = re.compile(r"PDFEditorWindow|PresenterWindow|Adw\.Application")
_seen = {}


def pytest_sessionstart(session):
    """Refuse to run against the user's real desktop session.

    A bare `pytest` inherits the live WAYLAND_DISPLAY, so the suite builds real
    windows on top of whatever the user is doing and — because `Bindings.save()`
    persists on every rebind — rewrites the button table of the app they
    actually use. `run_tests.sh` starts an isolated headless Weston and is the
    only thing that sets SIDEMARK_TEST_HARNESS.

    A rule in a document is a rule that gets missed; this one is enforced.
    Set SIDEMARK_ALLOW_BARE_PYTEST=1 to override deliberately — CI does, because
    it runs a bare pytest against a headless Weston of its own. Having a display
    is therefore NOT evidence of a real session, which is why the override is an
    explicit opt-in and not a guess about the environment."""
    if os.environ.get("SIDEMARK_TEST_HARNESS") or \
            os.environ.get("SIDEMARK_ALLOW_BARE_PYTEST"):
        return
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return          # no session to damage
    raise pytest.UsageError(
        "\n\nRefusing to run against your live desktop session.\n"
        "  Use  ./run_tests.sh            (isolated headless Weston)\n"
        "       ./run_tests.sh --full     (including the window tier)\n"
        "       ./run_tests.sh -k Name    (one class)\n"
        "A bare pytest would pop real windows over your work and rewrite the\n"
        "settings of the Sidemark you actually use.\n")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "window: builds real windows/apps — the slow tier, skipped by "
        "the default ./run_tests.sh (-m 'not window'); --full runs them)")


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Every test starts from the SHIPPED defaults.

    Settings are one file (`_settings_path`, under XDG_CONFIG_HOME — pointed at
    a throwaway dir by run_tests.sh) and `Bindings.save()` writes to it on every
    rebind. Without this, a test that binds a chord leaks its table into every
    window built after it, in file order: the failure is a test that passes
    alone and fails in a suite, blaming the wrong feature. Deleting the file
    between tests is enough — the table is only read when a window is built."""
    yield
    import sidemark
    try:
        os.unlink(sidemark._settings_path())
    except OSError:
        pass


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

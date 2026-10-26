# App lifecycle: reload, copies, autosave, logging

> Which copy is running, what survives a restart, and how a session is diagnosed.

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

- **Ctrl+R reloads the CODE, so everything else has to be written down (row
  157).** It spawns a standalone process, so `session_state()` records every
  tab that has a file, the page each is on, which was active, and the window's
  view state; `--restore` hands the child a temp JSON file, which is **consumed**
  — read once and deleted whatever happens — so a stale session cannot come
  back twice. Neither an unreadable state nor a file deleted since is fatal: a
  poor reload beats a crash on startup. The divider position must be applied
  from a **LOW-priority idle**, because `_init_pane_position` is a realize-time
  idle applying the default 62% split and silently overwrites anything set
  before it. A tab with no file (an untitled blank) is dropped — there is
  nothing on disk to name — and multiple windows are out of scope: Ctrl+R
  replaces the window it was pressed in. It asks about **every dirty tab**, one
  at a time, bringing each to the front first: a reload replaces the whole
  window, so a tab you never looked at would lose its edits unmentioned, and a
  single "save all?" cannot offer "discard this one, keep that one".
  Cancelling any of them abandons the reload.
- Single-instance app (`Gio.Application`, `HANDLES_COMMAND_LINE`): a second
  launch forwards its argv to the primary, which opens the file as a tab in the
  last-used window (`_open_target`/`open_file_in_tab`). For manual testing
  always launch standalone: `SIDEMARK_STANDALONE=1 /usr/bin/python3
  sidemark.py [FILE]` (the env var sets `NON_UNIQUE` so it bypasses the running
  instance — Ctrl+R reload uses the same trick to re-read the code).

- **`--tmp` is a scratch document that closes without a word** — `--tmp` for a
  blank page, `--tmp --new-text` for paper. It is a MODIFIER on `--new`, not a
  third kind of document, and a named path beats it (a file you named is a file
  you care about). `_asks_to_save()` is the ONE predicate behind the tab close,
  the window close and the autosave skip — a throwaway leaves no recovery
  snapshot either, or the next launch nags about a page you threw away.
  `_new_blank_document` is the one place a launch flag makes a blank document,
  so the mark lands on the session the ☰ actions just created.

- **`sidemark --version` answers "WHICH copy is running?"** (row 188), and
  that is what it is for: two installs and a PATH is why edits appear not to
  land — an AUR `sidemark-git` in `/usr` and `install.sh`'s in `~/.local`,
  with `/usr/bin` first. It prints the running FILE, its date, the commit,
  how many files changed since it, and every other `sidemark` on PATH with an
  arrow at the winner. An installed copy has no `.git`, so `install.sh` and
  both PKGBUILDs stamp a `build.json` — the only moment the source's
  dirtiness is still knowable. A **pre-GTK fast path** like `--help`, because
  the question matters most when the app will not start.

- **A COPY of the app is a different app.** `_copy_key()` is the one answer to
  "is this the installed script or a checkout?" — `""` for an installed path,
  else a hash of the source path (`SIDEMARK_INSTANCE=<name>` forces one). It
  suffixes both the GApplication id AND `settings.json`, so smoke-testing a
  checkout can neither join the running instance nor rewrite the button table,
  pen width or font size of the app you actually work in. Recent files
  (`recent.json`) stay shared on purpose — a copy is a different app, not a
  different person. Answer that question in one place or the two drift.

- **Autosave snapshots only what CHANGED, and the default is the expensive
  answer (row 170).** Re-serialising the document costs ~500 ms on a long PDF
  — most of it re-creating every ink annotation, not the save itself — and it
  runs on the main loop, which the user sees as a UI that freezes for half a
  second and then catches up in a burst. Notes live in the `.md` sidecar and
  cannot change the PDF, so `_mark_dirty(pdf=False)` (the notes buffer, and
  ONLY the notes buffer) leaves `_pdf_dirty` alone and the tick skips
  `save_copy`. **The opt-out is opt-IN on purpose**: a snapshot is a
  data-safety feature, so a caller nobody has audited must cost a needless
  write, never a lost recovery. The snapshot is still written when there is
  none, because recovery reads `doc.pdf` and notes must never be half a pair.
- **Writing ink touches only the pages whose ink changed (row 193).**
  `_write_ink_annotations` compares each page's strokes against a record of
  what its annotations hold, keyed by page xref: equal is skipped, a grown
  page gets only the new strokes appended, anything else is rewritten. A full
  rewrite was seconds on a long inked PDF AND a leak — deleted annotations
  stay in the document until a garbage-collecting save, so every tick grew
  memory and the snapshot by the whole document's ink. Two things hold it up:
  the record is a **deep, frozen copy** (`_frozen_stroke`), because strokes
  are edited in place and a shared list would change with them and hide the
  edit; and it must read **exactly the fields the writer writes** — add a
  field to the annotation and not to `_frozen_stroke`, and a change to only
  that field never reaches the file. Anything that replaces `self.document`
  must either re-seed (`_note_ink_in_document`, as load and save do) or leave
  the record to be dropped by the identity check.

- **PyMuPDF 1.27.2.3 calls a debug benchmark on every `Annot.update()`**
  (`update_timing_test`, counting to 30,000 in pure Python, result discarded):
  144 ms of the 420 ms it takes to write one lecture's ink.
  `_defuse_pymupdf_timing_test()` replaces it at import. Verified to leave the
  written PDF byte-identical apart from the trailer's random `/ID`, which
  differs between any two saves anyway.
- Logging: `logger` writes a per-session file under `~/.cache/sidemark/logs/`,
  auto-deleted on clean exit, **kept when anything logged at WARNING or above**
  and pruned to `LOG_KEEP`. Warning, not error, is deliberate: every warning
  site here is something having gone wrong, and the diagnostics that matter
  most — a GTK critical, a stalled loop, a slow render pass — all belong to a
  session that then exits perfectly cleanly, so an error-only rule deleted the
  log of exactly the run worth reading.
  - **A FREEZE is a blocked main loop, and only a thread outside it can see one
    (row 169).** The symptom users report is typing that stops appearing and
    then lands all at once: nothing is lost, the events were QUEUED while the
    loop was busy, so nothing inside the app notices anything wrong.
    `_watchdog` is a daemon thread watching a `STALL_BEAT_MS` heartbeat; once
    the loop is `STALL_WARN_MS` late it samples the MAIN thread's Python stack
    (`sys._current_frames()`) until it returns, and reports the busiest frames.
    Naming the frame is the whole point — "stalled 400 ms" is the symptom the
    user could already see. Reports are rate-limited (`STALL_REPEAT_S`), since
    the shape to expect is work repeating on every keystroke, and the thread
    never touches GTK. `SIDEMARK_NO_WATCHDOG=1` switches it off. Do not start
    it in the suite: `_loop_beat` frozen at the end of one test's loop makes
    every later moment look like a hang, and a stray warning breaks any test
    counting `assertLogs` records — test the pieces instead.
  - **A GTK abort writes NOTHING by itself, so two hooks make it readable (row
    169).** `g_error` kills the process from C: no exception, no
    `sys.excepthook`, and a log that just stops mid-session — which is all the
    2026-08-14 crash left behind, and why `coredumpctl` was the only thing that
    could answer it. `faulthandler` (pointed at the log's own stream) writes
    the PYTHON frames at SIGABRT/SIGSEGV, which is what names the handler;
    `_glib_log_writer` copies GTK's message in beside them, with a stack and an
    immediate flush. Four platform facts hold it up, all measured, and each is
    the opposite of the obvious guess:
    - **`g_log_set_handler` cannot see them** — modern GTK logs through the
      STRUCTURED API, so `g_log_set_writer_func` is the only hook that catches
      both paths.
    - **The writer cannot suppress a crash**: glib aborts in the caller ABOVE
      it whatever it returns. It is purely additive, and delegates to
      `g_log_writer_default` so stderr and the journal are unchanged.
    - **`FLAG_FATAL` is not set on the way in** — fatality is decided after the
      writer returns, so an always-fatal critical arrives as a plain level 8.
      `_glib_log_kind` classifies on `LEVEL_ERROR`, and gives a critical a
      stack too.
    - **`g_log_set_writer_func` may be called ONCE per process**; the second
      call is itself a `g_error`, so `_install_glib_log_bridge` is guarded — an
      unguarded re-install aborts the app with exactly the crash the hook
      exists to explain.
    PyGObject hands the writer the raw `GLogField` array, so values are
    gpointers read with `ctypes` (`_glib_log_fields`); the structured path also
    carries `CODE_FILE`/`LINE`/`FUNC`, naming the GTK source line that gave up.
  - **The row 166 crash is FIXED, and it was never the previews'** — it is
    `get_iter_at_location` itself. Hand GTK a y BELOW a line's text and its hit
    test answers "the end of that line", computed as the line's RAW byte count
    but converted as a VISIBLE line index: on a line carrying invisible
    characters the index overshoots by exactly the hidden bytes and
    `gtk_text_iter_set_visible_line_index` calls `g_error` — the process is
    gone where it stands, no exception, which is why the log simply stopped.
    **In this buffer nearly every line has hidden characters**, so a hover was
    enough. `MarkdownNotesView.iter_at_buffer_xy` clamps the y into the band
    the paragraph's display ROWS occupy first, and **every pointer→iter hit
    test in the notes view and the sheet goes through it — a bare
    `get_iter_at_location` on this buffer is a bug.** Two traps: the band must
    not be measured from the end-of-line iter (it sits on the newline and
    reports a 0×0 rect, which rejects clicks that are plainly on the text), and
    the regression test runs in a SUBPROCESS, because a regression there does
    not fail an assertion, it takes the interpreter with it.

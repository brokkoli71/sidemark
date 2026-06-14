#!/usr/bin/python3
"""Probe: can GTK4 reliably detect press AND release of the MX Master
thumb button (and other high-numbered buttons)?

Run with:  /usr/bin/python3 extras/probe_thumb.py
Press/release the thumb button over the window and watch stdout.

Three listeners run side by side:
  [legacy]  EventControllerLegacy — raw events, the ground truth for
            what GTK receives from the compositor.
  [click]   GestureClick(button=0) — pressed/released signals.
  [single]  GestureSingle(button=10) begin/end — what sidemark uses today.
  [drag]    GestureDrag(button=0) — present to reproduce sidemark's real
            gesture setup, since a competing claim is the suspected
            reason release detection failed.
"""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib


def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title="thumb button probe")
    win.set_default_size(500, 300)
    label = Gtk.Label(label="Press mouse buttons here.\nWatch terminal output.")
    win.set_child(label)

    legacy = Gtk.EventControllerLegacy()
    def on_legacy(_c, event):
        if event is None:  # PyGObject fails to marshal the signal arg — ask the controller
            event = _c.get_current_event()
        if event is None:
            return False
        t = event.get_event_type()
        if t in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
            kind = "PRESS  " if t == Gdk.EventType.BUTTON_PRESS else "RELEASE"
            print(f"[legacy] {kind} button={event.get_button()}")
        return False
    legacy.connect("event", on_legacy)
    win.add_controller(legacy)

    click = Gtk.GestureClick()
    click.set_button(0)
    click.connect("pressed",  lambda g, n, x, y: print(f"[click]  pressed  button={g.get_current_button()}"))
    click.connect("released", lambda g, n, x, y: print(f"[click]  released button={g.get_current_button()}"))
    click.connect("cancel",   lambda g, s:       print(f"[click]  CANCELLED"))
    win.add_controller(click)

    single = Gtk.GestureSingle()
    single.set_button(10)
    single.set_exclusive(True)
    single.connect("begin",  lambda g, s: print("[single] begin  (button 10)"))
    single.connect("end",    lambda g, s: print("[single] end    (button 10)"))
    single.connect("cancel", lambda g, s: print("[single] CANCELLED"))
    win.add_controller(single)

    drag = Gtk.GestureDrag()
    drag.set_button(0)
    def on_drag_begin(g, x, y):
        btn = g.get_current_button()
        print(f"[drag]   begin  button={btn}")
        if btn == 10:
            # the suspected fix: give up the sequence so the thumb
            # gestures keep it and receive a real release
            g.set_state(Gtk.EventSequenceState.DENIED)
            print("[drag]   DENIED sequence for button 10")
    drag.connect("drag-begin", on_drag_begin)
    drag.connect("drag-end",   lambda g, x, y: print(f"[drag]   end    button={g.get_current_button()}"))
    win.add_controller(drag)

    # candidate fix: a dedicated drag gesture for the thumb button —
    # movement is expected in a drag, so it should survive until release
    tdrag = Gtk.GestureDrag()
    tdrag.set_button(10)
    tdrag.connect("drag-begin",  lambda g, x, y: print("[tdrag]  begin   (button 10)"))
    tdrag.connect("drag-update", lambda g, x, y: print(f"[tdrag]  update  dx={x:.0f} dy={y:.0f}"))
    tdrag.connect("drag-end",    lambda g, x, y: print("[tdrag]  END     ← real release"))
    tdrag.connect("cancel",      lambda g, s:   print("[tdrag]  CANCELLED"))
    win.add_controller(tdrag)

    win.present()


app = Gtk.Application(application_id="de.hspitz.probe")
app.connect("activate", on_activate)
app.run()

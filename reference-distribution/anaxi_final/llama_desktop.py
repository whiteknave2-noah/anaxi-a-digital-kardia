"""
Anaxi -- Llama, desktop window. A native window wrapping the exact
same Gradio app llama_gui.py serves in a browser -- not a second
implementation, not a rewrite. Imports demo directly from llama_gui.py
and launches it non-blockingly, then points a native window at it.
Same respond() path, same database, same everything -- true by
construction, not by convention, since this file defines no interface
logic of its own at all.

LEGACY/NON-DEFAULT as of the desktop-launcher-simplification gate: the
production "Launch Anaxi.bat" shortcut now invokes llama_launch.py
(browser-first) instead of this file, because the pywebview native
window here had a recurring reliability problem (a centered red
`Error` while the backend itself stayed healthy). This file is left
completely unmodified and still works exactly as before when run
directly (`python llama_desktop.py --conversation`) -- it is simply no
longer the default entry point.

Setup: same folder and dependencies as llama_gui.py, plus:
    pip install pywebview

Run:
    python llama_desktop.py                  -- TASK mode (unchanged default)
    python llama_desktop.py --conversation   -- CONVERSATION mode

OWC6-G1: this file has no launch-mode parser of its own, deliberately
(spec section 7: "do not duplicate two independent mode parsers").
`sys.argv` is process-global, not scoped to whichever file happens to
be `__main__` -- so when this file is launched with --conversation,
that flag is equally visible to llama_gui.py's own `resolve_launch_
mode(sys.argv[1:])` call, which runs at import time below (`from
llama_gui import ...`), and resolves identically to a direct `python
llama_gui.py --conversation` launch. LAUNCH_MODE/INTERACTION_MODE_
LABEL are imported, not re-derived, so there is exactly one source of
truth for both entry points.

Opens a native window instead of a browser tab. The local Gradio
server still runs underneath it, same 127.0.0.1 address as always --
this window is a client of it, not a replacement for it. Closing the
window stops the app; there's no separate server process left behind.
"""

import webview

from llama_gui import demo, MODEL, LAUNCH_MODE, INTERACTION_MODE_LABEL, launch_production_backend

HOST = "127.0.0.1"
PORT = 7860
URL = f"http://{HOST}:{PORT}"


def main():
    # prevent_thread_lock=True is the documented way to launch Gradio
    # without it blocking the calling thread -- this call returns
    # immediately, server running in the background, so the window's
    # own event loop (webview.start(), which IS blocking) can take the
    # main thread afterward instead of the two fighting over it.
    try:
        # WSP2-P5: launch_production_backend() calls demo.launch() with
        # these exact kwargs, unchanged, then opportunistically starts
        # background Space activity -- see its own docstring in
        # llama_gui.py.
        launch_production_backend(
            server_name=HOST,
            server_port=PORT,
            prevent_thread_lock=True,
            inbrowser=False,
            show_error=True,
        )
    except OSError as e:
        print(f"Couldn't start on {URL} -- {e}")
        print(
            "Most likely cause: llama_gui.py or another llama_desktop.py "
            "window is still open and already holding this port. Close "
            "that window first, then try again."
        )
        return

    webview.create_window(f"Anaxi -- {MODEL} -- Interaction mode: {INTERACTION_MODE_LABEL}", URL)
    webview.start()  # blocks here until the window closes


if __name__ == "__main__":
    main()

"""Anaxi -- browser-first production launcher.

Desktop "Launch Clark" shortcut -> this file -> the exact same Gradio
app llama_gui.py/llama_desktop.py already serve, started the same way
llama_desktop.py already starts it (identical demo.launch() call) --
no new backend, no rewritten waking/roaming/private/Sleep wiring of
any kind. The only thing this file changes is the SURFACE: instead of
wrapping the server in a pywebview native window (llama_desktop.py,
currently unreliable -- a red `Error` window while the backend itself
stays healthy), this file waits for the server to actually become
ready and then opens it in the user's normal default browser.

`demo`/`MODEL`/`LAUNCH_MODE`/`INTERACTION_MODE_LABEL` are imported
from llama_gui.py, not re-derived -- exactly llama_desktop.py's own
established pattern (OWC6-G1: "do not duplicate two independent mode
parsers"). sys.argv is process-global, so `--conversation` passed to
THIS file is equally visible to llama_gui.py's own resolve_launch_
mode() call that runs at import time below.

The backend's lifetime is independent of the browser: demo.launch()
with prevent_thread_lock=True starts the server in a background
thread of THIS process; closing a browser tab is just a client
disconnecting an HTTP connection and has no way to reach back and stop
it. This process (not the browser) owns the backend, and is kept
alive by demo.block_thread() after the browser is opened -- closing
the browser leaves this process (and Clark) running exactly as before.

llama_desktop.py remains on disk, untouched, as a legacy/manual
fallback -- see its own module docstring.
"""
import runtime_roots
import json
import socket
import time
import urllib.error
import urllib.request
import webbrowser

from llama_gui import demo, MODEL, LAUNCH_MODE, INTERACTION_MODE_LABEL, launch_production_backend

HOST = "127.0.0.1"
PORT = 7860
URL = f"http://{HOST}:{PORT}"
CONFIG_URL = f"{URL}/config"

PROBE_TIMEOUT_SECONDS = 2
READINESS_TIMEOUT_SECONDS = 30
READINESS_POLL_INTERVAL_SECONDS = 0.5


def port_is_occupied(host=HOST, port=PORT, timeout=PROBE_TIMEOUT_SECONDS):
    """Localhost-only raw TCP check -- nothing here ever reaches
    outside 127.0.0.1. True if *something* is listening, regardless of
    what it is; callers use probe_gradio_server() separately to tell
    Clark's own server apart from an unrelated occupant."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def probe_gradio_server(url=CONFIG_URL, timeout=PROBE_TIMEOUT_SECONDS):
    """Localhost-only readiness probe -- `url` is always the literal
    127.0.0.1 config constant above, never external, never model-
    supplied. No model call of any kind is involved in reading a
    static Gradio config endpoint. Returns True only for a response
    that actually looks like Gradio's own /config JSON (both a
    "components" and a "version" key present) -- never merely "some
    HTTP server answered on this port." Returns False uniformly for
    "nothing is listening" and "something unrelated is listening";
    port_is_occupied() is how a caller tells those two apart."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read()
        data = json.loads(body)
        return isinstance(data, dict) and "components" in data and "version" in data
    except Exception:
        return False


def wait_for_readiness(probe=probe_gradio_server, timeout=READINESS_TIMEOUT_SECONDS,
                        interval=READINESS_POLL_INTERVAL_SECONDS, sleep_fn=time.sleep,
                        clock=time.monotonic):
    """Bounded polling loop -- never a fixed sleep as the sole
    readiness signal. Returns True the first moment `probe()` reports
    the real server responsive; False if `timeout` elapses first.
    `probe`/`sleep_fn`/`clock` are injectable purely for testing
    without a real bounded wall-clock wait or a real server."""
    deadline = clock() + timeout
    while clock() < deadline:
        if probe():
            return True
        sleep_fn(interval)
    return False


def start_backend():
    """Identical demo.launch() call to llama_desktop.py's own -- same
    host/port, same prevent_thread_lock/inbrowser/show_error -- no new
    or duplicated backend-startup logic. Never called when an existing
    healthy server was already detected on this port (see main()) --
    that already-running process handled its own startup independently
    when it was launched; a second call here would just be pointless
    (this process exits immediately after opening the browser tab).

    WSP2-P5: routed through llama_gui.py's own launch_production_
    backend() (demo.launch() with these exact kwargs, unchanged, then
    an opportunistic background-activity readiness step) rather than
    calling demo.launch() directly -- see that function's own docstring
    for why this is the single shared entrypoint every production
    launcher uses. This file itself still imports and names nothing
    from that lower background-activity layer -- only the one shared,
    already-existing function above."""
    try:
        launch_production_backend(
            server_name=HOST,
            server_port=PORT,
            prevent_thread_lock=True,
            inbrowser=False,
            show_error=True,
        )
        return True
    except OSError as e:
        print(f"Couldn't start Clark's backend on {URL} -- {e}")
        return False


def main():
    runtime_roots.refuse_if_redirected()  # a real session must never run on a redirected Workspace
    if port_is_occupied():
        if probe_gradio_server():
            print(f"Clark is already running and healthy at {URL} -- opening your browser "
                  f"there instead of starting a second backend.")
            webbrowser.open(URL)
            return
        print(
            f"Port {PORT} is already in use by something that does not look like Clark's "
            f"own server. Refusing to start a second backend or open a browser tab -- close "
            f"whatever is currently using port {PORT} and try again."
        )
        return

    if not start_backend():
        print("Clark's backend failed to start -- see the error above.")
        return

    if not wait_for_readiness():
        print(
            f"Clark's backend did not become ready at {URL} within "
            f"{READINESS_TIMEOUT_SECONDS} seconds. Not opening a browser tab to a page that "
            f"isn't there yet -- check the console output above for errors, or navigate to "
            f"{URL} yourself once it's ready."
        )
        demo.block_thread()
        return

    print(f"Clark is ready -- opening {URL} in your default browser "
          f"(mode: {INTERACTION_MODE_LABEL}, model: {MODEL}).")
    webbrowser.open(URL)
    demo.block_thread()  # keeps the backend running; the browser tab may be closed freely


if __name__ == "__main__":
    main()

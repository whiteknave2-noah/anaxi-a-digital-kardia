"""Opt-in REAL-MODEL check of the V1 audio observation path (OBSERVE; numpy/scipy features) through the
real ordinary waking seam: natural requests reach a whole-source observation of a synthetic track in an
isolated workspace (never the owner's collections), and the subject is told it received a mechanical
measurement, not audio perception.  Replies are printed for human review; no classifier judges them.
Enable with ANAXI_REAL_MODEL_HARNESS=1; run in its own pytest process."""
import os

import numpy as np
import pytest
import soundfile as sf

_real_ollama = None
try:
    import ollama as _real_ollama
except Exception:  # pragma: no cover
    pass

pytestmark = pytest.mark.skipif(
    os.environ.get("ANAXI_REAL_MODEL_HARNESS") != "1" or _real_ollama is None,
    reason="real-model harness is opt-in (ANAXI_REAL_MODEL_HARNESS=1) and needs the local models",
)

SR = 22050
REQUESTS = [
    "Could you observe the whole of Evening Study from the music collection, start to finish, and tell me what the observation shows?",
    "Would you take in a whole track from the music collection -- pick one -- and tell me what you actually received?",
]


def _track(seconds=90):
    """A synthetic piece: a slow chord progression with a regular pulse (no recording of anything)."""
    t = np.arange(int(SR * seconds)) / SR
    roots = [220.0, 174.61, 196.0, 164.81]
    y = np.zeros_like(t)
    for i, root in enumerate(roots):
        span = (t >= i * seconds / 4) & (t < (i + 1) * seconds / 4)
        for ratio in (1.0, 1.26, 1.5):
            y[span] += 0.15 * np.sin(2 * np.pi * root * ratio * t[span])
    pulse = (np.mod(t, 0.75) < 0.04).astype(float) * 0.4 * np.sin(2 * np.pi * 90 * t)
    return (y + pulse).astype(np.float32)


@pytest.mark.parametrize("text", REQUESTS)
def test_a_natural_request_reaches_a_whole_source_observation(monkeypatch, tmp_path, text):
    import workspace_capability as wc
    from test_wsp1_production_hard_floor import _build

    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True)
    sf.write(os.path.join(h.paths.music_dir, "Evening Study.wav"), _track(), SR)
    sf.write(os.path.join(h.paths.music_dir, "Morning Bell.wav"), _track(40)[::-1].copy(), SR)
    real_chat = _real_ollama.chat

    def chat(model, messages, format=None, options=None, think=None, **kwargs):
        h.calls.append({"model": model, "format": format, "options": options, "messages": [dict(m) for m in messages]})
        reply = real_chat(model=model, messages=messages, format=format, options=options, think=think, **kwargs)
        if format and not (options and options.get("num_predict") == 1):
            print("\n[raw]", reply["message"]["content"][:240].replace("\n", " "))
        return reply

    h.la.ollama.chat = chat
    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), text, interaction_mode="conversation")
    actions = [(r["resource_class"], r["action"], r["result"]) for r in wc.query_action_log(h.paths)]
    print("\n[actions]", actions, "\n[reply]", (result.get("reply") or "")[:900].replace("\n", " "))
    measured = [a for a in actions if a[0] == "music" and a[1] in ("observe", "listen", "inspect_audio") and a[2] == "performed"]
    assert measured, "the request never reached a genuine acoustic measurement"
    pass2 = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert "acoustic measurement" in pass2          # the epistemic status reached the subject

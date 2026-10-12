"""Regression: what a delivered music result ESTABLISHES is stated to the subject.

Live music checkout: after a genuine, successful listen (real decode, real mechanical views) the
subject narrated Albinoni's Adagio as heard -- "the cello's long, drawn-out notes", "the weight of
the harmony". ANAXI's music scope is mechanical/acoustic measurement; semantic or model-level
hearing is not established. Pass 2 was handed file facts and view names and nothing that said what
they are, so the action's own name ("listen") carried the meaning. The host now states the
epistemic status of the delivered representation (a host fact: it does not inspect, score, rewrite
or suppress what the subject then says), while every mechanical field is unchanged.
"""
import json
import os

import numpy as np
import pytest
import soundfile as sf

import workspace_audio as wa
import workspace_capability as wc
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message


def _tone(h, name="tone.wav"):
    samples = np.sin(2 * np.pi * 440 * np.arange(22050 * 2) / 22050).astype("float32") * 0.4
    sf.write(os.path.join(h.paths.music_dir, name), samples, 22050, format="WAV")


def _pass2(h):
    return "\n".join(m["content"] for m in h.calls[-1]["messages"])


@pytest.mark.parametrize("action,content", [("listen", ""), ("inspect_audio", json.dumps({"view": "waveform_envelope"}))])
def test_the_status_reaches_the_subject_at_the_real_waking_seam(monkeypatch, tmp_path, action, content):
    # realistic provider measurement (compress_probes): with byte-only accounting a waveform view
    # can exceed the room of this production-sized prompt and be withheld -- independent of this change.
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action={
        "resource_class": "music", "action": action, "relative_path": "tone.wav", "content": content,
    })
    _tone(h)

    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")

    text = _pass2(h)
    assert wa.AUDIO_DELIVERY_FRAMING in text                       # in the framing next to the task
    assert wa.AUDIO_EPISTEMIC_STATUS in text                       # and on the delivered result itself
    assert "not audio perception" in text and "how it sounds is inference" in text
    assert result["boundary_result"]["result"]["epistemic_status"] == wa.AUDIO_EPISTEMIC_STATUS


def test_the_mechanical_capability_is_intact(monkeypatch, tmp_path):
    """Nothing was removed or altered: every previous field is still delivered."""
    h = _build(monkeypatch, tmp_path, real_writer=True, action={
        "resource_class": "music", "action": "listen", "relative_path": "tone.wav", "content": "",
    })
    _tone(h)
    listen = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")["boundary_result"]["result"]
    for key in ("name", "extension", "size_bytes", "sha256", "duration_seconds", "sample_rate_hz", "channel_count",
                "decoded_interval_seconds", "available_views", "available_estimators"):
        assert key in listen, key
    assert listen["duration_seconds"] > 1.9 and listen["sample_rate_hz"] == 22050 and "waveform_envelope" in listen["available_views"]


def test_no_other_action_carries_the_music_framing(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True)          # ordinary library read
    h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")
    assert wa.AUDIO_DELIVERY_FRAMING not in _pass2(h)


def test_a_view_result_states_it_too(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    samples = np.sin(2 * np.pi * 440 * np.arange(22050 * 2) / 22050).astype("float32") * 0.4
    sf.write(os.path.join(paths.music_dir, "tone.wav"), samples, 22050, format="WAV")
    result, failure = wc.access_music_inspect_audio(paths, "tone.wav", json.dumps({"view": "dynamics"}))
    assert failure is None and result["epistemic_status"] == wa.AUDIO_EPISTEMIC_STATUS
    assert "not hearing" in result["epistemic_status"]


def test_the_status_is_a_fact_about_the_delivery_not_a_rule_about_expression():
    """It must never read as an instruction to, or a check of, the subject's words."""
    for text in (wa.AUDIO_EPISTEMIC_STATUS, wa.AUDIO_DELIVERY_FRAMING):
        lowered = text.lower()
        for forbidden in ("you must", "do not say", "never say", "must not", "should not", "you may not"):
            assert forbidden not in lowered, (forbidden, text)

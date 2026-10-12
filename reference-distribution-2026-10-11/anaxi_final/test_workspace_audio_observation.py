"""V1 acceptance tests for the richer audio observation path
(workspace_audio_observation.py + its wiring through capability,
direction, supervised delivery, and the waking seam). Synthetic fixtures
only -- no production music content, no network, no model calls."""
import json
import os

import numpy as np
import pytest
import soundfile as sf

import workspace_audio as wa
import workspace_audio_observation as wao
import workspace_capability as wc
import workspace_delivery as wdelivery
import workspace_direction as wd
from test_wsp1_production_hard_floor import PRODUCTION_HUMAN_BYTES, _build, _message

_SR = 8000


def _tone(freq_hz=440.0, duration_s=2.0, amplitude=0.5):
    t = np.linspace(0, duration_s, int(_SR * duration_s), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def _pulse_train(pulse_hz=2.0, duration_s=4.0, tone_hz=440, burst_s=0.05):
    t = np.linspace(0, duration_s, int(_SR * duration_s), endpoint=False)
    carrier = np.sin(2 * np.pi * tone_hz * t)
    period = 1.0 / pulse_hz
    envelope = (np.mod(t, period) < burst_s).astype(np.float32)
    return (0.8 * carrier * envelope).astype(np.float32)


def _write(tmp_path, name, samples):
    path = os.path.join(str(tmp_path), name)
    sf.write(path, samples, _SR, format="WAV")
    return path


def _observe(tmp_path, name, samples, source_name=None, probe_kwargs=None):
    path = _write(tmp_path, name, samples)
    decoded, failure = wa.decode_audio_bounded(path, **({"start_seconds": 0.0} if probe_kwargs is None else probe_kwargs))
    assert failure is None
    return wao.observe_audio_source(path, source_name or name, "wav", decoded)


def _assert_finite_json(text):
    """Every float in the JSON is finite: NaN/Infinity can never leak into a
    delivered observation (json.dumps would emit 'NaN'/undefined tokens)."""
    def _walk(node):
        if isinstance(node, float):
            assert np.isfinite(node), node
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
    _walk(json.loads(text))


# ------------------------------------------------------------ whole-source


def test_observe_whole_source_bounded_and_json_safe(tmp_path):
    observed, failure = _observe(tmp_path, "tone.wav", _tone(440, 2.0))
    assert failure is None
    assert observed["renderer"] == wao.AUDIO_OBSERVATION_V1
    _assert_finite_json(json.dumps(observed))       # JSON-safe by construction
    assert observed["epistemic_status"] == wa.AUDIO_EPISTEMIC_STATUS
    assert 1 <= len(observed["temporal_map"]) <= wao.MAX_TEMPORAL_MAP_SEGMENTS
    assert observed["analysis"]["mapping_level"] == "whole_source"
    assert observed["analysis"]["segments_planned"] == len(observed["temporal_map"])
    assert observed["analysis"]["silence_status"] == "audible"
    assert observed["duration_seconds"] == round(2.0, 3)


def test_observe_segment_count_is_deterministic_and_bounded(tmp_path):
    short, _ = _observe(tmp_path, "short.wav", _tone(220, 1.0))
    assert short["analysis"]["segments_planned"] == 1
    very_long, _ = _observe(tmp_path, "long.wav", _tone(110, 130.0))
    assert very_long["analysis"]["segments_planned"] == 2
    assert len(very_long["temporal_map"]) == 2
    # A source far longer than the per-call deep-analysis budget must not
    # spawn unbounded segments: K is clamped, and the deep pass stops at
    # the budget, leaving an honest, stated map-only remainder.
    huge, _ = _observe(tmp_path, "huge.wav", _tone(100, 300.0))
    assert huge["analysis"]["segments_planned"] <= wao.MAX_TEMPORAL_MAP_SEGMENTS
    assert huge["analysis"]["segments_map_only"] >= 1
    assert huge["analysis"]["segments_deep"] + huge["analysis"]["segments_map_only"] == huge["analysis"]["segments_planned"]
    assert any(seg["depth"] == "map" for seg in huge["temporal_map"])


def test_observe_measures_rhythm_truthfully(tmp_path):
    observed, failure = _observe(tmp_path, "pulse.wav", _pulse_train(pulse_hz=2.0, duration_s=4.0))
    assert failure is None
    # 2 Hz pulse train -> median inter-onset interval 0.5 s -> ~120 bpm.
    assert observed["tempo_estimate_bpm"] is not None
    assert abs(observed["tempo_estimate_bpm"] - 120.0) < 3.0
    assert observed["tempo_estimator_status"] == "estimate"


def test_observe_measures_spectral_content_mechanically(tmp_path):
    observed, failure = _observe(tmp_path, "low.wav", _tone(440, 2.0))
    assert failure is None
    row = observed["temporal_map"][0]
    assert row["depth"] == "deep"
    assert row["spectral_centroid_hz"] is not None and 350.0 <= row["spectral_centroid_hz"] <= 550.0
    assert row["harmonic_share"] is not None and row["harmonic_share"] > 0.9    # a pure tone is harmonic, not percussive
    assert row["percussive_share"] is not None and row["percussive_share"] < 0.1
    assert row["dominant_pitch_class"] == "A"    # 440 Hz is the concert A
    assert row["rms_amplitude"] > 0.3 and row["crest_factor"] is not None


def test_observe_pure_tone_reports_no_onset_tempo(tmp_path):
    observed, failure = _observe(tmp_path, "tone.wav", _tone(440, 2.0))
    assert failure is None
    row = observed["temporal_map"][0]
    assert row["onset_count"] == 0
    assert row["tempo_estimate_bpm"] is None
    assert row["estimator_status"] == "not_established"


def test_observe_near_silence_states_not_established(tmp_path):
    observed, failure = _observe(tmp_path, "silent.wav", _tone(440, 2.0, amplitude=1e-6))
    assert failure is None
    assert observed["analysis"]["silence_status"] == "near_silent"
    assert observed["analysis"]["segments_deep"] == 0
    assert observed["tempo_estimate_bpm"] is None
    assert observed["tempo_estimator_status"] == "not_established"


def test_observe_source_facts_reflect_the_real_file(tmp_path):
    path = _write(tmp_path, "named.wav", _tone(440, 2.0))
    decoded, failure = wa.decode_audio_bounded(path)
    observed, _ = wao.observe_audio_source(path, "named.wav", "wav", decoded)
    assert observed["sha256"] == decoded["sha256"]
    assert observed["size_bytes"] == decoded["size_bytes"]
    assert observed["decoder"].startswith("libsndfile")
    assert observed["name"] == "named.wav"


def test_features_are_computed_without_any_external_analysis_library():
    """The candidate's librosa dependency was never installed in production; the features are numpy/
    scipy.  Countermodel: a module that silently returned nulls without librosa would fail the value
    checks below."""
    import sys
    assert "librosa" not in sys.modules
    rng = np.random.default_rng(0)
    tone = wao._deep_features(_tone(440.0, 2.0), _SR)
    assert tone["dominant_pitch_class"] == "A" and tone["pitch_class_confidence"] > 0.5
    assert 400.0 < tone["spectral_centroid_hz"] < 480.0 and tone["spectral_flatness"] < 0.05
    assert tone["harmonic_share"] > 0.8
    noise = wao._deep_features((0.3 * rng.standard_normal(_SR * 2)).astype(np.float32), _SR)
    assert noise["spectral_flatness"] > 0.3 and abs(noise["spectral_centroid_hz"] - _SR / 4) < 0.1 * _SR / 4   # flat spectrum: centre of 0..Nyquist
    clicks = np.zeros(_SR * 4, dtype=np.float32)      # sparse impulses: broadband AND brief
    clicks[:: _SR] = 1.0
    assert wao._deep_features(clicks, _SR)["percussive_share"] > 0.8
    silent = wao._deep_features(np.zeros(_SR, dtype=np.float32), _SR)
    assert silent["spectral_centroid_hz"] is None and silent["dominant_pitch_class"] is None
    assert "librosa" not in sys.modules


# ----------------------------------------------------------- interval view


def test_segment_observation_interval_subdivides_and_measures(tmp_path):
    path = _write(tmp_path, "tone.wav", _tone(440, 2.0))
    decoded, _ = wa.decode_audio_bounded(path)
    result, failure = wao.run_segment_observation(decoded, 0.5, 1.5)
    assert failure is None
    assert result["renderer"] == wao.AUDIO_SEGMENT_OBSERVATION_V1
    assert result["view"] == wa.SEGMENT_OBSERVATION_VIEW
    assert result["sub_segment_count"] == wao.SEGMENT_OBSERVATION_SUBSEGMENTS
    assert result["temporal_map"][-1]["end_seconds"] == round(1.5, 3)
    assert (result["temporal_map"][0]["start_seconds"], result["temporal_map"][-1]["end_seconds"]) == (round(0.5, 3), round(1.5, 3))


def test_segment_observation_invalid_interval_fails_closed(tmp_path):
    path = _write(tmp_path, "tone.wav", _tone(440, 2.0))
    decoded, _ = wa.decode_audio_bounded(path)
    result, failure = wao.run_segment_observation(decoded, 0.0, 999.0)
    assert result is None
    assert failure == wa.INSPECT_AUDIO_INVALID_INTERVAL


def test_inspect_audio_payload_accepts_the_observation_view():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": wa.SEGMENT_OBSERVATION_VIEW}))
    assert failure is None and payload["view"] == wa.SEGMENT_OBSERVATION_VIEW
    payload, failure = wa.parse_inspect_audio_payload("segment_observation")
    assert failure is None and payload["view"] == wa.SEGMENT_OBSERVATION_VIEW
    _payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "not_a_view"}))
    assert failure == wa.INSPECT_AUDIO_UNKNOWN_VIEW
    assert wa.SEGMENT_OBSERVATION_VIEW in wa.ALL_KNOWN_VIEWS
    assert len(wa.AVAILABLE_VIEWS) == 5   # the five pure-numpy views are untouched


# -------------------------------------------------------- delivery window


def _observe_boundary_result(observed):
    return {
        "action": "observe", "scope": "local_workspace/music",
        "boundary": "workspace.music.read_only", "consequence": "Read permitted; source remains unchanged.",
        "rationale": "x", "result": observed,
    }


def test_observation_window_trims_map_rows_not_the_profile(tmp_path):
    observed, failure = _observe(tmp_path, "tone.wav", _tone(440, 2.0))
    assert failure is None
    # A wide temporal map makes windowing genuinely reachable: profile and
    # renderer stay whole no matter how small the map gets.
    row = dict(observed["temporal_map"][0])
    wide = dict(observed, temporal_map=[dict(row) for _ in range(8)])
    wide["analysis"] = dict(observed["analysis"], segments_planned=8)
    windowed_k1 = wdelivery._window_observation(dict(wide), 1)
    room = len(wdelivery.render_observation(_observe_boundary_result(windowed_k1))) + 120  # fits ~1-3 rows, not 8
    fitted, info = wdelivery.fit_boundary_result(_observe_boundary_result(wide), room)
    assert info["delivery"] == "windowed"
    result = fitted["result"]
    assert result["renderer"] == observed["renderer"]
    assert result["profile"] == observed["profile"]                       # profile intact, never char-truncated
    assert result["epistemic_status"] == wa.AUDIO_EPISTEMIC_STATUS
    assert result["sha256"] == observed["sha256"]
    assert "temporal_map_window" in result and result["has_more"] is True
    assert result["temporal_map_window"]["total_segments"] == 8
    assert 1 <= len(result["temporal_map"]) == info["delivered_units"] < 8
    assert len(wdelivery.render_observation(fitted)) <= room + 1
    _assert_finite_json(json.dumps(fitted))                               # still JSON-safe


def test_observation_window_never_touches_other_shapes():
    # No regression: a list result still windows as a list, an audio view
    # (no temporal_map) still falls through to the whole/withheld rules.
    list_result = {
        "action": "list", "scope": "local_workspace/music",
        "boundary": "x", "consequence": "y", "rationale": "z",
        "result": {"entries": [{"name": "x" * 250}] * 3, "start_index": 0, "returned_count": 3},
    }
    windowed_k1 = wdelivery._window_list(dict(list_result["result"]), "music", 1)
    room = len(wdelivery.render_observation({**list_result, "result": windowed_k1})) + 10
    fitted, info = wdelivery.fit_boundary_result(list_result, room)
    assert info["delivery"] == "windowed"
    assert isinstance(fitted["result"]["entries"], list) and "temporal_map_window" not in fitted["result"]


# -------------------------------------------------------- capability wiring


def test_access_music_observe_delivers_through_the_capability(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _write(paths.music_dir, "tone.wav", _tone(440, 2.0))
    result, failure = wc.access_music_observe(paths, "tone.wav")
    assert failure is None
    assert result["renderer"] == wao.AUDIO_OBSERVATION_V1
    log = wc.query_action_log(paths)
    assert log[-1]["action"] == wc.OBSERVE and log[-1]["result"] == "performed"


def test_segment_observation_view_dispatches_via_inspect_audio(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    _write(paths.music_dir, "tone.wav", _tone(440, 2.0))
    result, failure = wc.access_music_inspect_audio(
        paths, "tone.wav", json.dumps({"view": wa.SEGMENT_OBSERVATION_VIEW, "start_seconds": 0.25, "end_seconds": 1.25}),
    )
    assert failure is None
    assert result["renderer"] == wao.AUDIO_SEGMENT_OBSERVATION_V1
    assert result["sub_segment_count"] == wao.SEGMENT_OBSERVATION_SUBSEGMENTS


def test_observe_flows_through_the_direction_dispatcher_with_provenance(tmp_path):
    paths = wc.WorkspacePaths(str(tmp_path / "workspace"))
    paths.ensure_exists()
    tracks = os.path.join(paths.music_dir, "Tracks")
    os.makedirs(tracks, exist_ok=True)
    _write(tracks, "tone.wav", _tone(440, 2.0))
    validated, failure = wd.validate_pass1_workspace_action(
        {"resource_class": "music", "action": "observe", "relative_path": "Tracks/tone.wav", "content": ""},
    )
    assert failure is None
    boundary_result, performed = wd.execute_workspace_action(paths, validated, "clark")
    assert performed is True
    result = boundary_result["result"]
    assert result["renderer"] == wao.AUDIO_OBSERVATION_V1
    assert wc.is_target_bearing(wc.MUSIC, wc.OBSERVE)
    # provenance rides with a target-bearing action exactly like listen.
    assert result["resource_provenance"]["source_folder"] == "Tracks"


# ------------------------------------------------------------ waking seam


def test_observe_reaches_the_subject_at_the_waking_seam(monkeypatch, tmp_path):
    h = _build(monkeypatch, tmp_path, real_writer=True, compress_probes=True, action={
        "resource_class": "music", "action": "observe", "relative_path": "tone.wav", "content": "",
    })
    samples = np.sin(2 * np.pi * 440 * np.arange(22050 * 2) / 22050).astype("float32") * 0.4
    sf.write(os.path.join(h.paths.music_dir, "tone.wav"), samples, 22050, format="WAV")

    result = h.la.run_waking_turn(h.la.AnaxiOrchestrator(), _message(PRODUCTION_HUMAN_BYTES), interaction_mode="conversation")

    delivered = result["boundary_result"]["result"]
    assert delivered["renderer"] == wao.AUDIO_OBSERVATION_V1
    assert delivered["epistemic_status"] == wa.AUDIO_EPISTEMIC_STATUS

    text = "\n".join(m["content"] for m in h.calls[-1]["messages"])
    assert wa.AUDIO_DELIVERY_FRAMING in text        # hard framing rides like listen/inspect_audio
    assert "temporal_map" in delivered or "temporal_map_window" in delivered
    assert json.dumps(delivered)  # JSON-safe end to end

def test_tempo_is_an_estimate_only_where_the_detector_can_resolve_a_regular_pulse():
    """Live 2026-09-24: a slow choral movement measured "400 bpm" in both halves -- median onset spacing on the
    detector's own 0.1 s floor.  Countermodel: any estimator that reports a number for envelope ripple."""
    regular = np.arange(0, 20, 0.5)                      # 120 bpm
    bpm, reason = wa.tempo_from_onsets(regular)
    assert reason is None and abs(bpm - 120.0) < 0.5
    ripple = np.arange(0, 20, 0.15)                      # the "400 bpm" artifact class
    assert wa.tempo_from_onsets(ripple) == (None, wa.tempo_from_onsets(ripple)[1])
    assert "resolution" in wa.tempo_from_onsets(ripple)[1]
    rng = np.random.default_rng(3)
    irregular = np.cumsum(rng.choice([0.25, 1.4], size=40))
    assert wa.tempo_from_onsets(irregular)[0] is None
    assert wa.tempo_from_onsets(np.array([0.0, 1.0]))[0] is None


def test_an_unresolvable_tempo_is_not_established_with_its_reason(tmp_path):
    rng = np.random.default_rng(5)
    t = np.linspace(0, 20, _SR * 20, endpoint=False)
    dense = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + 0.3 * rng.standard_normal(t.size))).astype(np.float32)
    observed, failure = _observe(tmp_path, "dense.wav", dense)
    assert failure is None and observed["tempo_estimator_status"] == "not_established"
    assert observed["tempo_estimate_bpm"] is None and observed["tempo_not_established_reason"]

"""CAP2F acceptance tests for workspace_audio.py -- the bounded public
acoustic substrate. Synthetic fixtures only (sine tones, pulse trains)
-- no production music content, no network, no model calls."""
import hashlib
import json
import os
import sys
import tempfile
import traceback

import numpy as np
import soundfile as sf

ANAXI_FINAL = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ANAXI_FINAL)

import workspace_audio as wa

TEST_ROOT = tempfile.mkdtemp(prefix="wa_test_")


def _write_wav(name, samples, sample_rate=22050):
    path = os.path.join(TEST_ROOT, name)
    sf.write(path, samples, sample_rate, format="WAV")
    return path


def _write_flac(name, samples, sample_rate=22050):
    path = os.path.join(TEST_ROOT, name)
    sf.write(path, samples, sample_rate, format="FLAC")
    return path


def _write_mp3(name, samples, sample_rate=22050):
    path = os.path.join(TEST_ROOT, name)
    sf.write(path, samples, sample_rate, format="MP3")
    return path


def _tone(freq_hz, duration_s=2.0, sample_rate=22050, amplitude=0.5):
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def _pulse_train(pulse_hz, duration_s=4.0, sample_rate=22050, tone_hz=440, burst_s=0.05):
    """Amplitude-modulated bursts at a regular rate -- a genuine,
    unambiguous rhythmic signal for onset detection, unlike a pure
    continuous tone (which has no real onsets to find)."""
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    carrier = np.sin(2 * np.pi * tone_hz * t)
    period = 1.0 / pulse_hz
    phase = np.mod(t, period)
    envelope = (phase < burst_s).astype(np.float32)
    return (0.8 * carrier * envelope).astype(np.float32)


# ------------------------------------------------------------------ decode


def test_decode_valid_wav_succeeds():
    path = _write_wav("valid.wav", _tone(220))
    decoded, failure = wa.decode_audio_bounded(path)
    assert failure is None
    assert decoded["sample_rate_hz"] == 22050
    assert decoded["channel_count"] == 1
    assert abs(decoded["duration_seconds"] - 2.0) < 0.01
    with open(path, "rb") as f:
        assert decoded["sha256"] == hashlib.sha256(f.read()).hexdigest()


def test_decode_nonexistent_file_fails_closed():
    decoded, failure = wa.decode_audio_bounded(os.path.join(TEST_ROOT, "nope.wav"))
    assert decoded is None
    assert failure == wa.AUDIO_MALFORMED


def test_decode_unsupported_extension_fails_closed():
    # CAP2F-P1: mp3 is now genuinely supported -- use a format
    # deliberately NOT in SUPPORTED_AUDIO_EXTENSIONS (ogg is a real
    # libsndfile-supported container, but not one of Alex's confirmed
    # public-library formats, so it must still fail closed here).
    path = os.path.join(TEST_ROOT, "song.ogg")
    with open(path, "wb") as f:
        f.write(b"OggS fake ogg bytes")
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_UNSUPPORTED_FORMAT


def test_decode_malformed_wav_fails_closed():
    path = os.path.join(TEST_ROOT, "corrupt.wav")
    with open(path, "wb") as f:
        f.write(b"RIFF....WAVEfmt not actually valid pcm data at all")
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_MALFORMED


def test_decode_empty_file_fails_closed():
    path = os.path.join(TEST_ROOT, "empty.wav")
    with open(path, "wb") as f:
        pass
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_EMPTY


def test_decode_oversized_file_fails_closed():
    path = os.path.join(TEST_ROOT, "huge.wav")
    with open(path, "wb") as f:
        f.seek(wa.MAX_AUDIO_BYTES + 1)
        f.write(b"\x00")
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    # File size is no longer a global collection ceiling. This sparse file is
    # rejected because it is malformed, not merely because it is large.
    assert failure == wa.AUDIO_MALFORMED


def test_long_source_decodes_only_a_bounded_first_window():
    # A short WAV header describing an implausibly long duration would
    # need real bytes -- instead genuinely exceed MAX_AUDIO_DURATION_
    # SECONDS with a real (small-file, low-sample-rate) synthetic tone.
    sr = 4000
    duration = wa.MAX_AUDIO_DURATION_SECONDS + 5
    samples = _tone(100, duration_s=duration, sample_rate=sr)
    path = _write_wav("toolong.wav", samples, sample_rate=sr)
    decoded, failure = wa.decode_audio_bounded(path)
    assert failure is None
    assert decoded["duration_seconds"] == duration
    assert decoded["decoded_interval_seconds"] == [0.0, wa.MAX_INSPECT_WINDOW_SECONDS]
    assert decoded["samples"].shape[0] == sr * wa.MAX_INSPECT_WINDOW_SECONDS


def test_decode_directory_masquerading_as_audio_rejected():
    path = os.path.join(TEST_ROOT, "fakedir.wav")
    os.makedirs(path, exist_ok=True)
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_MALFORMED


# ---------------------------------------------------------- causal assay


def test_causal_low_vs_high_frequency_changes_frequency_bands():
    low, _ = wa.decode_audio_bounded(_write_wav("low.wav", _tone(110)))
    high, _ = wa.decode_audio_bounded(_write_wav("high.wav", _tone(8000)))
    low_bands = wa.view_frequency_bands(low, 0, 2.0)["band_energy_share"]
    high_bands = wa.view_frequency_bands(high, 0, 2.0)["band_energy_share"]
    assert low_bands != high_bands
    # low tone's energy peak must sit in a materially lower band index
    assert low_bands.index(max(low_bands)) < high_bands.index(max(high_bands))


def test_causal_low_vs_high_frequency_reproducible():
    decoded, _ = wa.decode_audio_bounded(_write_wav("repro.wav", _tone(220)))
    a = wa.view_frequency_bands(decoded, 0, 2.0)
    b = wa.view_frequency_bands(decoded, 0, 2.0)
    assert a == b  # same waveform -> stable/reproducible measurement


def test_causal_slow_vs_fast_pulse_changes_onset_rhythm():
    slow, _ = wa.decode_audio_bounded(_write_wav("slow_pulse.wav", _pulse_train(1.0)))
    fast, _ = wa.decode_audio_bounded(_write_wav("fast_pulse.wav", _pulse_train(4.0)))
    slow_result = wa.view_onset_rhythm(slow, 0, 4.0)
    fast_result = wa.view_onset_rhythm(fast, 0, 4.0)
    assert fast_result["onset_count"] > slow_result["onset_count"]
    slow_bpm = slow_result.get("estimators", {}).get(wa.ESTIMATED_TEMPO_BPM)
    fast_bpm = fast_result.get("estimators", {}).get(wa.ESTIMATED_TEMPO_BPM)
    assert slow_bpm is not None and fast_bpm is not None
    assert fast_bpm > slow_bpm
    # estimator must be explicitly labeled, never presented as fact
    assert slow_result["estimators"]["estimator_status"] == wa.ESTIMATOR_STATUS_ESTIMATE
    assert fast_result["estimators"]["estimator_status"] == wa.ESTIMATOR_STATUS_ESTIMATE


def test_causal_quiet_vs_loud_changes_dynamics():
    quiet, _ = wa.decode_audio_bounded(_write_wav("quiet.wav", _tone(440, amplitude=0.05)))
    loud, _ = wa.decode_audio_bounded(_write_wav("loud.wav", _tone(440, amplitude=0.9)))
    quiet_dyn = wa.view_dynamics(quiet, 0, 2.0)
    loud_dyn = wa.view_dynamics(loud, 0, 2.0)
    assert loud_dyn["rms_amplitude"] > quiet_dyn["rms_amplitude"]
    assert loud_dyn["peak_amplitude"] > quiet_dyn["peak_amplitude"]
    assert loud_dyn["rms_dbfs"] > quiet_dyn["rms_dbfs"]


def test_causal_waveform_envelope_reflects_amplitude_change_over_time():
    sr = 22050
    silence = np.zeros(sr, dtype=np.float32)
    loud = _tone(440, duration_s=1.0, sample_rate=sr, amplitude=0.9)
    combined = np.concatenate([silence, loud])
    decoded, _ = wa.decode_audio_bounded(_write_wav("half_silent.wav", combined, sample_rate=sr))
    env = wa.view_waveform_envelope(decoded, 0, 2.0)["envelope_rms"]
    first_half = env[: len(env) // 2]
    second_half = env[len(env) // 2:]
    assert max(first_half) < 0.05
    assert max(second_half) > 0.3


# ------------------------------------------------- CAP2F-P1: format coverage


def test_flac_low_tone_decodes_with_low_frequency_dominance():
    path = _write_flac("flac_low.flac", _tone(110))
    decoded, failure = wa.decode_audio_bounded(path)
    assert failure is None
    with open(path, "rb") as f:
        assert decoded["sha256"] == hashlib.sha256(f.read()).hexdigest()
    bands = wa.view_frequency_bands(decoded, 0, 2.0)["band_energy_share"]
    low_band_index = bands.index(max(bands))
    assert low_band_index <= 2  # dominant energy sits in a low band


def test_flac_reproducible_and_lossless_lookalike():
    # FLAC is lossless -- decoded samples should closely match the
    # original synthetic PCM (small float rounding aside).
    original = _tone(220)
    decoded, _ = wa.decode_audio_bounded(_write_flac("flac_repro.flac", original))
    assert decoded["channel_count"] == 1
    assert np.allclose(decoded["samples"], original, atol=1e-3)


def test_flac_malformed_fails_closed():
    path = os.path.join(TEST_ROOT, "corrupt.flac")
    with open(path, "wb") as f:
        f.write(b"fLaC not actually valid flac stream data")
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_MALFORMED


def test_mp3_high_tone_decodes_with_high_frequency_dominance():
    low_path = _write_mp3("mp3_low.mp3", _tone(110))
    high_path = _write_mp3("mp3_high.mp3", _tone(8000))
    low_decoded, failure_low = wa.decode_audio_bounded(low_path)
    high_decoded, failure_high = wa.decode_audio_bounded(high_path)
    assert failure_low is None and failure_high is None
    low_bands = wa.view_frequency_bands(low_decoded, 0, 2.0)["band_energy_share"]
    high_bands = wa.view_frequency_bands(high_decoded, 0, 2.0)["band_energy_share"]
    # CAP2F-P1 section 7: MP3 is lossy -- test the STRUCTURAL property
    # (which band dominates), never sample-for-sample equality with
    # the original synthetic PCM.
    assert low_bands.index(max(low_bands)) < high_bands.index(max(high_bands))


def test_mp3_pulse_train_still_shows_onset_structure_despite_lossy_encoding():
    path = _write_mp3("mp3_pulse.mp3", _pulse_train(2.0))
    decoded, failure = wa.decode_audio_bounded(path)
    assert failure is None
    result = wa.view_onset_rhythm(decoded, 0, 4.0)
    # Lossy encoding may shift exact onset timing/count slightly, but a
    # clearly pulsed 2 Hz signal over 4 seconds must still show several
    # distinct onsets, not none/one.
    assert result["onset_count"] >= 4


def test_mp3_source_hash_reflects_actual_encoded_bytes_not_original_pcm():
    original = _tone(220)
    path = _write_mp3("mp3_hash.mp3", original)
    decoded, _ = wa.decode_audio_bounded(path)
    with open(path, "rb") as f:
        encoded_bytes = f.read()
    assert decoded["sha256"] == hashlib.sha256(encoded_bytes).hexdigest()
    # The lossy-encoded file's own bytes are NOT byte-identical to the
    # original synthetic PCM's own bytes -- proves this is a genuine
    # encoded-file digest, not a copy of the pre-encoding signal.
    assert encoded_bytes != original.tobytes()


def test_renamed_garbage_mp3_fails_closed():
    path = os.path.join(TEST_ROOT, "fake.mp3")
    with open(path, "wb") as f:
        f.write(b"this is not an mp3 file, just garbage bytes renamed")
    decoded, failure = wa.decode_audio_bounded(path)
    assert decoded is None
    assert failure == wa.AUDIO_MALFORMED


def test_small_compressed_long_source_remains_navigable_by_bounded_window():
    # A genuinely long signal, MP3-encoded -- must still be rejected on
    # DECODED duration even though the compressed file itself is small.
    sr = 8000
    duration = wa.MAX_AUDIO_DURATION_SECONDS + 10
    samples = _tone(220, duration_s=duration, sample_rate=sr)
    path = _write_mp3("long_compressed.mp3", samples, sample_rate=sr)
    compressed_size = os.stat(path).st_size
    assert compressed_size < wa.MAX_AUDIO_BYTES  # genuinely small on disk
    decoded, failure = wa.decode_audio_bounded(path)
    assert failure is None
    assert decoded["duration_seconds"] > wa.MAX_AUDIO_DURATION_SECONDS
    assert decoded["decoded_interval_seconds"] == [0.0, wa.MAX_INSPECT_WINDOW_SECONDS]
    later, later_failure = wa.decode_audio_bounded(path, start_seconds=500, end_seconds=510)
    assert later_failure is None
    assert later["decoded_interval_seconds"][0] == 500
    assert later["decoded_interval_seconds"][1] >= 509.9


# ------------------------------------------------------ bounded renderers


def test_render_audio_source_bounded_and_json_safe():
    decoded, _ = wa.decode_audio_bounded(_write_wav("bounded_src.wav", _tone(220, duration_s=30.0)))
    rendered = wa.render_audio_source("bounded_src.wav", "wav", decoded)
    text = json.dumps(rendered)
    assert len(text.encode("utf-8")) < 1024
    assert rendered["renderer"] == wa.AUDIO_SOURCE_RENDERER_VERSION
    for forbidden in ("mood", "genre", "instrument", "emotion", "caption", "transcript"):
        assert forbidden not in text.lower()


def test_render_audio_view_bounded_for_every_view():
    decoded, _ = wa.decode_audio_bounded(_write_wav("bounded_view.wav", _tone(220, duration_s=30.0)))
    for view_name in wa.AVAILABLE_VIEWS:
        result, failure = wa.run_inspect_audio(decoded, view_name, None, None)
        assert failure is None, view_name
        text = json.dumps(result)
        assert len(text.encode("utf-8")) < 2048, view_name


def test_two_materially_different_audio_produce_different_representations():
    a, _ = wa.decode_audio_bounded(_write_wav("diff_a.wav", _tone(110)))
    b, _ = wa.decode_audio_bounded(_write_wav("diff_b.wav", _tone(9000)))
    view_a, _ = wa.run_inspect_audio(a, wa.SPECTRUM, None, None)
    view_b, _ = wa.run_inspect_audio(b, wa.SPECTRUM, None, None)
    assert view_a["magnitude_normalized"] != view_b["magnitude_normalized"]


# --------------------------------------------------- INSPECT_AUDIO payload


def test_parse_inspect_audio_payload_valid():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "dynamics"}))
    assert failure is None
    assert payload == {"view": "dynamics", "start_seconds": None, "end_seconds": None}


def test_parse_inspect_audio_payload_valid_with_interval():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "spectrum", "start_seconds": 1.0, "end_seconds": 5.0}))
    assert failure is None
    assert payload["start_seconds"] == 1.0 and payload["end_seconds"] == 5.0


def test_parse_inspect_audio_payload_rejects_malformed_json():
    payload, failure = wa.parse_inspect_audio_payload("not json at all")
    assert payload is None
    assert failure == wa.INSPECT_AUDIO_MALFORMED_PAYLOAD


def test_parse_inspect_audio_payload_rejects_natural_language():
    payload, failure = wa.parse_inspect_audio_payload("please show me the spectrum")
    assert payload is None
    assert failure == wa.INSPECT_AUDIO_MALFORMED_PAYLOAD


def test_parse_inspect_audio_payload_rejects_unknown_view():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "mood_analysis"}))
    assert payload is None
    assert failure == wa.INSPECT_AUDIO_UNKNOWN_VIEW


def test_parse_inspect_audio_payload_rejects_extra_keys():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "dynamics", "extra": "nope"}))
    assert payload is None
    assert failure == wa.INSPECT_AUDIO_MALFORMED_PAYLOAD


def test_parse_inspect_audio_payload_rejects_non_numeric_interval():
    payload, failure = wa.parse_inspect_audio_payload(json.dumps({"view": "dynamics", "start_seconds": "zero"}))
    assert payload is None
    assert failure == wa.INSPECT_AUDIO_MALFORMED_PAYLOAD


def test_run_inspect_audio_rejects_invalid_interval():
    decoded, _ = wa.decode_audio_bounded(_write_wav("interval.wav", _tone(220, duration_s=5.0)))
    result, failure = wa.run_inspect_audio(decoded, "dynamics", 4.0, 1.0)  # end before start
    assert result is None
    assert failure == wa.INSPECT_AUDIO_INVALID_INTERVAL

    result2, failure2 = wa.run_inspect_audio(decoded, "dynamics", 0.0, 999.0)  # exceeds duration
    assert result2 is None
    assert failure2 == wa.INSPECT_AUDIO_INVALID_INTERVAL


def test_run_inspect_audio_default_interval_bounded_by_max_window():
    sr = 4000
    decoded, _ = wa.decode_audio_bounded(
        _write_wav("longish.wav", _tone(220, duration_s=wa.MAX_INSPECT_WINDOW_SECONDS + 30, sample_rate=sr), sample_rate=sr)
    )
    result, failure = wa.run_inspect_audio(decoded, "dynamics", None, None)
    assert failure is None
    start, end = result["interval_seconds"]
    assert end - start <= wa.MAX_INSPECT_WINDOW_SECONDS + 0.01


# -------------------------------------------------------------- no imports


def test_no_model_or_network_imports():
    with open(os.path.join(ANAXI_FINAL, "workspace_audio.py"), encoding="utf-8") as f:
        source = f.read()
    for forbidden in ("requests", "urllib", "socket", "torch", "tensorflow"):
        assert forbidden not in source.lower(), forbidden
    import ast
    tree = ast.parse(source)
    module_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.append(node.module)
    assert set(module_names) == {"av", "hashlib", "json", "os", "numpy", "soundfile", "scipy", "scipy.signal"}, module_names


ALL_TESTS = [
    test_decode_valid_wav_succeeds,
    test_decode_nonexistent_file_fails_closed,
    test_decode_unsupported_extension_fails_closed,
    test_decode_malformed_wav_fails_closed,
    test_decode_empty_file_fails_closed,
    test_decode_oversized_file_fails_closed,
    test_long_source_decodes_only_a_bounded_first_window,
    test_decode_directory_masquerading_as_audio_rejected,
    test_causal_low_vs_high_frequency_changes_frequency_bands,
    test_causal_low_vs_high_frequency_reproducible,
    test_causal_slow_vs_fast_pulse_changes_onset_rhythm,
    test_causal_quiet_vs_loud_changes_dynamics,
    test_causal_waveform_envelope_reflects_amplitude_change_over_time,
    test_flac_low_tone_decodes_with_low_frequency_dominance,
    test_flac_reproducible_and_lossless_lookalike,
    test_flac_malformed_fails_closed,
    test_mp3_high_tone_decodes_with_high_frequency_dominance,
    test_mp3_pulse_train_still_shows_onset_structure_despite_lossy_encoding,
    test_mp3_source_hash_reflects_actual_encoded_bytes_not_original_pcm,
    test_renamed_garbage_mp3_fails_closed,
    test_small_compressed_long_source_remains_navigable_by_bounded_window,
    test_render_audio_source_bounded_and_json_safe,
    test_render_audio_view_bounded_for_every_view,
    test_two_materially_different_audio_produce_different_representations,
    test_parse_inspect_audio_payload_valid,
    test_parse_inspect_audio_payload_valid_with_interval,
    test_parse_inspect_audio_payload_rejects_malformed_json,
    test_parse_inspect_audio_payload_rejects_natural_language,
    test_parse_inspect_audio_payload_rejects_unknown_view,
    test_parse_inspect_audio_payload_rejects_extra_keys,
    test_parse_inspect_audio_payload_rejects_non_numeric_interval,
    test_run_inspect_audio_rejects_invalid_interval,
    test_run_inspect_audio_default_interval_bounded_by_max_window,
    test_no_model_or_network_imports,
]


def main():
    passed, failed = 0, 0
    failures = []
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            tb = traceback.format_exc()
            failures.append((t.__name__, str(exc), tb))
            print(f"FAIL {t.__name__}: {exc}")
    print()
    print(f"TOTAL={len(ALL_TESTS)} PASSED={passed} FAILED={failed}")
    if failures:
        print()
        for name, msg, tb in failures:
            print(f"--- {name} ---")
            print(tb)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------- WMA (owner set contains WMA Lossless)


def _write_wma(name, samples, sample_rate=44100):
    """Synthesise a genuine ASF/WMA file with the same PyAV/FFmpeg stack the decoder uses."""
    import av
    import numpy as np
    path = os.path.join(TEST_ROOT, name)
    stereo = np.vstack([samples, samples]).astype("float32")
    container = av.open(path, "w", format="asf")
    stream = container.add_stream("wmav2", rate=sample_rate, layout="stereo")
    context = stream.codec_context
    context.bit_rate = 128000
    context.format = "fltp"
    context.sample_rate = sample_rate
    context.layout = "stereo"
    for start in range(0, stereo.shape[1] - 2047, 2048):
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(stereo[:, start:start + 2048]), format="fltp", layout="stereo")
        frame.sample_rate = sample_rate
        frame.pts = start
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return path


def test_wma_decodes_with_causal_frequency_structure_and_malformed_wma_fails_closed():
    import numpy as np
    seconds = 2
    rate = 44100
    t = np.arange(seconds * rate) / rate
    low = _write_wma("wma_low.wma", 0.4 * np.sin(2 * np.pi * 110 * t))
    high = _write_wma("wma_high.wma", 0.4 * np.sin(2 * np.pi * 8000 * t))
    low_decoded, failure_low = wa.decode_audio_bounded(low)
    high_decoded, failure_high = wa.decode_audio_bounded(high)
    assert failure_low is None and failure_high is None
    assert low_decoded["duration_seconds"] > 1.5
    low_bands = wa.view_frequency_bands(low_decoded, 0, 1.5)["band_energy_share"]
    high_bands = wa.view_frequency_bands(high_decoded, 0, 1.5)["band_energy_share"]
    assert low_bands.index(max(low_bands)) < high_bands.index(max(high_bands))

    garbage = os.path.join(TEST_ROOT, "garbage.wma")
    with open(garbage, "wb") as handle:
        handle.write(b"not an asf container at all" * 50)
    result, failure = wa.decode_audio_bounded(garbage)
    assert result is None and failure is not None

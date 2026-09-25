"""V1 richer audio observation: bounded host-side acoustic measurement.

Same boundary as workspace_audio.py (HOST PROVIDES TRANSDUCTION +
INSTRUMENTS, CLARK MAKES MEANING), extended in the dimension CAP2F
deliberately held back: whole-source and interval acoustic measurement
computed directly with numpy/scipy (spectral centroid/bandwidth/rolloff/
flatness, median-filter harmonic/percussive decomposition, pitch-class
distribution) plus existing
streaming-level mechanical stats. This module makes NO meaning: every
value is a number or a mechanical label ("estimate" for anything derived
through peak-picking/onset inference, "measured" for direct spectral
averages, "not_established" when the source gives no material to work
with), and no emotion/mood/genre/sentiment classifier exists or is
imported anywhere in ANAXI.

No external analysis library (final completion build, 2026-09-24): the
candidate's librosa dependency (plus numba/llvmlite) was not installed in
the production environment, and every feature it used is a few lines of
numpy/scipy -- same framing conventions, same fixed key set.  It never
touches a model,
the network, or the source filesystem beyond the single real_path it is
given (already contained/resolved by workspace_capability).

Both entry points analyze the WHOLE requested interval, then partition
it into a bounded number of segments (the temporal map), each carrying
its own fixed, bounded set of scalar features. Nothing unbounded is
produced -- no raw arrays, no spectrogram images, no audio bytes, no
samples, no NaN/Inf. Because the JSON blob is still a soft Pass-2
observation that may exceed one prompt's room, both result shapes carry
a windowable ``temporal_map`` list -- the key
workspace_delivery._window_observation trims whole segments from first;
profile/source/provenance/epistemic fields are never truncated.

Source length is NOT a computation ceiling: the streaming pass measures
the whole source cheaply (memory-bounded), while the deep spectral pass
runs on at most MAX_TOTAL_DEEP_ANALYSIS_SECONDS of source per call;
segments beyond that budget still appear in the map at depth="map" with
truthful nulls for the deep fields. Deterministic: fixed segmentation,
no RNG, no sampling.
"""
import numpy as np
from scipy.signal import find_peaks

import workspace_audio as wa

# ------------------------------------------------------------------ bounds

OBSERVATION_TARGET_SEGMENT_SECONDS = 60.0     # desired wall-clock segment length
MAX_TEMPORAL_MAP_SEGMENTS = 8                 # K upper bound for the whole-source map
MAX_TOTAL_DEEP_ANALYSIS_SECONDS = 240.0       # deep spectral budget per OBSERVE call
SEGMENT_OBSERVATION_SUBSEGMENTS = 8           # fixed sub-division for the interval view
OBSERVATION_SILENCE_RMS = 1e-3                # below this, the source is near-silent
OBSERVATION_LOW_ENERGY_FRACTION = 0.5         # "low energy" envelope boundary (fraction of mean)
_OBSERVATION_HOP_MS = 50.0                    # envelope hop for streaming statistics
_SF_BLOCK_FRAMES = 65536

AUDIO_OBSERVATION_V1 = "AUDIO_OBSERVATION_V1"
AUDIO_SEGMENT_OBSERVATION_V1 = "AUDIO_SEGMENT_OBSERVATION_V1"
ANALYSIS_VERSION = "v1"


_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

_DEEP_KEY_ORDER = (
    "spectral_centroid_hz", "spectral_bandwidth_hz", "spectral_rolloff_hz",
    "spectral_flatness", "harmonic_share", "percussive_share",
    "dominant_pitch_class", "pitch_class_confidence",
)
_DEEP_NULL_ROW = {key: None for key in _DEEP_KEY_ORDER}


def _f(value, digits):
    """Round a finite scalar, else None -- observations never emit
    NaN/Inf (a JSON-safety and a boundedness law at once)."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return round(value, digits)


def _hop_seconds(sample_rate):
    return max(1, int(sample_rate * _OBSERVATION_HOP_MS / 1000.0)) / float(sample_rate)


def _count_segments(duration_seconds):
    """Deterministic whole-source partition: K = round(duration/60),
    clamped to [1, MAX_TEMPORAL_MAP_SEGMENTS]."""
    k = int(round(duration_seconds / OBSERVATION_TARGET_SEGMENT_SECONDS))
    return max(1, min(MAX_TEMPORAL_MAP_SEGMENTS, k))


# -------------------------------------------------------------- streaming (A)


def _block_envelope(mono, hop_samples, out):
    """Append the per-hop RMS envelope of one mono block to ``out``."""
    n_hops = max(1, mono.shape[0] // hop_samples)
    usable = mono[: n_hops * hop_samples]
    out.extend(np.sqrt(np.mean(np.square(usable.reshape(n_hops, hop_samples)), axis=1)).tolist())
    if mono.shape[0] > usable.shape[0]:
        out.append(float(np.sqrt(np.mean(np.square(mono[usable.shape[0]:])))))


def _stream_with_soundfile(real_path):
    soundfile = __import__("soundfile")
    sample_rate = channel_count = None
    cursor = peak_abs = energy = 0.0
    envelope = []
    hop_samples = None
    try:
        with soundfile.SoundFile(real_path, mode="r") as source:
            if source.frames <= 0 or source.samplerate <= 0:
                return None, wa.AUDIO_EMPTY
            sample_rate, channel_count = int(source.samplerate), int(source.channels)
            hop_samples = max(1, int(sample_rate * _OBSERVATION_HOP_MS / 1000.0))
            while True:
                block = source.read(frames=_SF_BLOCK_FRAMES, dtype="float32", always_2d=True)
                if block.size == 0:
                    break
                mono = block.mean(axis=1).astype(np.float32)
                peak_abs = max(peak_abs, float(np.max(np.abs(mono))))
                energy += float(np.sum(np.square(mono)))
                _block_envelope(mono, hop_samples, envelope)
                cursor += mono.shape[0]
    except Exception:
        return None, wa.AUDIO_MALFORMED
    if sample_rate is None or cursor == 0:
        return None, wa.AUDIO_EMPTY
    return {
        "duration_seconds": cursor / float(sample_rate),
        "sample_rate_hz": sample_rate,
        "channel_count": channel_count,
        "peak_amplitude": float(peak_abs),
        "rms_amplitude": float(np.sqrt(energy / cursor)),
        "envelope": np.asarray(envelope, dtype=np.float64),
        "hop_seconds": hop_samples / float(sample_rate),
        "decoder": "libsndfile/stream",
    }, None


def _stream_with_pyav(real_path):
    try:
        av = __import__("av")
        container = av.open(real_path)
        stream = next(item for item in container.streams if item.type == "audio")
        sample_rate = int(stream.codec_context.sample_rate or 0)
        channel_count = int(stream.codec_context.channels or 0)
        codec_name = stream.codec_context.name
        if sample_rate <= 0 or channel_count <= 0:
            container.close()
            return None, wa.AUDIO_EMPTY
        layout_name = stream.codec_context.layout.name
        resampler = av.AudioResampler(format="fltp", layout=layout_name, rate=sample_rate)
        hop_samples = max(1, int(sample_rate * _OBSERVATION_HOP_MS / 1000.0))
        cursor = peak_abs = energy = 0.0
        envelope = []
        for frame in wa._iter_pyav_frames(container, stream):
            for converted in resampler.resample(frame):
                array = converted.to_ndarray()
                if array.ndim == 1:
                    array = array.reshape(1, -1)
                mono = array.mean(axis=0).astype(np.float32)
                peak_abs = max(peak_abs, float(np.max(np.abs(mono))))
                energy += float(np.sum(np.square(mono)))
                _block_envelope(mono, hop_samples, envelope)
                cursor += mono.shape[0]
        container.close()
        if cursor == 0:
            return None, wa.AUDIO_EMPTY
        return {
            "duration_seconds": cursor / float(sample_rate),
            "sample_rate_hz": sample_rate,
            "channel_count": channel_count,
            "peak_amplitude": float(peak_abs),
            "rms_amplitude": float(np.sqrt(energy / cursor)),
            "envelope": np.asarray(envelope, dtype=np.float64),
            "hop_seconds": hop_samples / float(sample_rate),
            "decoder": f"pyav/stream:{codec_name}",
        }, None
    except Exception:
        try:
            container.close()
        except Exception:
            pass
        return None, wa.AUDIO_MALFORMED


def _stream_whole_source(real_path, extension):
    """Memory-bounded whole-source streaming measurement. Duration is
    measured from samples the chosen reader genuinely returned -- never
    container metadata -- and the read spans the whole source by design.
    Decoder selection mirrors decode_audio_bounded()."""
    if extension == "wma":
        return _stream_with_pyav(real_path)
    if extension == "mp3":
        stats, failure = _stream_with_soundfile(real_path)
        return (stats, None) if failure is None else _stream_with_pyav(real_path)
    stats, failure = _stream_with_soundfile(real_path)
    if failure in (wa.AUDIO_MALFORMED, wa.AUDIO_DECODER_UNAVAILABLE):
        return _stream_with_pyav(real_path)
    return stats, failure


# ------------------------------------------------------------ deep features (B)


def _n_fft_for(length):
    """Largest power of two <= min(2048, length), or None if too short."""
    if length < 64:
        return None
    n = 1
    while n * 2 <= min(2048, length):
        n *= 2
    return int(n)


def _stft_magnitude(mono, n_fft, hop):
    """|STFT| (freq x frames): centered (zero-padded by n_fft//2) periodic-Hann frames -- the same
    framing convention as librosa.stft's default, implemented directly on numpy."""
    from scipy import fft as scipy_fft          # imported where used: the analysis stack loads only when
    from scipy.signal import get_window         # an observation actually runs
    pad = n_fft // 2
    y = np.pad(mono.astype(np.float32), pad, mode="constant")
    n_frames = 1 + (y.shape[0] - n_fft) // hop
    if n_frames < 1:
        return None
    window = get_window("hann", n_fft, fftbins=True).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop][:n_frames] * window
    # float32 end to end (scipy.fft keeps single precision): a 60 s segment's transient stays ~100 MB.
    return np.abs(scipy_fft.rfft(frames, axis=1)).T


def _chroma_distribution(power, freqs):
    """Pitch-class energy: each FFT bin from A0 (27.5 Hz) up is assigned to its nearest equal-tempered
    pitch class (A4 = 440 Hz); every frame is normalized by its maximum (as librosa's chroma norm=inf),
    then frames are averaged.  A mechanical distribution, never a key or harmony judgment."""
    usable = freqs >= 27.5
    if not np.any(usable):
        return None
    pitch = 12.0 * np.log2(freqs[usable] / 440.0) + 69.0
    classes = np.mod(np.rint(pitch).astype(int), 12)
    frames = np.zeros((12, power.shape[1]))
    for pc in range(12):
        frames[pc] = power[usable][classes == pc].sum(axis=0)
    peak = frames.max(axis=0)
    keep = peak > 0
    if not np.any(keep):
        return None
    return np.mean(frames[:, keep] / peak[keep], axis=1)


def _deep_features(mono, sample_rate):
    """Bounded spectral / harmonic / pitch-class features for one segment, computed directly with
    numpy/scipy (no external analysis library): direct spectral averages (measured) plus mechanical
    harmonic/percussive and pitch-class structure.  Fixed key set; values are None where the segment
    gave nothing to measure."""
    n_fft = _n_fft_for(mono.shape[0])
    if n_fft is None:
        return dict(_DEEP_NULL_ROW)
    hop = max(1, n_fft // 2)
    magnitude = _stft_magnitude(mono, n_fft, hop)
    if magnitude is None:
        return dict(_DEEP_NULL_ROW)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    totals = magnitude.sum(axis=0)
    voiced = totals > 1e-12
    deep = dict(_DEEP_NULL_ROW, chroma=None)
    if not np.any(voiced):
        return deep
    S = magnitude[:, voiced]
    weights = S / totals[voiced]
    centroid = (freqs[:, None] * weights).sum(axis=0)
    bandwidth = np.sqrt((weights * np.square(freqs[:, None] - centroid[None, :])).sum(axis=0))
    cumulative = np.cumsum(S, axis=0)
    rolloff = freqs[np.argmax(cumulative >= 0.85 * cumulative[-1][None, :], axis=0)]
    power = np.maximum(np.square(magnitude, dtype=np.float64), 1e-10)
    flatness = np.exp(np.mean(np.log(power), axis=0)) / np.mean(power, axis=0)
    del power

    # Median-filtering HPSS (Fitzgerald 2010, the method librosa.decompose.hpss implements): harmonic
    # = median across time, percussive = median across frequency, soft masks with power 2.
    from scipy.ndimage import median_filter
    kernel = 31
    harmonic = median_filter(magnitude, size=(1, kernel), mode="reflect")
    percussive = median_filter(magnitude, size=(kernel, 1), mode="reflect")
    h2, p2 = np.square(harmonic), np.square(percussive)
    denom = h2 + p2
    denom[denom == 0] = 1.0
    h_energy = float(np.sum(np.square(magnitude * h2 / denom)))
    p_energy = float(np.sum(np.square(magnitude * p2 / denom)))
    hpss_total = h_energy + p_energy

    deep.update({
        "spectral_centroid_hz": _f(float(np.mean(centroid)), 1),
        "spectral_bandwidth_hz": _f(float(np.mean(bandwidth)), 1),
        "spectral_rolloff_hz": _f(float(np.mean(rolloff)), 1),
        "spectral_flatness": _f(float(np.mean(flatness)), 4),
        "harmonic_share": _f(h_energy / hpss_total, 4) if hpss_total > 0.0 else None,
        "percussive_share": _f(p_energy / hpss_total, 4) if hpss_total > 0.0 else None,
    })
    distribution = _chroma_distribution(np.square(magnitude), freqs)
    if distribution is not None and float(np.sum(distribution)) > 0.0:
        total = float(np.sum(distribution))
        dominant = int(np.argmax(distribution))
        deep["chroma"] = [round(float(v / total), 4) for v in distribution]
        deep["dominant_pitch_class"] = _PITCH_CLASSES[dominant]
        deep["pitch_class_confidence"] = _f(float(distribution[dominant] / total), 4)
    return deep


# ------------------------------------------------------------ shared segment


def _onset_peaks(envelope, hop_seconds):
    if envelope.size < 3 or np.max(envelope) <= 1e-9:
        return np.array([])
    peaks, _ = find_peaks(envelope, height=0.25 * np.max(envelope),
                          distance=max(1, int(wa.ONSET_MIN_SPACING_SECONDS / hop_seconds)))
    if peaks.size == 0:
        return np.array([])
    return peaks * hop_seconds


def _tempo_from_peaks(peak_times):
    """The shared estimator rule (workspace_audio.tempo_from_onsets): None when not established."""
    return wa.tempo_from_onsets(peak_times)[0]


def _seg_row(index, start, end, rms, envelope_peak, low_energy,
             onset_count, tempo, deep):
    estimator_status = "estimate" if tempo is not None else "not_established"
    row = {
        "segment_index": int(index),
        "start_seconds": _f(start, 3),
        "end_seconds": _f(end, 3),
        "duration_seconds": _f(end - start, 3),
        "rms_amplitude": _f(rms, 4),
        "envelope_peak_rms": _f(envelope_peak, 4),
        "crest_factor": _f((envelope_peak / rms) if rms > 1e-9 else None, 3),
        "low_energy_proportion": _f(low_energy, 4),
        "onset_count": int(onset_count),
        "onset_rate_hz": _f((onset_count / (end - start)) if end > start else None, 4),
        "tempo_estimate_bpm": _f(tempo, 1),
        "estimator_status": estimator_status,
        "depth": "deep" if deep is not None else "map",
    }
    row.update(deep if deep is not None else dict(_DEEP_NULL_ROW))
    return row


# ------------------------------------------------------------------ OBSERVE


def observe_audio_source(real_path, name, extension, probe_decoded):
    """Whole-source acoustic observation (OBSERVE action). Returns
    (result, failure_kind). The temporal map covers the ENTIRE source at
    depth="map"; leading segments within the deep-analysis budget are
    additionally analyzed spectrally at depth="deep" (their exact count
    is stated truthfully). Every value is a host measurement -- Clark
    decides what it means."""
    stats, failure = _stream_whole_source(real_path, extension)
    if failure is not None:
        return None, failure
    envelope = stats["envelope"]
    hop_seconds = stats["hop_seconds"]
    duration = stats["duration_seconds"]
    rms = stats["rms_amplitude"]
    near_silent = bool(rms < OBSERVATION_SILENCE_RMS)
    low_energy_threshold = OBSERVATION_LOW_ENERGY_FRACTION * (rms if rms > 1e-9 else 0.0)
    low_energy_global = float(np.mean(envelope < low_energy_threshold)) if envelope.size else 1.0

    k = _count_segments(duration)
    segment_len = duration / k
    frame_times = np.arange(1, envelope.size + 1, dtype=np.float64) * hop_seconds
    segment_index_of_frame = np.minimum(k - 1, (frame_times / segment_len).astype(np.int64))

    peak_times = _onset_peaks(envelope, hop_seconds)
    global_tempo = _tempo_from_peaks(peak_times)

    seg_envelope_peak = np.zeros(k, np.float64)
    seg_energy = np.zeros(k, np.float64)
    seg_frames = np.zeros(k, np.int64)
    seg_low = np.ones(k, np.float64)
    for s in range(k):
        env = envelope[segment_index_of_frame == s]
        if env.size:
            seg_envelope_peak[s] = float(np.max(env))
            seg_energy[s] = float(np.sum(np.square(env)))
            seg_frames[s] = int(env.size)
            seg_low[s] = float(np.mean(env < low_energy_threshold))

    # Leading segments whose whole span fits the deep-analysis budget get
    # spectral features; the rest stay map-only -- stated, never implied.
    deep_until_index = 0
    while deep_until_index < k:
        if (deep_until_index + 1) * segment_len <= MAX_TOTAL_DEEP_ANALYSIS_SECONDS + 1e-9:
            deep_until_index += 1
        else:
            break
    if near_silent:
        deep_until_index = 0

    temporal_map = []
    for s in range(k):
        start = s * segment_len
        end = (s + 1) * segment_len
        seg_mask = (peak_times >= start) & (peak_times < end)
        onset_count = int(np.sum(seg_mask))
        tempo = _tempo_from_peaks(peak_times[seg_mask])
        env = envelope[segment_index_of_frame == s]
        seg_rms = float(np.sqrt(seg_energy[s] / seg_frames[s])) if seg_frames[s] else 0.0
        deep = None
        depth = "deep" if s < deep_until_index else "map"
        if depth == "deep":
            segment_decoded, decode_failure = wa.decode_audio_bounded(real_path, start, end)
            if decode_failure is None:
                mono = wa._mono(segment_decoded["samples"])
                deep = _deep_features(mono, segment_decoded["sample_rate_hz"])
        temporal_map.append(_seg_row(
            s, start, end, seg_rms, seg_envelope_peak[s], seg_low[s],
            onset_count, tempo, deep,
        ))

    crest = (stats["peak_amplitude"] / rms) if rms > 1e-9 else None
    dbfs = 20.0 * np.log10(rms) if rms > 1e-9 else None
    result = {
        "renderer": AUDIO_OBSERVATION_V1,
        "name": name,
        "extension": extension,
        "size_bytes": probe_decoded["size_bytes"],
        "sha256": probe_decoded["sha256"],
        "duration_seconds": _f(duration, 3),
        "sample_rate_hz": int(stats["sample_rate_hz"]),
        "channel_count": int(stats["channel_count"]),
        "decoder": stats["decoder"],
        "source_interval_seconds": [_f(0.0, 3), _f(duration, 3)],
        "epistemic_status": wa.AUDIO_EPISTEMIC_STATUS,
        "analysis": {
            "analysis_version": ANALYSIS_VERSION,
            "mapping_level": "whole_source",
            "segment_target_seconds": OBSERVATION_TARGET_SEGMENT_SECONDS,
            "max_segments": MAX_TEMPORAL_MAP_SEGMENTS,
            "segments_planned": k,
            "segments_deep": deep_until_index,
            "segments_map_only": k - deep_until_index,
            "deep_analysis_budget_seconds": MAX_TOTAL_DEEP_ANALYSIS_SECONDS,
            "silence_status": "near_silent" if near_silent else "audible",
        },
        "profile": {
            "peak_amplitude": _f(stats["peak_amplitude"], 4),
            "rms_amplitude": _f(rms, 4),
            "crest_factor": _f(crest, 3),
            "rms_dbfs": _f(dbfs, 2),
            "low_energy_proportion": _f(low_energy_global, 4),
        },
        "tempo_estimate_bpm": _f(global_tempo, 1),
        "tempo_estimator_status": "estimate" if global_tempo is not None else "not_established",
        **({"tempo_not_established_reason": wa.tempo_from_onsets(peak_times)[1]} if global_tempo is None else {}),
        "temporal_map": temporal_map,
    }
    return result, None


# ---------------------------------------------------------- interval view (B)


def run_segment_observation(decoded, start_seconds, end_seconds):
    """inspect_audio view ``segment_observation``: divide the requested
    interval into SEGMENT_OBSERVATION_SUBSEGMENTS equal windows and give
    each the full bounded feature set (spectral/harmonic/chroma/tempo).
    Interval resolution mirrors every other inspect_audio view (fail
    closed). Returns (result, failure_kind)."""
    try:
        resolved_start, resolved_end = wa._clamp_interval(
            decoded["duration_seconds"], start_seconds, end_seconds)
    except ValueError:
        return None, wa.INSPECT_AUDIO_INVALID_INTERVAL
    decoded_interval = decoded.get("decoded_interval_seconds", [resolved_start, resolved_end])
    resolved_start = max(resolved_start, decoded_interval[0])
    resolved_end = min(resolved_end, decoded_interval[1])
    if resolved_end <= resolved_start:
        return None, wa.INSPECT_AUDIO_INVALID_INTERVAL

    sample_rate = decoded["sample_rate_hz"]
    mono = wa._mono(decoded["samples"])
    decoded_start = decoded_interval[0]
    n = SEGMENT_OBSERVATION_SUBSEGMENTS
    sub_len = (resolved_end - resolved_start) / n
    rows = []
    for i in range(n):
        start = resolved_start + i * sub_len
        end = start + sub_len
        start_idx = int(round((start - decoded_start) * sample_rate))
        end_idx = int(round((end - decoded_start) * sample_rate))
        windowed = mono[max(0, start_idx):end_idx]
        envelope, hop_seconds = wa._envelope_for_onsets(windowed, sample_rate)
        peak_times = _onset_peaks(envelope, hop_seconds)
        onset_count = int(peak_times.size)
        tempo = _tempo_from_peaks(peak_times)
        rms = float(np.sqrt(np.mean(np.square(windowed)))) if windowed.size else 0.0
        envelope_peak = float(np.max(envelope)) if envelope.size else 0.0
        if rms > 1e-9:
            low_energy = float(np.mean(envelope < (OBSERVATION_LOW_ENERGY_FRACTION * rms))) if envelope.size else 1.0
        else:
            low_energy = 1.0
        silent = bool(rms < OBSERVATION_SILENCE_RMS)
        deep = None if silent else _deep_features(windowed, sample_rate)
        rows.append(_seg_row(i, start, end, rms, envelope_peak, low_energy,
                             onset_count, tempo, deep))
    result = {
        "renderer": AUDIO_SEGMENT_OBSERVATION_V1,
        "view": wa.SEGMENT_OBSERVATION_VIEW,
        "interval_seconds": [_f(resolved_start, 3), _f(resolved_end, 3)],
        "sub_segment_count": n,
        "sample_rate_hz": int(sample_rate),
        "channel_count": int(decoded["channel_count"]),
        "source_sha256": decoded["sha256"],
        "decoder": decoded.get("decoder"),
        "epistemic_status": wa.AUDIO_EPISTEMIC_STATUS,
        "analysis": {
            "analysis_version": ANALYSIS_VERSION,
            "mapping_level": "interval",
            "sub_segments": n,
        },
        "temporal_map": rows,
    }
    return result, None
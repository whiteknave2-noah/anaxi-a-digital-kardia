"""CAP2F: bounded public acoustic substrate.

HOST PROVIDES TRANSDUCTION + INSTRUMENTS. CLARK CHOOSES ATTENTION +
MAKES MEANING. This module owns exactly one boundary:

    validated source -> decoded bounded PCM -> requested acoustic view

It never decides which view matters, never runs every analyzer on
every track, and never promotes a measurement into a belief,
preference, emotion, or autobiographical memory (MEASUREMENT !=
INTERPRETATION -- see CAP2F spec section 2). It never sends audio to a
model -- there is no model-runtime client import anywhere in this file,
and none is needed: LISTEN/INSPECT_AUDIO produce bounded text/numeric context for
the existing waking pathways, exactly like any other workspace
action's own `result`.

FORMAT/SCALING AUDIT: WAV/FLAC/MP3 first use libsndfile.  The intended
collection also contains WMA Lossless and MP3 variants libsndfile does
not accept, so a pinned PyAV wheel supplies a bundled-codec fallback.
Both paths normalize only the requested interval to the same float32
sample shape and report which decoder actually ran.  Source duration
is never a collection-access ceiling: each operation reads at most
MAX_INSPECT_WINDOW_SECONDS, while later windows remain independently
selectable.  Metadata, waveform statistics, and estimates remain
mechanical representations, never semantic hearing.

FUTURE-SOURCE BOUNDARY (spec section 0): `decode_audio_bounded()`
takes already-resolved, already-validated bytes and a real file path
only to hand to scipy's own reader -- it has no path-escape logic of
its own (that is `resolve_workspace_path()`'s job, in workspace_
capability.py, exactly like every other resource class) and no
knowledge of "where the source came from" beyond needing real bytes on
disk today. A future, SEPARATELY AUTHORIZED source (e.g. a bounded
microphone capture) could hand this same function bytes staged to a
temp file, or a future variant could accept an in-memory buffer
directly, without altering the view/estimator functions below at all
-- they only ever consume decoded (samples, sample_rate, channel_count)
tuples, never a file path. This module implements NO microphone
enumeration, permission request, capture, or streaming logic of any
kind -- there is no code path here that could reach an input device.

VIEW REGISTRY (spec section 17): VIEW_FUNCTIONS is a plain dict from
view name to a pure `(samples, sample_rate) -> dict` function. Adding
a FUTURE, model-derived, PROVENANCE-DISTINCT instrument (transcription,
audio/text similarity, sound-event classification) means adding one
new entry here and to AVAILABLE_VIEWS -- never rewriting decode or the
LISTEN/INSPECT_AUDIO dispatch. None of those future instruments are
implemented in this gate; only mechanical, waveform-derived views and
the one explicitly-labeled tempo estimator exist here. No CLAP, no
YAMNet, no Whisper, no genre/mood classifier -- none of those imports
exist anywhere in this file.
"""
import hashlib
import json
import os

import numpy as np
import soundfile as sf
from scipy import fft as scipy_fft
from scipy.signal import find_peaks

try:
    import av
except ImportError:  # truthful runtime failure for compatibility-only formats
    av = None

# ------------------------------------------------------- format / size bounds

# CAP2F-P1: exactly Alex's confirmed real public-library formats --
# never widened merely because libsndfile also supports other
# containers/codecs (OGG, AIFF, etc.).
SUPPORTED_AUDIO_EXTENSIONS = frozenset({"wav", "flac", "mp3", "wma"})
MAX_AUDIO_BYTES = 60 * 1024 * 1024          # legacy diagnostic constant; not a collection-access ceiling
MAX_AUDIO_DURATION_SECONDS = 600            # legacy diagnostic constant; not a source-duration ceiling
MAX_INSPECT_WINDOW_SECONDS = 120            # INSPECT_AUDIO analyzes at most this many seconds per call

AUDIO_UNSUPPORTED_FORMAT = "AUDIO_UNSUPPORTED_FORMAT"
AUDIO_TOO_LARGE = "AUDIO_TOO_LARGE"
AUDIO_TOO_LONG = "AUDIO_TOO_LONG"
AUDIO_MALFORMED = "AUDIO_MALFORMED"
AUDIO_EMPTY = "AUDIO_EMPTY"
AUDIO_DECODER_UNAVAILABLE = "AUDIO_DECODER_UNAVAILABLE"

_PYAV_SOURCE_CACHE = {}


def _iter_pyav_frames(container, stream):
    """Yield decodable audio frames while truthfully skipping bad packets."""
    for packet in container.demux(stream):
        try:
            frames = packet.decode()
        except Exception:
            continue
        for frame in frames:
            yield frame

# ---------------------------------------------------------------- view registry

WAVEFORM_ENVELOPE = "waveform_envelope"
DYNAMICS = "dynamics"
FREQUENCY_BANDS = "frequency_bands"
SPECTRUM = "spectrum"
ONSET_RHYTHM = "onset_rhythm"

# CAP2F section 4: chroma and a full spectrogram are deliberately NOT
# implemented this gate -- both benefit materially from librosa-grade
# pitch/harmonic and framing machinery this environment does not have
# installed; a from-scratch scipy/numpy version would either be a much
# larger undertaking than this bounded gate warrants or would risk
# producing a LOW-QUALITY representation dressed up as a real one,
# which is exactly the kind of dishonesty this gate's own design
# principle forbids. The five views below are each genuinely buildable
# from numpy/scipy alone with a clear, defensible mechanical meaning.
AVAILABLE_VIEWS = (WAVEFORM_ENVELOPE, DYNAMICS, FREQUENCY_BANDS, SPECTRUM, ONSET_RHYTHM)

# NAME-ONLY registry slot for the V1 observation view. Its implementation
# deliberately lives OUTSIDE this file (workspace_audio_observation.py --
# see its module docstring; numpy/scipy only),
# and workspace_capability routes it there before run_inspect_audio is
# ever called. Declaring the name HERE is what makes inspect_audio's own
# strict payload parser accept it and what lets LISTEN advertise it; if
# that module is ever unavailable the dispatch fails closed with
# INSPECT_AUDIO_ROUTE_TO_OBSERVATION_MODULE, never a partial result.
SEGMENT_OBSERVATION_VIEW = "segment_observation"
OBSERVATION_ONLY_VIEWS = (SEGMENT_OBSERVATION_VIEW,)
ALL_KNOWN_VIEWS = AVAILABLE_VIEWS + OBSERVATION_ONLY_VIEWS

WAVEFORM_ENVELOPE_BINS = 64
FREQUENCY_BANDS_COUNT = 8
SPECTRUM_BINS = 32

# Only estimator implemented this gate -- see ESTIMATOR_STATUS_ESTIMATE
# below for how it is labeled, never as mechanically certain fact. Key/
# tonal-center estimation is skipped for the same reason chroma is.
ESTIMATED_TEMPO_BPM = "estimated_tempo_bpm"
AVAILABLE_ESTIMATORS = (ESTIMATED_TEMPO_BPM,)
ESTIMATOR_STATUS_ESTIMATE = "estimate"


class AudioDecodeError(Exception):
    """Raised only for a genuinely unexpected mechanical failure --
    every ordinary validation failure (unsupported format, malformed,
    empty, invalid interval, or unavailable decoder) is instead returned as a
    (None, failure_kind) pair, exactly like workspace_capability.py's
    own deliver_photograph_bytes() convention."""


# --------------------------------------------------------------------- decode


def _resolve_interval(duration_seconds, start_seconds, end_seconds):
    start = 0.0 if start_seconds is None else start_seconds
    end = min(duration_seconds, start + MAX_INSPECT_WINDOW_SECONDS) if end_seconds is None else end_seconds
    try:
        return _clamp_interval(duration_seconds, start, end)
    except ValueError:
        return None


def _decode_with_soundfile(real_path, start_seconds, end_seconds):
    try:
        with sf.SoundFile(real_path, mode="r") as source:
            if source.frames <= 0 or source.samplerate <= 0:
                return None, AUDIO_EMPTY
            duration = source.frames / float(source.samplerate)
            interval = _resolve_interval(duration, start_seconds, end_seconds)
            if interval is None:
                return None, INSPECT_AUDIO_INVALID_INTERVAL
            start, end = interval
            start_frame = int(round(start * source.samplerate))
            frame_count = max(1, int(round((end - start) * source.samplerate)))
            source.seek(start_frame)
            samples = source.read(frames=frame_count, dtype="float32", always_2d=False)
            if samples.size == 0:
                return None, AUDIO_EMPTY
            channel_count = 1 if samples.ndim == 1 else samples.shape[1]
            return {
                "samples": samples,
                "sample_rate_hz": int(source.samplerate),
                "channel_count": channel_count,
                "duration_seconds": duration,
                "decoded_interval_seconds": [start, start + samples.shape[0] / float(source.samplerate)],
                "decoder": "libsndfile",
            }, None
    except Exception:
        return None, AUDIO_MALFORMED


def _decode_with_soundfile_sequential(real_path, start_seconds, end_seconds):
    """Decode by sample order when compressed-container seeking lies.

    Some intended MP3s contain damaged/junk regions whose declared frame count
    is far larger than their decodable sample stream.  This path keeps memory
    bounded, measures duration from samples genuinely returned by the decoder,
    and selects the requested window against that same sample order.
    """
    try:
        with sf.SoundFile(real_path, mode="r") as source:
            sample_rate = int(source.samplerate)
            channel_count = int(source.channels)
            if sample_rate <= 0 or channel_count <= 0:
                return None, AUDIO_EMPTY
            requested_start = 0.0 if start_seconds is None else start_seconds
            requested_end = (
                requested_start + MAX_INSPECT_WINDOW_SECONDS
                if end_seconds is None else end_seconds
            )
            if (
                not isinstance(requested_start, (int, float))
                or not isinstance(requested_end, (int, float))
                or requested_start < 0 or requested_end <= requested_start
                or requested_end - requested_start > MAX_INSPECT_WINDOW_SECONDS
            ):
                return None, INSPECT_AUDIO_INVALID_INTERVAL
            target_start = int(round(requested_start * sample_rate))
            target_end = int(round(requested_end * sample_rate))
            cursor = 0
            chunks = []
            while True:
                block = source.read(frames=65536, dtype="float32", always_2d=True)
                if block.size == 0:
                    break
                block_start = cursor
                block_end = block_start + block.shape[0]
                keep_start = max(0, target_start - block_start)
                keep_end = min(block.shape[0], target_end - block_start)
                if keep_end > keep_start:
                    chunks.append(block[keep_start:keep_end])
                cursor = block_end
            duration = cursor / float(sample_rate)
            interval = _resolve_interval(duration, requested_start, end_seconds)
            if interval is None or not chunks:
                return None, INSPECT_AUDIO_INVALID_INTERVAL
            samples = np.concatenate(chunks, axis=0)
            if channel_count == 1:
                samples = samples[:, 0]
            return {
                "samples": samples,
                "sample_rate_hz": sample_rate,
                "channel_count": channel_count,
                "duration_seconds": duration,
                "decoded_interval_seconds": [
                    target_start / float(sample_rate),
                    (target_start + samples.shape[0]) / float(sample_rate),
                ],
                "decoder": "libsndfile/sequential",
            }, None
    except Exception:
        return None, AUDIO_MALFORMED


def _decode_with_pyav(real_path, start_seconds, end_seconds):
    if av is None:
        return None, AUDIO_DECODER_UNAVAILABLE
    try:
        stat = os.stat(real_path)
        cache_key = (os.path.realpath(real_path), stat.st_size, stat.st_mtime_ns)
        source_facts = _PYAV_SOURCE_CACHE.get(cache_key)
        if source_facts is None:
            measuring = av.open(real_path)
            measured_stream = next(item for item in measuring.streams if item.type == "audio")
            sample_rate = int(measured_stream.codec_context.sample_rate or 0)
            channel_count = int(measured_stream.codec_context.channels or 0)
            codec_name = measured_stream.codec_context.name
            if sample_rate <= 0 or channel_count <= 0:
                measuring.close()
                return None, AUDIO_EMPTY
            decoded_samples = 0
            for measured_frame in _iter_pyav_frames(measuring, measured_stream):
                decoded_samples += measured_frame.samples
            measuring.close()
            if decoded_samples <= 0:
                return None, AUDIO_EMPTY
            source_facts = {
                "sample_rate": sample_rate,
                "channel_count": channel_count,
                "codec_name": codec_name,
                "duration": decoded_samples / float(sample_rate),
            }
            _PYAV_SOURCE_CACHE.clear()
            _PYAV_SOURCE_CACHE[cache_key] = source_facts
        sample_rate = source_facts["sample_rate"]
        channel_count = source_facts["channel_count"]
        duration = source_facts["duration"]

        container = av.open(real_path)
        stream = next(item for item in container.streams if item.type == "audio")
        interval = _resolve_interval(duration, start_seconds, end_seconds)
        if interval is None:
            container.close()
            return None, INSPECT_AUDIO_INVALID_INTERVAL
        start, end = interval
        layout_name = stream.codec_context.layout.name
        resampler = av.AudioResampler(format="fltp", layout=layout_name, rate=sample_rate)
        chunks = []
        target_start_sample = int(round(start * sample_rate))
        target_end_sample = int(round(end * sample_rate))
        decoded_sample_cursor = 0
        for frame in _iter_pyav_frames(container, stream):
            for converted in resampler.resample(frame):
                array = converted.to_ndarray()
                if array.ndim == 1:
                    array = array.reshape(1, -1)
                chunk_start = decoded_sample_cursor
                chunk_end = chunk_start + array.shape[1]
                keep_start = max(0, target_start_sample - chunk_start)
                keep_end = min(array.shape[1], target_end_sample - chunk_start)
                if keep_end > keep_start:
                    chunks.append(array[:, keep_start:keep_end].T.astype(np.float32, copy=False))
                decoded_sample_cursor = chunk_end
                if decoded_sample_cursor >= target_end_sample:
                    break
            if decoded_sample_cursor >= target_end_sample:
                break
        container.close()
        if not chunks:
            return None, AUDIO_EMPTY
        samples = np.concatenate(chunks, axis=0)
        if channel_count == 1:
            samples = samples[:, 0]
        return {
            "samples": samples,
            "sample_rate_hz": sample_rate,
            "channel_count": channel_count,
            "duration_seconds": duration,
            "decoded_interval_seconds": [
                target_start_sample / float(sample_rate),
                (target_start_sample + samples.shape[0]) / float(sample_rate),
            ],
            "decoder": f"pyav/{source_facts['codec_name']}",
        }, None
    except Exception:
        try:
            container.close()
        except Exception:
            pass
        return None, AUDIO_MALFORMED


def decode_audio_bounded(real_path, start_seconds=None, end_seconds=None):
    """Genuine bounded decode -- returns (result, failure_kind) where a
    successful result is {"samples" (numpy float32 array in [-1, 1],
    already format-normalized by the selected decoder regardless of
    source codec/bit-depth -- 2D (frames, channels) for a multi-channel
    source, 1D for mono), "sample_rate_hz", "channel_count",
    "duration_seconds", "size_bytes", "sha256"}. `failure_kind` is one
    of AUDIO_UNSUPPORTED_FORMAT / AUDIO_MALFORMED / AUDIO_EMPTY /
    AUDIO_DECODER_UNAVAILABLE / INSPECT_AUDIO_INVALID_INTERVAL, or None on
    success. The legacy AUDIO_TOO_* constants remain import-compatible but
    are intentionally not collection-access ceilings.

    Never mistakes "path existed" for "audio was successfully decoded"
    (CAP2F section 10): only returns success after libsndfile has
    actually parsed real PCM sample data out of the file, for whichever
    of the four supported formats it genuinely is (auto-detected from
    the file's own header bytes, never assumed from its extension).
    `samples` themselves are used only in-memory by the view/estimator
    functions below -- never serialized, logged, or handed to a caller
    for provenance.

    Duration is measured from samples the selected decoder actually returns,
    rather than trusting damaged compressed-container metadata. Only the
    requested interval (at most MAX_INSPECT_WINDOW_SECONDS) is retained in
    memory, while later windows remain independently addressable."""
    if not os.path.isfile(real_path):
        return None, AUDIO_MALFORMED

    extension = os.path.splitext(real_path)[1].lstrip(".").lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        return None, AUDIO_UNSUPPORTED_FORMAT

    size_bytes = os.stat(real_path).st_size
    if size_bytes == 0:
        return None, AUDIO_EMPTY

    if extension in {"mp3", "wma"} and av is not None:
        decoded, failure = _decode_with_pyav(real_path, start_seconds, end_seconds)
        if failure is not None and extension == "mp3":
            decoded, failure = _decode_with_soundfile_sequential(real_path, start_seconds, end_seconds)
    elif extension == "mp3":
        decoded, failure = _decode_with_soundfile_sequential(real_path, start_seconds, end_seconds)
    else:
        decoded, failure = _decode_with_soundfile(real_path, start_seconds, end_seconds)
        if failure in (AUDIO_MALFORMED, AUDIO_DECODER_UNAVAILABLE):
            soundfile_failure = failure
            decoded, failure = _decode_with_pyav(real_path, start_seconds, end_seconds)
            if failure == AUDIO_DECODER_UNAVAILABLE and extension != "wma":
                failure = soundfile_failure
    if failure is not None:
        return None, failure
    digest = hashlib.sha256()
    with open(real_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    decoded.update({"size_bytes": size_bytes, "sha256": digest.hexdigest()})
    return decoded, None


def _mono(samples):
    """Deterministic channel reduction for views that operate on a
    single signal -- simple average across channels, never a lossy
    "pick channel 0" that would silently discard real source content."""
    if samples.ndim == 1:
        return samples
    return samples.mean(axis=1).astype(np.float32)


def _clamp_interval(duration_seconds, start_seconds, end_seconds):
    """Fail-closed interval resolution (CAP2F section 3): both bounds
    optional; when omitted, analyzes from 0 up to MAX_INSPECT_WINDOW_
    SECONDS or the source's own duration, whichever is smaller -- never
    silently claims to have analyzed the whole file when a bounded
    window was actually used. Returns (start, end) or raises ValueError
    on any invalid combination -- the caller converts that to a fail-
    closed validation failure, never a normalized/guessed value."""
    if start_seconds is None:
        start_seconds = 0.0
    if end_seconds is None:
        end_seconds = min(duration_seconds, start_seconds + MAX_INSPECT_WINDOW_SECONDS)
    if not isinstance(start_seconds, (int, float)) or not isinstance(end_seconds, (int, float)):
        raise ValueError("start_seconds/end_seconds must be numeric")
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("interval must satisfy 0 <= start < end")
    if end_seconds > duration_seconds:
        raise ValueError("end_seconds exceeds source duration")
    if end_seconds - start_seconds > MAX_INSPECT_WINDOW_SECONDS:
        raise ValueError(f"interval exceeds MAX_INSPECT_WINDOW_SECONDS ({MAX_INSPECT_WINDOW_SECONDS})")
    return float(start_seconds), float(end_seconds)


def _windowed_mono(decoded, start_seconds, end_seconds):
    sample_rate = decoded["sample_rate_hz"]
    mono = _mono(decoded["samples"])
    decoded_start = decoded.get("decoded_interval_seconds", [0.0, decoded["duration_seconds"]])[0]
    start_idx = int(round((start_seconds - decoded_start) * sample_rate))
    end_idx = int(round((end_seconds - decoded_start) * sample_rate))
    start_idx = max(0, start_idx)
    end_idx = min(end_idx, mono.shape[0])
    start_idx = min(start_idx, end_idx)
    return mono[start_idx:end_idx]


# ------------------------------------------------------------------- views


def view_waveform_envelope(decoded, start_seconds, end_seconds):
    """Mechanical amplitude envelope -- RMS per bin, bounded to
    WAVEFORM_ENVELOPE_BINS values regardless of interval length or
    sample rate."""
    segment = _windowed_mono(decoded, start_seconds, end_seconds)
    bins = _bin_rms(segment, WAVEFORM_ENVELOPE_BINS)
    bin_duration = (end_seconds - start_seconds) / WAVEFORM_ENVELOPE_BINS
    return {
        "bin_count": len(bins),
        "bin_duration_seconds": round(bin_duration, 4),
        "envelope_rms": [round(float(v), 4) for v in bins],
    }


def view_dynamics(decoded, start_seconds, end_seconds):
    """Mechanical scalar summary -- deterministic formulas only (peak,
    RMS, crest factor, dBFS), never an inference/estimate."""
    segment = _windowed_mono(decoded, start_seconds, end_seconds)
    peak = float(np.max(np.abs(segment))) if segment.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(segment)))) if segment.size else 0.0
    crest_factor = (peak / rms) if rms > 1e-9 else 0.0
    dbfs = 20.0 * np.log10(rms) if rms > 1e-9 else float("-inf")
    return {
        "peak_amplitude": round(peak, 4),
        "rms_amplitude": round(rms, 4),
        "crest_factor": round(crest_factor, 3),
        "rms_dbfs": round(float(dbfs), 2) if dbfs != float("-inf") else None,
    }


def view_frequency_bands(decoded, start_seconds, end_seconds):
    """Mechanical FFT energy split into FREQUENCY_BANDS_COUNT
    log-spaced bands, each normalized to a 0-1 share of total energy
    -- bounded regardless of sample rate or interval length."""
    segment = _windowed_mono(decoded, start_seconds, end_seconds)
    sample_rate = decoded["sample_rate_hz"]
    magnitudes, freqs = _magnitude_spectrum(segment, sample_rate)
    nyquist = sample_rate / 2.0
    edges = _log_spaced_edges(20.0, nyquist, FREQUENCY_BANDS_COUNT)
    energy = np.square(magnitudes)
    total_energy = float(np.sum(energy)) or 1.0
    band_energy = []
    for i in range(FREQUENCY_BANDS_COUNT):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        band_energy.append(round(float(np.sum(energy[mask])) / total_energy, 4))
    return {
        "band_edges_hz": [round(e, 1) for e in edges],
        "band_energy_share": band_energy,
    }


def view_spectrum(decoded, start_seconds, end_seconds):
    """Mechanical bounded magnitude spectrum -- SPECTRUM_BINS log-spaced
    bins, each the mean magnitude of the FFT components falling in that
    bin, normalized by the maximum bin so values stay in [0, 1]."""
    segment = _windowed_mono(decoded, start_seconds, end_seconds)
    sample_rate = decoded["sample_rate_hz"]
    magnitudes, freqs = _magnitude_spectrum(segment, sample_rate)
    nyquist = sample_rate / 2.0
    edges = _log_spaced_edges(20.0, nyquist, SPECTRUM_BINS)
    binned = np.zeros(SPECTRUM_BINS)
    for i in range(SPECTRUM_BINS):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if np.any(mask):
            binned[i] = np.mean(magnitudes[mask])
    peak = float(np.max(binned)) or 1.0
    return {
        "bin_count": SPECTRUM_BINS,
        "bin_edges_hz": [round(e, 1) for e in edges],
        "magnitude_normalized": [round(float(v) / peak, 4) for v in binned],
    }


ONSET_MIN_SPACING_SECONDS = 0.1        # find_peaks distance floor used by every onset detector here
TEMPO_RESOLVABLE_FACTOR = 2.0          # median spacing must clear the floor by this factor
TEMPO_MAX_SPACING_VARIATION = 0.5      # coefficient of variation of inter-onset intervals


def tempo_from_onsets(onset_times):
    """(bpm or None, reason or None).  An estimate only where the pulse spacing is something the detector
    can actually resolve, and regular enough for a median to describe it.  Live 2026-09-24: a slow choral
    movement measured "400 bpm" in both halves (and a smooth tone "300 bpm") -- the median spacing sat on
    the detector's own 0.1 s floor, i.e. envelope ripple, not pulse.  Mechanical rule, no musical judgment."""
    if onset_times.size < 3:
        return None, "fewer than three onsets"
    intervals = np.diff(onset_times)
    median = float(np.median(intervals))
    if median < TEMPO_RESOLVABLE_FACTOR * ONSET_MIN_SPACING_SECONDS:
        return None, (f"median onset spacing {median:.3f} s is at the detector's resolution "
                      f"({ONSET_MIN_SPACING_SECONDS} s floor): envelope fluctuation, not a resolvable pulse")
    mean = float(np.mean(intervals))
    if mean > 0 and float(np.std(intervals)) / mean > TEMPO_MAX_SPACING_VARIATION:
        return None, "onset spacing too irregular for a single tempo"
    return 60.0 / median, None


def view_onset_rhythm(decoded, start_seconds, end_seconds):
    """Mechanical onset-pulse measurement -- peak-picks the amplitude
    envelope (scipy.signal.find_peaks, deterministic, no learned model)
    to find distinct energy pulses; reports their COUNT and median
    spacing as mechanical facts. Also computes ONE labeled estimator,
    estimated_tempo_bpm, from the median inter-onset interval -- a
    genuinely different evidence class (CAP2F section 5), always
    carrying estimator_status="estimate", never presented as a
    mechanically certain fact."""
    segment = _windowed_mono(decoded, start_seconds, end_seconds)
    sample_rate = decoded["sample_rate_hz"]
    envelope, hop_seconds = _envelope_for_onsets(segment, sample_rate)
    if envelope.size < 3 or np.max(envelope) <= 1e-9:
        onset_times = np.array([])
    else:
        threshold = 0.25 * np.max(envelope)
        peaks, _ = find_peaks(envelope, height=threshold, distance=max(1, int(ONSET_MIN_SPACING_SECONDS / hop_seconds)))
        onset_times = peaks * hop_seconds

    onset_count = int(onset_times.size)
    result = {"onset_count": onset_count}
    if onset_count >= 2:
        intervals = np.diff(onset_times)
        median_ioi = float(np.median(intervals))
        result["median_inter_onset_interval_seconds"] = round(median_ioi, 4)
        estimated_bpm, reason = tempo_from_onsets(onset_times)
        result["estimators"] = (
            {ESTIMATED_TEMPO_BPM: round(float(estimated_bpm), 1), "estimator_status": ESTIMATOR_STATUS_ESTIMATE}
            if estimated_bpm is not None else
            {ESTIMATED_TEMPO_BPM: None, "estimator_status": "not_established", "reason": reason})
    else:
        result["median_inter_onset_interval_seconds"] = None
    return result


VIEW_FUNCTIONS = {
    WAVEFORM_ENVELOPE: view_waveform_envelope,
    DYNAMICS: view_dynamics,
    FREQUENCY_BANDS: view_frequency_bands,
    SPECTRUM: view_spectrum,
    ONSET_RHYTHM: view_onset_rhythm,
}


# ------------------------------------------------------------- DSP helpers


def _bin_rms(segment, bin_count):
    if segment.size == 0:
        return [0.0] * bin_count
    edges = np.linspace(0, segment.size, bin_count + 1, dtype=int)
    out = []
    for i in range(bin_count):
        chunk = segment[edges[i]:edges[i + 1]]
        out.append(float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0)
    return out


def _magnitude_spectrum(segment, sample_rate):
    if segment.size == 0:
        return np.array([0.0]), np.array([0.0])
    windowed = segment * np.hanning(segment.size)
    spectrum = scipy_fft.rfft(windowed)
    freqs = scipy_fft.rfftfreq(segment.size, d=1.0 / sample_rate)
    return np.abs(spectrum), freqs


def _log_spaced_edges(low_hz, high_hz, count):
    low_hz = max(low_hz, 1.0)
    high_hz = max(high_hz, low_hz + 1.0)
    return [float(v) for v in np.geomspace(low_hz, high_hz, count + 1)]


def _envelope_for_onsets(segment, sample_rate, hop_ms=20):
    hop_samples = max(1, int(sample_rate * hop_ms / 1000.0))
    n_hops = max(1, segment.size // hop_samples)
    trimmed = segment[: n_hops * hop_samples]
    frames = trimmed.reshape(n_hops, hop_samples) if trimmed.size else np.zeros((1, hop_samples))
    envelope = np.sqrt(np.mean(np.square(frames), axis=1))
    return envelope, hop_samples / float(sample_rate)


# ---------------------------------------------------- renderers (bounded)

AUDIO_SOURCE_RENDERER_VERSION = "AUDIO_SOURCE_V1"
AUDIO_VIEW_RENDERER_VERSION = "AUDIO_VIEW_V1"


# What a delivered audio result ESTABLISHES, stated as a host fact on the result itself.
# Live finding (music checkout): after a genuine, successful listen the subject narrated the
# piece as heard ("the cello's long, drawn-out notes", "the weight of the harmony"), although
# only mechanical acoustic information was delivered and ANAXI's music scope does not include
# semantic / model-level hearing. Pass 2 saw file facts and view names and nothing said what
# they are, so the action's own name ("listen") carried the meaning. This states it. It is a
# fact about the delivered representation -- not an instruction about, or a check of, what
# the subject then says.
AUDIO_EPISTEMIC_STATUS = "Acoustic measurement, not hearing."

# The same fact, restated in the Pass-2 framing next to the task (the subject reads the
# framing more attentively than a field inside a result). Both pieces are deliberately terse:
# they ride in a prompt whose conservative (byte-only) room is small, and an audio result cannot
# be windowed -- a longer statement made the whole listen result withheld in that worst case
# (test_music_with_no_target_is_chosen_from_a_listing_and_decodes).
AUDIO_DELIVERY_FRAMING = (
    "Delivered: a mechanical acoustic measurement, not audio perception; how it sounds is inference."
)


def render_audio_source(name, extension, decoded):
    """CAP2F section 7: LISTEN's own bounded neutral orientation.
    Size is inherently fixed/small regardless of audio length -- no
    bins, no arrays, only scalar source facts and the list of
    available view/estimator names. NEVER includes mood/genre/
    interpretation of any kind (spec section 3)."""
    return {
        "renderer": AUDIO_SOURCE_RENDERER_VERSION,
        "name": name,
        "extension": extension,
        "size_bytes": decoded["size_bytes"],
        "sha256": decoded["sha256"],
        "duration_seconds": round(decoded["duration_seconds"], 3),
        "sample_rate_hz": decoded["sample_rate_hz"],
        "channel_count": decoded["channel_count"],
        "decoder": decoded.get("decoder"),
        "decoded_interval_seconds": [round(value, 3) for value in decoded.get("decoded_interval_seconds", [0.0, decoded["duration_seconds"]])],
        "available_views": list(ALL_KNOWN_VIEWS),
        "available_estimators": list(AVAILABLE_ESTIMATORS),
        "epistemic_status": AUDIO_EPISTEMIC_STATUS,
    }


def render_audio_view(view_name, start_seconds, end_seconds, decoded, view_result):
    """CAP2F section 7: INSPECT_AUDIO's own bounded representation.
    `view_result` is one of the small, fixed-shape dicts the VIEW_
    FUNCTIONS above return -- each already bounded by construction
    (fixed bin counts, scalar fields only, no unbounded arrays)."""
    rendered = {
        "renderer": AUDIO_VIEW_RENDERER_VERSION,
        "view": view_name,
        "interval_seconds": [round(start_seconds, 3), round(end_seconds, 3)],
        "sample_rate_hz": decoded["sample_rate_hz"],
        "channel_count": decoded["channel_count"],
        "source_sha256": decoded["sha256"],
        "decoder": decoded.get("decoder"),
    }
    rendered.update(view_result)
    rendered["epistemic_status"] = AUDIO_EPISTEMIC_STATUS
    return rendered


# --------------------------------------------------- INSPECT_AUDIO payload

INSPECT_AUDIO_ALLOWED_KEYS = frozenset({"view", "start_seconds", "end_seconds"})
INSPECT_AUDIO_MALFORMED_PAYLOAD = "INSPECT_AUDIO_MALFORMED_PAYLOAD"
INSPECT_AUDIO_UNKNOWN_VIEW = "INSPECT_AUDIO_UNKNOWN_VIEW"
INSPECT_AUDIO_INVALID_INTERVAL = "INSPECT_AUDIO_INVALID_INTERVAL"
# Reachable only if someone hands run_inspect_audio an OBSERVATION_ONLY_
# VIEW name directly -- workspace_capability always routes those to
# workspace_audio_observation BEFORE run_inspect_audio, so this is a
# defensive fail-closed path, never a partial result.
INSPECT_AUDIO_ROUTE_TO_OBSERVATION_MODULE = "INSPECT_AUDIO_ROUTE_TO_OBSERVATION_MODULE"


def parse_inspect_audio_payload(content):
    """CAP2F section 3: strict, machine-readable, fail-closed parsing
    of INSPECT_AUDIO's own `content` field -- a JSON object with ONLY
    `view` (required, one of ALL_KNOWN_VIEWS) and optionally
    `start_seconds`/`end_seconds` (numeric). NEVER parses natural
    language; any deviation from this exact shape fails closed.
    Returns (payload, failure_kind) -- payload is {"view", "start_seconds"
    or None, "end_seconds" or None} on success."""
    if isinstance(content, str) and content.strip() in ALL_KNOWN_VIEWS:
        # An exact view name (not prose) is unambiguous: it means {"view": <name>}.
        return {"view": content.strip(), "start_seconds": None, "end_seconds": None}, None
    try:
        raw = json.loads(content)
    except (TypeError, ValueError):
        return None, INSPECT_AUDIO_MALFORMED_PAYLOAD
    if not isinstance(raw, dict) or "view" not in raw:
        return None, INSPECT_AUDIO_MALFORMED_PAYLOAD
    if set(raw.keys()) - INSPECT_AUDIO_ALLOWED_KEYS:
        return None, INSPECT_AUDIO_MALFORMED_PAYLOAD
    view_name = raw.get("view")
    if not isinstance(view_name, str) or view_name not in ALL_KNOWN_VIEWS:
        return None, INSPECT_AUDIO_UNKNOWN_VIEW
    start_seconds = raw.get("start_seconds")
    end_seconds = raw.get("end_seconds")
    for value in (start_seconds, end_seconds):
        if value is not None and not isinstance(value, (int, float)):
            return None, INSPECT_AUDIO_MALFORMED_PAYLOAD
    return {"view": view_name, "start_seconds": start_seconds, "end_seconds": end_seconds}, None


def run_inspect_audio(decoded, view_name, start_seconds, end_seconds):
    """Resolves the interval, dispatches to the requested view function,
    and renders the bounded result -- or returns (None, failure_kind)
    on an invalid interval. Never runs any view other than the one
    Clark's own typed action named (spec section 6)."""
    try:
        resolved_start, resolved_end = _clamp_interval(decoded["duration_seconds"], start_seconds, end_seconds)
    except ValueError:
        return None, INSPECT_AUDIO_INVALID_INTERVAL
    decoded_interval = decoded.get("decoded_interval_seconds", [resolved_start, resolved_end])
    resolved_start = max(resolved_start, decoded_interval[0])
    resolved_end = min(resolved_end, decoded_interval[1])
    if resolved_end <= resolved_start:
        return None, INSPECT_AUDIO_INVALID_INTERVAL
    view_fn = VIEW_FUNCTIONS.get(view_name)
    if view_fn is None:
        # An OBSERVATION_ONLY view reached the wrong dispatcher
        # (workspace_capability routes those elsewhere) -- fail closed.
        return None, INSPECT_AUDIO_ROUTE_TO_OBSERVATION_MODULE
    view_result = view_fn(decoded, resolved_start, resolved_end)
    return render_audio_view(view_name, resolved_start, resolved_end, decoded, view_result), None

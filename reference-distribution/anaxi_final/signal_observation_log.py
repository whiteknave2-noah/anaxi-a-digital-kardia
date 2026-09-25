"""
Anaxi -- Persistent, append-only signal-observation log. Records
every signal-gate decision -- what the deterministic matcher in
signal_matcher.py actually saw and decided -- without ever modifying
the matcher's own behavior. Purely observational: the log records
what happened, it never feeds back into classification.

Per explicit design principle: this is host-side evidence, not model
interpretation. No model-generated content is ever stored here.

Storage: append-only JSONL, matching the project's existing raw-log
convention (anaxi_log.jsonl). Genuinely append-only by construction --
opened in "a" mode, never rewritten or truncated by anything in this
module.

The intended feedback loop this enables:
    real language -> deterministic gate -> persistent observation ->
    human review -> deliberate taxonomy change -> regression test
rather than:
    imagine another phrase -> add another regex.

Run:
    python signal_observation_log.py
"""

import datetime
import json
import os

from signal_matcher import MATCHER_VERSION, classify_signal_with_detail

LOG_FILE = "signal_observations.jsonl"


def record_observation(raw_text: str, log_path: str = LOG_FILE) -> dict:
    """Classifies raw_text and appends one observation record. Returns
    the record that was written. Never modifies matcher behavior --
    purely records what the (unchanged) matcher decided."""
    detail = classify_signal_with_detail(raw_text)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "signal_category": detail["category"],
        "matched_pattern": detail["matched_pattern"],
        "matched_text": detail["matched_text"],
        "original_text": raw_text,
        "normalized_text": detail["normalized_text"],
        "matcher_version": MATCHER_VERSION,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def query_observations(
    log_path: str = LOG_FILE,
    category: str = None,
    since_days: int = None,
) -> list:
    """Read-only query over the append-only log. Never modifies the
    log file. category filters to one signal category if given;
    since_days filters to records within that many days of now if
    given. Returns a list of matching records, oldest first."""
    if not os.path.exists(log_path):
        return []

    cutoff = None
    if since_days is not None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=since_days)

    results = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if category is not None and record["signal_category"] != category:
                continue
            if cutoff is not None:
                record_time = datetime.datetime.fromisoformat(record["timestamp"])
                if record_time < cutoff:
                    continue
            results.append(record)
    return results


def summarize(log_path: str = LOG_FILE) -> dict:
    """Read-only count of observations by category. Never modifies
    the log."""
    counts = {"POSITIVE": 0, "NEGATIVE": 0, "NO_SIGNAL": 0, "UNRECOGNIZED": 0}
    for record in query_observations(log_path):
        counts[record["signal_category"]] = counts.get(record["signal_category"], 0) + 1
    return counts


def main():
    print("=" * 70)
    print("SIGNAL OBSERVATION LOG -- self-test")
    print("=" * 70)

    test_log = "test_signal_observations.jsonl"
    if os.path.exists(test_log):
        os.remove(test_log)

    results = []

    def check(name, cond):
        print(f"{'PASS' if cond else 'FAIL'}: {name}")
        results.append(cond)

    samples = [
        ("Please save this.", "POSITIVE"),
        ("Don't save this.", "NEGATIVE"),
        ("What's a good way to organize a bookshelf?", "NO_SIGNAL"),
        ("You should remember that I said this.", "UNRECOGNIZED"),
    ]
    for text, expected_category in samples:
        record = record_observation(text, log_path=test_log)
        check(f"Records {expected_category} correctly: {text!r}",
              record["signal_category"] == expected_category)
        check(f"Original text preserved verbatim for {expected_category}",
              record["original_text"] == text)
        check(f"Matcher version recorded for {expected_category}",
              record["matcher_version"] == MATCHER_VERSION)

    check("POSITIVE record has a real matched_pattern",
          query_observations(test_log, category="POSITIVE")[0]["matched_pattern"] is not None)
    check("NO_SIGNAL record has matched_pattern=None",
          query_observations(test_log, category="NO_SIGNAL")[0]["matched_pattern"] is None)

    record_observation("I'll want to come back to this someday, remember.", log_path=test_log)
    record_observation("That's worth keeping, I think.", log_path=test_log)
    unrecognized = query_observations(test_log, category="UNRECOGNIZED")
    check("Multiple UNRECOGNIZED observations accumulate (3 total after 3 recorded)",
          len(unrecognized) == 3)

    positive_only = query_observations(test_log, category="POSITIVE")
    check("Category filter returns only POSITIVE records",
          all(r["signal_category"] == "POSITIVE" for r in positive_only))

    size_before = os.path.getsize(test_log)
    record_observation("One more observation.", log_path=test_log)
    size_after = os.path.getsize(test_log)
    check("Log file only grows (append-only), never shrinks", size_after > size_before)

    counts = summarize(test_log)
    print(f"\nFinal counts: {counts}")
    check("Summary counts match total records written", sum(counts.values()) == 7)

    os.remove(test_log)
    print(f"\n{'='*70}\n{sum(results)}/{len(results)} pass\n{'='*70}")


if __name__ == "__main__":
    main()

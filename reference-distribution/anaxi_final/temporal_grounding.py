"""Bounded host facts only. No message prose, retrieval, or private dependencies."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

UNKNOWN = 'not established'
MAX_BLOCK_BYTES = 1800


def process_start_time():
    """OS creation time, not module import or lazy session creation time.

    Windows path unchanged (accepted, tested): raw ctypes GetProcessTimes.

    macOS/Linux migration-readiness addition (2026-09-06): tries
    psutil.Process().create_time() -- a widely-used, independently
    verified library that already handles the real per-platform kernel
    struct layout correctly, rather than this module hand-rolling a
    ctypes sysctl/kinfo_proc parse it has no way to test against real
    hardware. ANY failure (psutil not installed, unexpected exception)
    falls back to None -- exactly the same 'not established' contract
    this function has always had on non-Windows platforms. This can
    only ever gain a real answer where one was previously always
    absent; it can never produce a fabricated or guessed value, and it
    never touches the existing Windows behavior at all."""
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(v) for v in values)):
                return None
            return ((values[0].dwHighDateTime << 32) + values[0].dwLowDateTime) / 10000000 - 11644473600
        except (OSError, AttributeError):
            return None
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except Exception:
        return None


def elapsed(seconds):
    if seconds < 0:
        return 'not established (host clock precedes recorded boundary)'
    minutes = int(seconds // 60)
    if minutes < 2:
        return 'less than 2 minutes'
    if minutes < 60:
        return f'{minutes} minutes'
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours} hours' + (f' {minutes} minutes' if minutes else '')
    return f'{hours // 24} days'


def local_time(epoch, tz=None):
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(tz).strftime('%A, %B %d, %Y, %I:%M %p %Z')


def recent_attempt(path, human_id, now):
    """Only exact H linkage qualifies; old unlinked telemetry is unknown.

    A bounded tail can miss an older record, which is never a negative claim.
    No raw exception, preview, or model content is returned or rendered.
    """
    if not path or not human_id:
        return None
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 262144))
            if size > 262144:
                stream.readline()
            lines = stream.read(262144).splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if row.get('human_input_event_id') != human_id or not isinstance(row.get('timestamp_end'), (int, float)):
                continue
            if row['timestamp_end'] > now:
                continue
            stages = row.get('stages')
            if not isinstance(stages, list) or any(not isinstance(s, dict) for s in stages):
                return None
            if row.get('waking_turn_success') is False:
                if any(s.get('purpose') in ('pass1', 'pass2', 'pass2_regen', 'artifact_judgment', 'pass2_task_mode') and s.get('success') is False for s in stages):
                    return 'The latest incomplete attempt failed during model generation.'
                if not stages:
                    return 'The latest incomplete attempt failed before Clark generation began.'
                if any(s.get('purpose') == 'pass2' and s.get('success') is True for s in stages):
                    return 'Model generation returned; no canonical reply was committed for the latest incomplete input.'
                return 'The latest incomplete attempt did not complete; generation boundary not established.'
            return None
    except (OSError, TypeError, AttributeError):
        return None
    return None


def snapshot(db_path, current_h, *, now=None, process_started=None,
             previous_end=None, lifecycle_notice=True, diagnostics_path=None,
             sleep_ledger_exhaustive=False, tz=None):
    """One consistent read transaction; injectable clock and established boundaries.

    Production has no durable waking-end receipt or exhaustive Sleep coverage
    receipt: their defaults MUST remain unknown. Synthetic callers can supply
    established boundaries to exercise the same rendering/query path.
    """
    now = time.time() if now is None else now
    lines = ['Temporal grounding — host facts:', f'Current local time: {local_time(now, tz)}.']
    last = None
    last_was_null = False
    incomplete = None
    latest_id = None
    sleep_count = None
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            # Scope history to the authenticated current human and pipeline.
            scope = conn.execute("SELECT e.pipeline_id,c.creator_actor_id FROM events e JOIN event_components c ON c.event_id=e.event_id WHERE e.event_id=? AND e.event_type='human_waking_input' AND c.sequence=0 AND c.authorship_resolution='resolved'", (current_h,)).fetchone()
            if scope:
                base = " FROM events h JOIN event_components hc ON hc.event_id=h.event_id AND hc.sequence=0 WHERE h.event_type='human_waking_input' AND h.pipeline_id=? AND hc.creator_actor_id=? AND hc.authorship_resolution='resolved'"
                pairs = "SELECT x.event_id,x.occurred_at FROM events x JOIN event_components link ON link.event_id=x.event_id JOIN events h ON h.event_id=link.component_text JOIN event_components hc ON hc.event_id=h.event_id AND hc.sequence=0 WHERE x.event_type='waking_turn' AND link.component_kind='human_input_event_id' AND h.event_type='human_waking_input' AND x.pipeline_id=h.pipeline_id AND h.pipeline_id=? AND hc.creator_actor_id=? AND hc.authorship_resolution='resolved' ORDER BY x.occurred_at DESC,x.event_id DESC LIMIT 1"
                last = conn.execute(pairs, scope).fetchone()
                # LAWFUL NULL: the last answered input may have been answered by Clark's own typed
                # choice to say nothing -- a completed exchange, but not a reply.
                last_was_null = bool(last) and conn.execute(
                    "SELECT 1 FROM event_components WHERE event_id=? AND component_kind='clark_reply_choice' "
                    "AND component_text='no_reply'", (last[0],)).fetchone() is not None
                # Exclude the fresh current H; a same-H continuation is also
                # explicitly represented below when its earlier attempt exists.
                predicate = " AND h.event_id<>? AND NOT EXISTS (SELECT 1 FROM events x JOIN event_components link ON link.event_id=x.event_id WHERE x.event_type='waking_turn' AND x.pipeline_id=h.pipeline_id AND link.component_kind='human_input_event_id' AND link.component_text=h.event_id)"
                params = (*scope, current_h)
                if last:
                    predicate += ' AND h.occurred_at>=?'
                    params += (last[1],)
                incomplete = conn.execute('SELECT COUNT(*)' + base + predicate, params).fetchone()[0]
                latest = conn.execute('SELECT h.event_id' + base + predicate + ' ORDER BY h.occurred_at DESC,h.event_id DESC LIMIT 1', params).fetchone()
                latest_id = latest[0] if latest else None
            if previous_end is not None and process_started is not None and previous_end <= process_started <= now:
                try:
                    # Read only public completion boundaries, never derivations
                    # or even selection/derivation counts.
                    sleep_count = conn.execute('SELECT COUNT(*) FROM sleep_cycles WHERE started_at>=? AND completed_at<=?', (previous_end, process_started)).fetchone()[0]
                except sqlite3.Error:
                    pass
    except (sqlite3.Error, OSError):
        pass
    if last and last_was_null:
        lines.append(f'Last canonical human→Clark exchange: {elapsed(now-last[1])} ago. You chose not to reply (your own typed choice); nothing was composed or sent.')
    elif last:
        lines.append(f'Last canonical human→Clark reply: {elapsed(now-last[1])} ago. Generated and persisted; delivery not established.')
    elif incomplete is not None:
        lines.append('No linked canonical human→Clark reply in available history.')
    else:
        lines.append(f'Last canonical human→Clark reply boundary: {UNKNOWN}.')
    if incomplete:
        lines.append(f'{incomplete} earlier authenticated human inputs have no linked canonical Clark reply' + (' at/after that recorded time.' if last else ' in available canonical history.'))
        lines.append(recent_attempt(diagnostics_path, latest_id, now) or 'Latest incomplete attempt generation status: not established.')
    elif incomplete is None:
        lines.append('Earlier incomplete interaction history: not established.')
    prior_current = recent_attempt(diagnostics_path, current_h, now)
    if prior_current:
        lines.append('The current input is a continuation of an earlier incomplete attempt. ' + prior_current)
    lines.append('Noncanonical attempts: not established.')
    if process_started is not None and process_started <= now:
        lines.append(f'Current waking process began {elapsed(now-process_started)} ago.')
    else:
        lines.append(f'Current waking-process start: {UNKNOWN}.')
    if lifecycle_notice:
        valid_interval = previous_end is not None and process_started is not None and previous_end <= process_started <= now
        lines.append(f'Previous waking-process end: {local_time(previous_end, tz) if valid_interval else UNKNOWN}.')
        lines.append(f'Dormant interval: {elapsed(process_started-previous_end) if valid_interval else UNKNOWN}.')
        if sleep_count:
            lines.append(f'{sleep_count} authorized Sleep cycle(s) completed during that interval.')
        elif sleep_count == 0 and sleep_ledger_exhaustive:
            lines.append('No authorized Sleep cycle is recorded during that interval.')
        else:
            lines.append('Sleep occurrence during that interval: not established.')
    result = '\n'.join(lines)
    if len(result.encode('utf-8')) > MAX_BLOCK_BYTES:
        raise ValueError('Temporal host facts exceed their bounded representation')
    return result

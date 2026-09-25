"""Authorized inbound Caret correspondence as a waking occasion.

While ANAXI is running, this service does one cheap thing on a modest cadence:
poll the already-authorized Caret destinations (a plain HTTP read, never a
model call), durably stage whatever is lawfully new through the existing
receive path, and -- only when a staged, undelivered message exists -- ask the
existing waking pipeline to run ONE occasion for the oldest such message.

It is not a heartbeat, an agenda, or a reply mechanism.  Receiving
correspondence creates an occasion, never an obligation: whether Clark answers
(through his own canonical send action), says nothing, or answers later is
entirely his.  Once a message has been carried into a committed waking turn it
is delivered -- silence is never a retry condition.

States kept distinct: observed/fetched -> durably staged (canonical inbound
event) -> delivered (canonical carriage in a committed waking turn + delivery
marker).  Everything canonical lives in discord_correspondence; this module
owns only timing and the content-free trace.

Owner pause: while the owner has paused Background Space activity the service
neither polls nor wakes.  Nothing is lost: the durable cursor makes the first
poll after resume fetch everything sent since (authorization-bounded).
"""
import json
import threading
import time

import correspondence_surfaces as cs
import discord_correspondence as dc

POLL_INTERVAL_SECONDS = 10.0
AFTER_TURN_INTERVAL_SECONDS = 1.0
RETRY_BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0)
MAX_ATTEMPTS_PER_MESSAGE = len(RETRY_BACKOFF_SECONDS) + 1
assert MAX_ATTEMPTS_PER_MESSAGE == cs.MAX_OCCASION_ATTEMPTS

DEFAULT_TRACE_PATH = "caret_wake_trace.jsonl"


class CaretWakeService:
    """`run_occasion(pending) -> {"status": ...}` performs one waking occasion.
    `owner_paused() -> bool`.  Both are injected so the timing/durability logic
    is testable without a model or a GUI."""

    def __init__(self, *, data_dir, run_occasion, owner_paused, trace_path=DEFAULT_TRACE_PATH,
                 clock=time.time, poll_fn=None):
        self.data_dir = data_dir
        self.run_occasion = run_occasion
        self.owner_paused = owner_paused
        self.trace_path = trace_path
        self.clock = clock
        self.poll_fn = poll_fn or dc.poll_authorized_inbound
        self._attempts = {}       # inbound event id -> consecutive failures (process-local)
        self._not_before = {}     # inbound event id -> earliest next attempt time
        self._held_traced = set() # inbound event ids already reported as held (process-local)
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------- trace
    def _trace(self, event, **fields):
        """Content-free operational trace.  Never message text, never
        waking context; failures to write it never affect delivery."""
        record = {"ts": int(self.clock()), "event": event, **fields}
        try:
            with open(self.trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception:
            pass

    # ---------------------------------------------------------- one step
    def step(self):
        """One service tick.  Returns (outcome, next_delay_seconds)."""
        try:
            if self.owner_paused():
                self._trace("deferred", reason="owner_paused")
                return "paused", POLL_INTERVAL_SECONDS
        except Exception:
            self._trace("deferred", reason="pause_state_unreadable")
            return "paused", POLL_INTERVAL_SECONDS

        try:
            polled = self.poll_fn(self.data_dir, occurred_at=int(self.clock()))
        except Exception as exc:
            polled = [{"status": "error", "detail": type(exc).__name__, "ingested": []}]
        staged = sum(len(r.get("ingested") or []) for r in polled)
        failed = [r.get("status") for r in polled if r.get("status") not in ("ok", None, "not_authorized")]
        if staged:
            self._trace("staged", count=staged,
                        event_ids=[e for r in polled for e in (r.get("ingested") or [])])
        if failed and not staged:
            self._trace("receive_failed", status=failed[0])

        try:
            repaired = dc.repair_carried_but_unmarked(self.data_dir, occurred_at=int(self.clock()))
        except Exception:
            repaired = []
        if repaired:
            self._trace("delivery_marker_repaired", count=len(repaired))

        try:   # an interrupted multipart reply resumes at its first never-attempted part; nothing else
            resumed = dc.resume_multipart_dispatch(self.data_dir)
        except Exception:
            resumed = []
        if resumed:
            self._trace("multipart_resumed", count=len(resumed),
                        statuses=[r.get("status") for r in resumed])

        try:   # a committed send that was never attempted (process died first) completes; at most once
            completed = dc.resume_pending_sends(self.data_dir)
        except Exception:
            completed = []
        if completed:
            self._trace("pending_send_resumed", count=len(completed),
                        statuses=[r.get("status") for r in completed])

        try:   # authorized-channel messages whose AUTHOR is not a valid mapped principal never wake Clark
            for held in dc.held_inbound(self.data_dir):
                if held["event_id"] not in self._held_traced:
                    self._held_traced.add(held["event_id"])
                    self._trace("held_author_not_deliverable", event_id=held["event_id"],
                                reason=held["reason"])
        except Exception:
            pass

        now = self.clock()
        candidates = dc.next_pending_inbound(self.data_dir, limit=dc.MAX_PENDING_INBOUND_LIMIT)
        # Exhausted / owner-closed occasions are already durably excluded by next_pending_inbound
        # (correspondence_surfaces.occasion_state); only the process-local backoff timing is applied here,
        # so an owner reopen takes effect immediately.
        candidates = [p for p in candidates if self._not_before.get(p["event_id"], 0) <= now]
        if not candidates:
            return "idle", POLL_INTERVAL_SECONDS
        pending = candidates[0]
        ids = {"event_id": pending["event_id"],
               "discord_message_id": (pending.get("metadata") or {}).get("discord_message_id")}

        self._trace("wake_attempted", **ids)
        try:
            outcome = self.run_occasion(pending)
        except Exception as exc:
            # DURABLE retry accounting (correspondence_surfaces): exhaustion survives a restart, so an
            # occasion that failed its bounded retries never resurrects on an unrelated future start.
            # A host fact -- never Clark's silence.  The process-local count is only a fallback.
            try:
                state = cs.record_occasion_failure(
                    self.data_dir, inbound_event_id=pending["event_id"],
                    failure_class=getattr(exc, "failure_code", None) or type(exc).__name__,
                    attempted_at=int(now))
                n = state["failures"]
            except Exception:
                n = self._attempts.get(pending["event_id"], 0) + 1
            self._attempts[pending["event_id"]] = n
            if n >= MAX_ATTEMPTS_PER_MESSAGE:
                self._trace("wake_failed", failure=type(exc).__name__, attempt=n, held=True, **ids)
            else:
                self._not_before[pending["event_id"]] = now + RETRY_BACKOFF_SECONDS[n - 1]
                self._trace("wake_failed", failure=type(exc).__name__, attempt=n, held=False, **ids)
            return "failed", POLL_INTERVAL_SECONDS

        status = outcome.get("status")
        if status == "busy":
            self._trace("deferred", reason="waking_turn_in_progress", **ids)
            return "busy", POLL_INTERVAL_SECONDS
        if status == "not_pending":
            self._trace("wake_skipped", reason="no_longer_lawfully_pending", **ids)
            return "skipped", AFTER_TURN_INTERVAL_SECONDS
        result = outcome.get("result") or {}
        report = (result.get("discord_correspondence") or {})
        outbound = report.get("outbound") or {}
        delivered = report.get("inbound_delivered") or {}
        self._trace("wake_result", delivered_status=delivered.get("status"),
                    outbound_status=outbound.get("status"),
                    caret_reply_status=(report.get("caret_reply") or {}).get("status"),
                    caret_reply_reason=(report.get("caret_reply") or {}).get("reason"), **ids)
        try:   # a carried message whose marker write failed must never wake twice
            dc.repair_carried_but_unmarked(self.data_dir, occurred_at=int(self.clock()))
        except Exception:
            pass
        return "delivered", AFTER_TURN_INTERVAL_SECONDS

    # ---------------------------------------------------------- thread
    def _loop(self):
        while not self._stop.is_set():
            try:
                _outcome, delay = self.step()
            except Exception as exc:
                self._trace("service_error", failure=type(exc).__name__)
                delay = POLL_INTERVAL_SECONDS
            self._stop.wait(delay)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="caret-wake", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

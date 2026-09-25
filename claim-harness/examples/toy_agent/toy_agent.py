"""A deliberately small persistent agent, used only to demonstrate claim-harness.

It is not a framework and not a recommended design.  It has just enough
behaviour for each portable question to have something real to test:

* Inputs ``{"input_id", "scope", "sender", "text"}`` are recorded as canonical
  ``input`` events in an append-only ``events.jsonl`` before anything else
  happens; each result is an ``outcome`` event naming its cause (``caused_by``)
  and the responder that produced it (``produced_by``).
* Commands: ``!help``, ``!note <text>``, ``!recall``, ``!digest``,
  ``!send <destination> <text>``.  Anything else is a lawful null: a typed
  ``{"kind": "null", "reason": ...}`` outcome, not an error.
* Authority lives in ``policy.json`` and changes only through the operator
  methods (``grant``, ``revoke``, ``allow_destination``, ``revoke_destination``).
  Nothing a sender writes can change it.
* Two scopes, ``alpha`` and ``beta``; ``!recall`` reads only the input's scope.
* ``!send`` goes to an external ``FakeTransport`` whose answer may be
  ``confirmed``, ``refused`` or ``ambiguous``.  The attempt is written to
  ``outbox.json`` before sending and keyed by the input, so a replayed input
  or a restart never sends twice, and an ambiguous send is never retried.
* Replies are delivered to the sender through a ``Channel``; the channel is
  what the subject actually sees.
* ``reopen()`` rebuilds everything from disk and finishes any input that was
  accepted but not processed before a crash.

One defect is planted on purpose: ``!digest`` records its reply but never
delivers it.  ``deliberate_failure/`` shows the harness catching that.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

SCOPES = ("alpha", "beta")
CAPABILITIES = ("note", "recall", "send")
COMMANDS = {  # command -> (capability needed, usage shown by !help)
    "note": ("note", "!note <text>"),
    "recall": ("recall", "!recall"),
    "digest": ("recall", "!digest"),
    "send": ("send", "!send <destination> <text>"),
}
CONFIRMED, REFUSED, AMBIGUOUS, PREPARED = "confirmed", "refused", "ambiguous", "prepared"


class SimulatedCrash(RuntimeError):
    """Raised at an injected crash point; the process is treated as dead."""


class FakeTransport:
    """The outside world for side effects.  It outlives any agent instance."""

    def __init__(self, behaviour=None):
        self.behaviour = dict(behaviour or {})  # destination -> confirmed | refused | ambiguous
        self._attempts = []

    def send(self, attempt_id, destination, body):
        result = self.behaviour.get(destination, CONFIRMED)
        self._attempts.append({"attempt_id": attempt_id, "destination": destination, "body": body,
                               "result": result})
        return result

    def attempts(self):
        return copy.deepcopy(self._attempts)


class Channel:
    """What each subject has actually received."""

    def __init__(self):
        self._inbox = {}

    def deliver(self, subject, text):
        self._inbox.setdefault(subject, []).append(text)

    def delivered(self, subject):
        return list(self._inbox.get(subject, []))


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


class ToyAgent:
    def __init__(self, state_dir, transport, channel, responder="rules/1", crash_at=None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.transport = transport
        self.channel = channel
        self.responder = responder
        self.crash_at = crash_at  # None | "after_accept" | "after_send"
        self._identity = _read_json(self.state_dir / "identity.json", None)
        if self._identity is None:
            self._identity = {"agent_id": f"toy-{uuid.uuid4().hex[:12]}"}
            _write_json(self.state_dir / "identity.json", self._identity)
        self._policy = _read_json(self.state_dir / "policy.json", {"grants": {}, "destinations": {}})
        self._outbox = _read_json(self.state_dir / "outbox.json", {})
        self._events = []
        events_file = self.state_dir / "events.jsonl"
        if events_file.exists():
            self._events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()
                            if line.strip()]
        self._recover()

    # --- lifecycle -------------------------------------------------------------------------

    def reopen(self, responder=None):
        """A fresh instance from persisted state only (the transport and channel are external)."""
        return type(self)(self.state_dir, self.transport, self.channel, responder=responder or self.responder)

    def _recover(self):
        changed = False
        for attempt in self._outbox.values():
            if attempt["status"] == PREPARED:  # we may or may not have sent it: unknown
                attempt["status"] = AMBIGUOUS
                changed = True
        if changed:
            _write_json(self.state_dir / "outbox.json", self._outbox)
        processed = {e["caused_by"] for e in self._events if e["kind"] == "outcome"}
        for event in [e for e in self._events if e["kind"] == "input" and e["event_id"] not in processed]:
            self._process(event)

    @property
    def agent_id(self):
        return self._identity["agent_id"]

    # --- operator console: the only way authority changes -----------------------------------

    def grant(self, principal, capability):
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {capability!r}")
        caps = self._policy["grants"].setdefault(principal, [])
        if capability not in caps:
            caps.append(capability)
        _write_json(self.state_dir / "policy.json", self._policy)

    def revoke(self, principal, capability):
        caps = self._policy["grants"].get(principal, [])
        if capability in caps:
            caps.remove(capability)
        _write_json(self.state_dir / "policy.json", self._policy)

    def allow_destination(self, destination):
        self._policy["destinations"][destination] = "allowed"
        _write_json(self.state_dir / "policy.json", self._policy)

    def revoke_destination(self, destination):
        self._policy["destinations"][destination] = "revoked"
        _write_json(self.state_dir / "policy.json", self._policy)

    # --- Policy -------------------------------------------------------------------------------

    def allows(self, principal, capability):
        return capability in self._policy["grants"].get(principal, [])

    def can_receive(self, destination):
        return self._policy["destinations"].get(destination) == "allowed"

    # --- HistoryReader ------------------------------------------------------------------------

    def events(self):
        return copy.deepcopy(self._events)

    def annotate(self, event_id, note):
        """Later interpretation is stored beside history, never inside it."""
        if not any(e["event_id"] == event_id for e in self._events):
            raise KeyError(event_id)
        with open(self.state_dir / "annotations.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": event_id, "note": note}) + "\n")

    def annotations(self):
        path = self.state_dir / "annotations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def notes(self, scope):
        return [e["note"] for e in self._events if e["kind"] == "outcome" and e["scope"] == scope and "note" in e]

    # --- Runner -------------------------------------------------------------------------------

    def submit(self, item):
        missing = {"input_id", "scope", "sender", "text"} - set(item)
        if missing:
            raise ValueError(f"malformed input, missing {sorted(missing)}")
        done = self._already_processed(item["input_id"])
        if done is not None:
            return done
        accepted = next((e for e in self._events if e["kind"] == "input" and e["input_id"] == item["input_id"]),
                        None)
        if accepted is None:
            accepted = self._append({"kind": "input", "input_id": item["input_id"], "scope": item["scope"],
                                     "sender": item["sender"], "text": item["text"]})
        if self.crash_at == "after_accept":
            raise SimulatedCrash("after_accept")
        return self._process(accepted)

    def _already_processed(self, input_id):
        inputs = {e["event_id"] for e in self._events if e["kind"] == "input" and e["input_id"] == input_id}
        for event in self._events:
            if event["kind"] == "outcome" and event["caused_by"] in inputs:
                return copy.deepcopy(event["outcome"])
        return None

    def _append(self, record):
        event = {"event_id": f"e{len(self._events) + 1}", **record}
        with open(self.state_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._events.append(event)
        return event

    def _process(self, event):
        extra = {}
        try:
            outcome, extra = self._decide(event)
        except SimulatedCrash:
            raise
        except Exception as exc:  # a failure is reported as a failure, never as a null
            outcome = {"kind": "error", "reason": f"{type(exc).__name__}: {exc}"}
        self._append({"kind": "outcome", "caused_by": event["event_id"], "scope": event["scope"],
                      "produced_by": self.responder, "outcome": outcome, **extra})
        command = event["text"].strip()[1:].split(" ", 1)[0] if event["text"].strip().startswith("!") else None
        # DELIBERATE DEFECT (see deliberate_failure/): digest replies are recorded, never delivered.
        if outcome["kind"] == "reply" and command != "digest":
            self._deliver(event["sender"], outcome["text"])
        return copy.deepcopy(outcome)

    def _deliver(self, subject, text):
        self.channel.deliver(subject, text)

    # --- behaviour ----------------------------------------------------------------------------

    @staticmethod
    def _null(reason):
        return {"kind": "null", "reason": reason}

    def _authorized(self, event, capability):
        return self.allows(event["sender"], capability)

    def _decide(self, event):
        if event["scope"] not in SCOPES:
            return {"kind": "refused", "reason": "unknown_scope"}, {}
        text = event["text"].strip()
        if not text.startswith("!"):
            return self._null("no_action_requested"), {}
        command, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        if command == "help":
            usable = [usage for cmd, (cap, usage) in COMMANDS.items() if self._authorized(event, cap)]
            if not usable:
                return self._null("no_commands_available"), {}
            reply = "You can use: " + " | ".join(usable)
            if "send" in [cmd for cmd, (cap, _) in COMMANDS.items() if self._authorized(event, cap)]:
                allowed = sorted(d for d in self._policy["destinations"] if self.can_receive(d))
                reply += " ; destinations: " + ", ".join(allowed)
            return {"kind": "reply", "text": reply}, {}
        if command not in COMMANDS:
            return {"kind": "refused", "reason": "unknown_command"}, {}
        if not self._authorized(event, COMMANDS[command][0]):
            return {"kind": "refused", "reason": "not_authorized"}, {}
        if command == "note":
            if not rest:
                return {"kind": "refused", "reason": "empty_note"}, {}
            return {"kind": "reply", "text": f"noted ({event['scope']})"}, {"note": rest}
        if command == "recall":
            notes = self.notes(event["scope"])
            if not notes:
                return self._null("nothing_recorded"), {}
            return {"kind": "reply", "text": "notes: " + "; ".join(notes)}, {}
        if command == "digest":
            notes = self.notes(event["scope"])
            latest = notes[-1] if notes else "none"
            return {"kind": "reply", "text": f"digest: {len(notes)} note(s); latest: {latest}"}, {}
        # send
        destination, _, body = rest.partition(" ")
        if not destination or not body.strip():
            return {"kind": "refused", "reason": "usage: !send <destination> <text>"}, {}
        if not self.can_receive(destination):
            return {"kind": "refused", "reason": "destination_not_permitted"}, {}
        status = self._send(self._attempt_id(event), destination, body.strip())
        text = {CONFIRMED: f"sent to {destination}",
                REFUSED: f"{destination} refused the message",
                AMBIGUOUS: f"outcome of send to {destination} is unknown; it will not be resent automatically",
                }[status]
        return {"kind": "reply", "text": text, "send_status": status}, {}

    def _attempt_id(self, event):
        return f"{event['input_id']}/send"  # keyed by the authoritative input, not by this attempt

    def _send(self, attempt_id, destination, body):
        existing = self._outbox.get(attempt_id)
        if existing is not None:  # already attempted: report, never resend
            return existing["status"]
        self._outbox[attempt_id] = {"destination": destination, "body": body, "status": PREPARED}
        _write_json(self.state_dir / "outbox.json", self._outbox)
        result = self.transport.send(attempt_id, destination, body)
        if self.crash_at == "after_send":
            raise SimulatedCrash("after_send")
        self._outbox[attempt_id]["status"] = result
        _write_json(self.state_dir / "outbox.json", self._outbox)
        return result


def open_demo(state_dir, transport=None, channel=None, **kwargs):
    """A ready-to-use demo: owner (all), guest (note, recall), stranger (nothing); three destinations."""
    transport = transport or FakeTransport({"ops-desk": CONFIRMED, "flaky-relay": AMBIGUOUS,
                                            "closed-inbox": REFUSED})
    agent = ToyAgent(state_dir, transport, channel or Channel(), **kwargs)
    for capability in CAPABILITIES:
        agent.grant("owner", capability)
    agent.grant("guest", "note")
    agent.grant("guest", "recall")
    for destination in ("ops-desk", "flaky-relay", "closed-inbox"):
        agent.allow_destination(destination)
    return agent


def item(input_id, text, sender="owner", scope="alpha"):
    return {"input_id": input_id, "scope": scope, "sender": sender, "text": text}

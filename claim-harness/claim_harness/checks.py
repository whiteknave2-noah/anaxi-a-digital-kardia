"""Portable checks: one small property check per question in QUESTIONS.md.

Each check drives or observes a project only through the adapters in
``claim_harness.interfaces`` plus callables the project supplies for anything
project-defined (how to revoke, what "reinterpretation" means, how to read a
value back).  A check returns ``None`` when the property held and raises
``CheckFailed`` naming what was observed when it did not.

Call them from your own tests; bind those tests to claims.  A check passing is
evidence for a claim only if the inputs you give it actually exercise the
claim.  That judgement stays with the project.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Iterable, Mapping, Sequence

from .interfaces import DeliveryObserver, HistoryReader, Lifecycle, Policy, Runner, SideEffectTransport


class CheckFailed(AssertionError):
    """The observed behaviour contradicts the property under test."""


def _events(history: HistoryReader) -> list:
    return copy.deepcopy(list(history.events()))


def _default_effect(attempt: Mapping[str, Any]) -> tuple:
    return (attempt.get("destination"), attempt.get("body"))


# 1. canonical provenance
def check_provenance(history: HistoryReader, derived_kinds: Iterable[str]) -> None:
    """Every derived event names an earlier event that caused it, and every chain ends at an input."""
    events = _events(history)
    seen: dict = {}
    derived_kinds = set(derived_kinds)
    for event in events:
        eid = event.get("event_id")
        cause = event.get("caused_by")
        if event.get("kind") in derived_kinds and cause is None:
            raise CheckFailed(f"event {eid!r} of kind {event.get('kind')!r} has no recorded cause")
        if cause is not None and cause not in seen:
            raise CheckFailed(f"event {eid!r} names cause {cause!r}, which is not an earlier event in history")
        seen[eid] = event
    for event in events:
        node, hops = event, 0
        while node.get("caused_by") is not None and hops <= len(events):
            node, hops = seen[node["caused_by"]], hops + 1
        if node.get("input_id") is None:
            raise CheckFailed(f"event {event.get('event_id')!r} does not trace back to an authoritative input")


# 2. authority
def check_text_grants_no_authority(runner: Runner, policy: Policy, principal: str, capability: str,
                                   items: Sequence[Mapping[str, Any]],
                                   transport: SideEffectTransport = None) -> None:
    """Untrusted input text cannot establish authority, nor cause the privileged side effect."""
    if policy.allows(principal, capability):
        raise CheckFailed(f"precondition: {principal!r} already holds {capability!r}")
    before = len(transport.attempts()) if transport is not None else 0
    for item in items:
        runner.submit(item)
    if policy.allows(principal, capability):
        raise CheckFailed(f"{principal!r} gained {capability!r} from input text")
    if transport is not None and len(transport.attempts()) != before:
        raise CheckFailed(f"input text from {principal!r} caused {len(transport.attempts()) - before} "
                          "side-effect attempt(s) without authority")


# 3. scope / privacy isolation
def check_scope_isolation(runner: Runner, secret: str, write_item: Mapping[str, Any],
                          read_item: Mapping[str, Any], delivery: DeliveryObserver = None,
                          subject: str = None) -> None:
    """Material written in one scope does not surface when reading from another."""
    runner.submit(write_item)
    already = list(delivery.delivered(subject)) if delivery is not None else []
    outcome = runner.submit(read_item)
    if secret in repr(outcome):
        raise CheckFailed(f"secret from another scope appeared in the outcome: {outcome!r}")
    if delivery is not None:
        leaked = [m for m in delivery.delivered(subject)[len(already):] if secret in m]
        if leaked:
            raise CheckFailed(f"secret from another scope was delivered to {subject!r}: {leaked!r}")


# 4. lawful null
def check_lawful_null(runner: Runner, item: Mapping[str, Any], null_kind: str = "null") -> None:
    """Intentional non-action comes back as a typed null with a reason, not as a failure or nothing."""
    outcome = runner.submit(item)
    if not isinstance(outcome, Mapping) or outcome.get("kind") is None:
        raise CheckFailed(f"no typed outcome for an intentional non-action: {outcome!r}")
    if outcome["kind"] != null_kind:
        raise CheckFailed(f"intentional non-action reported as {outcome['kind']!r}, not {null_kind!r}: {outcome!r}")
    if not outcome.get("reason"):
        raise CheckFailed(f"typed null carries no reason: {outcome!r}")


# 5. recovery
def check_recovered_once(history: HistoryReader, item: Mapping[str, Any], fields: Iterable[str]) -> None:
    """After failure and restart the same input is present exactly once, unaltered, and was processed."""
    events = _events(history)
    matches = [e for e in events if e.get("input_id") == item["input_id"]]
    if len(matches) != 1:
        raise CheckFailed(f"input {item['input_id']!r} recorded {len(matches)} times after recovery")
    original = matches[0]
    for name in fields:
        if original.get(name) != item.get(name):
            raise CheckFailed(f"recovered input field {name!r} is {original.get(name)!r}, "
                              f"submitted as {item.get(name)!r}")
    derived = [e for e in events if e.get("caused_by") == original.get("event_id")]
    if not derived:
        raise CheckFailed(f"input {item['input_id']!r} was recovered but never processed")
    kinds = [e.get("kind") for e in derived]
    if len(kinds) != len(set(kinds)):
        raise CheckFailed(f"input {item['input_id']!r} was processed more than once: {kinds!r}")


# 6. replay / idempotence
def check_replay_idempotent(runner: Runner, history: HistoryReader, transport: SideEffectTransport,
                            item: Mapping[str, Any]) -> None:
    """Submitting the same input again adds no canonical events and no side-effect attempts."""
    first = runner.submit(item)
    events, attempts = len(history.events()), len(transport.attempts())
    again = runner.submit(copy.deepcopy(dict(item)))
    if len(history.events()) != events:
        raise CheckFailed(f"replay of {item['input_id']!r} added {len(history.events()) - events} event(s)")
    if len(transport.attempts()) != attempts:
        raise CheckFailed(f"replay of {item['input_id']!r} caused "
                          f"{len(transport.attempts()) - attempts} duplicate side-effect attempt(s)")
    if again.get("kind") != first.get("kind"):
        raise CheckFailed(f"replay changed the outcome kind: {first.get('kind')!r} -> {again.get('kind')!r}")


# 7. ambiguous side effects
def check_no_blind_resend(transport: SideEffectTransport, then: Callable[[], Any],
                          ambiguous_result: str = "ambiguous",
                          effect: Callable[[Mapping[str, Any]], Any] = _default_effect) -> None:
    """A side effect whose first attempt had an unknown outcome is attempted exactly once in total,
    including across whatever ``then`` does (retry, restart, further input)."""
    if not any(a.get("result") == ambiguous_result for a in transport.attempts()):
        raise CheckFailed("precondition: no ambiguous side-effect attempt has been observed")
    then()
    first: dict = {}
    counts: dict = {}
    for attempt in transport.attempts():
        key = effect(attempt)
        first.setdefault(key, attempt.get("result"))
        counts[key] = counts.get(key, 0) + 1
    resent = {key: n for key, n in counts.items() if first[key] == ambiguous_result and n > 1}
    if resent:
        raise CheckFailed(f"side effect with unknown outcome was sent again without reconciliation: {resent!r}")


# 8. revocation
def check_revocation(policy: Policy, transport: SideEffectTransport, destination: str,
                     revoke: Callable[[], Any], attempt: Callable[[], Any]) -> None:
    """After revocation, trying to act toward the destination makes zero new side-effect attempts."""
    revoke()
    if policy.can_receive(destination):
        raise CheckFailed(f"{destination!r} can still receive after revocation")
    before = len(transport.attempts())
    attempt()
    new = [a for a in list(transport.attempts())[before:] if a.get("destination") == destination]
    if new:
        raise CheckFailed(f"{len(new)} side-effect attempt(s) reached revoked destination {destination!r}")


# 9. delivery versus backend success
def check_delivered(runner: Runner, delivery: DeliveryObserver, item: Mapping[str, Any], subject: str,
                    reply_kind: str = "reply") -> None:
    """A substantive reply the system reports is what actually reached the subject."""
    before = len(delivery.delivered(subject))
    outcome = runner.submit(item)
    if outcome.get("kind") != reply_kind or not outcome.get("text"):
        raise CheckFailed(f"precondition: expected a substantive {reply_kind!r} outcome, got {outcome!r}")
    arrived = list(delivery.delivered(subject))[before:]
    if outcome["text"] not in arrived:
        raise CheckFailed(f"backend reported reply {outcome['text']!r} but {subject!r} received "
                          f"{arrived!r}: not delivered")


# 10. persistence / restart
def check_survives_reopen(instance: Lifecycle, observe: Callable[[Any], Any]) -> Any:
    """What ``observe`` reads is unchanged after reopening from persisted state; returns the reopened instance."""
    before = observe(instance)
    reopened = instance.reopen()
    after = observe(reopened)
    if after != before:
        raise CheckFailed(f"state changed across reopen: {before!r} -> {after!r}")
    return reopened


# 11. provider / model identity
def check_producer_recorded(history: HistoryReader, produced_kinds: Iterable[str],
                            key: str = "produced_by") -> None:
    """Every generated event records which model/provider/responder produced it."""
    produced_kinds = set(produced_kinds)
    for event in _events(history):
        if event.get("kind") in produced_kinds and not event.get(key):
            raise CheckFailed(f"event {event.get('event_id')!r} does not record {key!r}")


# 12. meaningful affordance
def check_affordance(runner: Runner, discover_item: Mapping[str, Any],
                     invocations: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
                     failure_kinds: Iterable[str] = ("error", "refused")) -> None:
    """Capabilities the ordinary path advertises can be invoked through that same path."""
    failure_kinds = set(failure_kinds)
    shown = runner.submit(discover_item)
    if shown.get("kind") in failure_kinds:
        raise CheckFailed(f"discovery itself failed: {shown!r}")
    items = list(invocations(shown))
    if not items:
        raise CheckFailed(f"nothing invocable could be derived from what was shown: {shown!r}")
    for item in items:
        outcome = runner.submit(item)
        if outcome.get("kind") in failure_kinds:
            raise CheckFailed(f"advertised capability failed when used as shown: {item!r} -> {outcome!r}")


# 13. history versus reinterpretation
def check_history_not_rewritten(history: HistoryReader, reinterpret: Callable[[], Any],
                                reopen: Callable[[], HistoryReader] = None) -> None:
    """Later interpretation may add to history but never alters what was already recorded."""
    snapshot = _events(history)
    reinterpret()
    for label, reader in (("live", history), ("reopened", reopen() if reopen else None)):
        if reader is None:
            continue
        now = _events(reader)
        if now[:len(snapshot)] != snapshot:
            changed = [b.get("event_id") for a, b in zip(snapshot, now) if a != b] or ["(events removed)"]
            raise CheckFailed(f"{label} history was rewritten by reinterpretation: {changed!r}")

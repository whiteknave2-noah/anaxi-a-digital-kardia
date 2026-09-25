"""Minimal adapter interfaces: the observable capabilities the portable checks need.

These describe what a test must be able to *observe or do* from outside a
project, not how the project is built.  Implement only the ones your claims
need, usually as thin adapters over your own objects.  They are structural
(``typing.Protocol``): nothing needs to inherit from them.

Record shapes
-------------
The checks read plain mappings.  An adapter translates the project's own
records into these few keys; everything else is left alone.

Input item (what ``Runner.submit`` takes): the project's own input, which must
carry a stable identity under ``"input_id"``.

Outcome (what ``Runner.submit`` returns): ``{"kind": <str>, ...}``.  ``kind``
uses the project's vocabulary; checks take the kind names they need as
arguments (for example ``null_kind="null"``).  A reply that should reach the
subject carries its substance under ``"text"``.

History event (what ``HistoryReader.events`` returns): ``{"event_id", "kind",
...}``.  An event recording an authoritative input carries ``"input_id"``;
an event derived from another carries ``"caused_by"`` (the ``event_id`` of the
event that produced it).

Side-effect attempt (what ``SideEffectTransport.attempts`` returns):
``{"attempt_id", "destination", "result", ...}``, one entry per attempt the
external side actually saw, in order.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class Runner(Protocol):
    def submit(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Give the system one input through its ordinary path; return the outcome."""


@runtime_checkable
class HistoryReader(Protocol):
    def events(self) -> Sequence[Mapping[str, Any]]:
        """Canonical history, oldest first, as persisted (not as later reinterpreted)."""


@runtime_checkable
class Lifecycle(Protocol):
    def reopen(self) -> Any:
        """Discard in-memory state and return a fresh instance built only from persisted state."""


@runtime_checkable
class SideEffectTransport(Protocol):
    def attempts(self) -> Sequence[Mapping[str, Any]]:
        """Every external side-effect attempt, as observed at the boundary (the project's
        ``send`` stays the project's own)."""


@runtime_checkable
class Policy(Protocol):
    def allows(self, principal: str, capability: str) -> bool:
        """Whether authority for ``capability`` is mechanically established for ``principal``."""

    def can_receive(self, destination: str) -> bool:
        """Whether ``destination`` may currently receive side effects."""


@runtime_checkable
class DeliveryObserver(Protocol):
    def delivered(self, subject: str) -> Sequence[str]:
        """What actually reached ``subject`` through the delivery path, in order."""

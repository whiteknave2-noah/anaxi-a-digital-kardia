"""Claim inventory: what the project says is true, and what kind of evidence it rests on.

An inventory is a JSON document::

    {
      "schema": "claim-harness.inventory/v0",
      "project": "my-agent",
      "policy": {"not_attempted_fails_run": false},
      "claims": [
        {"id": "REPLAY-1", "statement": "...", "question": "replay-idempotence",
         "evidence": "executable"},
        {"id": "FIELD-1", "statement": "...", "evidence": "live_only",
         "live_record": {"observed": "...", "summary": "..."}},
        {"id": "RISK-1", "statement": "...", "evidence": "disposition",
         "disposition": {"decided_by": "...", "decision": "...", "rationale": "..."}},
        {"id": "OLD-1", "statement": "...", "evidence": "executable",
         "withdrawn": {"reason": "..."}}
      ]
    }

Evidence kinds:

``executable``   reproducible evidence (tests) bound in the bindings file.
``live_only``    observed once in a live setting; recorded, never re-run offline.
``disposition``  a decision by the owner/project; it is not execution evidence.

Any claim may carry ``withdrawn``; a withdrawn claim is never counted as passing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

INVENTORY_SCHEMA = "claim-harness.inventory/v0"

EXECUTABLE = "executable"
LIVE_ONLY = "live_only"
DISPOSITION = "disposition"
EVIDENCE_KINDS = (EXECUTABLE, LIVE_ONLY, DISPOSITION)

_CLAIM_KEYS = {"id", "statement", "question", "evidence", "live_record", "disposition", "withdrawn", "notes"}
_POLICY_KEYS = {"not_attempted_fails_run"}


class AccountingError(ValueError):
    """The inventory, bindings or evidence cannot be accounted for truthfully.

    ``problems`` lists every defect found, not only the first.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Claim:
    id: str
    statement: str
    evidence: str
    question: Optional[str] = None
    live_record: Optional[Mapping[str, Any]] = None
    disposition: Optional[Mapping[str, Any]] = None
    withdrawn: Optional[Mapping[str, Any]] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class Inventory:
    project: str
    claims: tuple
    not_attempted_fails_run: bool = False
    source: Optional[str] = None
    by_id: dict = field(default_factory=dict, compare=False, repr=False)

    def get(self, claim_id: str) -> Claim:
        return self.by_id[claim_id]


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _check_record(where: str, name: str, record, required: tuple, problems: list) -> None:
    if not isinstance(record, dict):
        problems.append(f"{where}: '{name}' must be an object")
        return
    for key in required:
        if not _nonempty_str(record.get(key)):
            problems.append(f"{where}: '{name}.{key}' must be a non-empty string")


def parse_inventory(data: Any, source: Optional[str] = None) -> Inventory:
    problems: list = []
    if not isinstance(data, dict):
        raise AccountingError(["inventory must be a JSON object"])
    if data.get("schema") != INVENTORY_SCHEMA:
        problems.append(f"inventory: 'schema' must be {INVENTORY_SCHEMA!r}, got {data.get('schema')!r}")
    if not _nonempty_str(data.get("project")):
        problems.append("inventory: 'project' must be a non-empty string")
    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        problems.append("inventory: 'policy' must be an object")
        policy = {}
    for key in set(policy) - _POLICY_KEYS:
        problems.append(f"inventory: unknown policy key {key!r}")
    fails_run = policy.get("not_attempted_fails_run", False)
    if not isinstance(fails_run, bool):
        problems.append("inventory: 'policy.not_attempted_fails_run' must be true or false")
    raw_claims = data.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        problems.append("inventory: 'claims' must be a non-empty list")
        raw_claims = []

    claims = []
    seen = set()
    for index, raw in enumerate(raw_claims):
        where = f"claims[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: must be an object")
            continue
        cid = raw.get("id")
        if not _nonempty_str(cid) or cid != cid.strip() or any(c.isspace() for c in cid):
            problems.append(f"{where}: 'id' must be a non-empty string without whitespace")
            continue
        where = f"claim {cid}"
        if cid in seen:
            problems.append(f"{where}: duplicate claim id")
            continue
        seen.add(cid)
        for key in sorted(set(raw) - _CLAIM_KEYS):
            problems.append(f"{where}: unknown key {key!r}")
        if not _nonempty_str(raw.get("statement")):
            problems.append(f"{where}: 'statement' must be a non-empty string")
        kind = raw.get("evidence")
        if kind not in EVIDENCE_KINDS:
            problems.append(f"{where}: 'evidence' must be one of {', '.join(EVIDENCE_KINDS)}; got {kind!r}")
        if "question" in raw and not _nonempty_str(raw["question"]):
            problems.append(f"{where}: 'question' must be a non-empty string when present")
        if kind == LIVE_ONLY:
            _check_record(where, "live_record", raw.get("live_record"), ("observed", "summary"), problems)
        elif "live_record" in raw:
            problems.append(f"{where}: 'live_record' is only valid for live_only claims")
        if kind == DISPOSITION:
            _check_record(where, "disposition", raw.get("disposition"),
                          ("decided_by", "decision", "rationale"), problems)
        elif "disposition" in raw:
            problems.append(f"{where}: 'disposition' is only valid for disposition claims")
        if "withdrawn" in raw:
            _check_record(where, "withdrawn", raw["withdrawn"], ("reason",), problems)
        claims.append(Claim(
            id=cid,
            statement=raw.get("statement", ""),
            evidence=kind,
            question=raw.get("question"),
            live_record=raw.get("live_record"),
            disposition=raw.get("disposition"),
            withdrawn=raw.get("withdrawn"),
            notes=raw.get("notes"),
        ))

    if problems:
        raise AccountingError(problems)
    return Inventory(
        project=data["project"],
        claims=tuple(claims),
        not_attempted_fails_run=fails_run,
        source=source,
        by_id={c.id: c for c in claims},
    )


def load_json(path) -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AccountingError([f"{path}: file not found"]) from None
    except json.JSONDecodeError as exc:
        raise AccountingError([f"{path}: invalid JSON ({exc})"]) from None


def load_inventory(path) -> Inventory:
    return parse_inventory(load_json(path), source=str(path))

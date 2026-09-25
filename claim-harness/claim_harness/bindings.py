"""Bindings: which executed evidence each executable claim requires.

A bindings file is a JSON document::

    {
      "schema": "claim-harness.bindings/v0",
      "bindings": {
        "REPLAY-1": [
          "tests/test_replay.py::test_second_submit_adds_no_events",
          "tests/test_replay.py::TestOutbox::test_no_second_send[confirm]"
        ]
      }
    }

Each entry is a pytest node ID relative to the pytest rootdir (the directory
holding the project's pytest configuration).  Every listed node ID is
*required*: a claim bound to N node IDs passes only when all N ran and passed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .inventory import EXECUTABLE, AccountingError, Inventory, load_json

BINDINGS_SCHEMA = "claim-harness.bindings/v0"


@dataclass(frozen=True)
class NodeId:
    """A parsed pytest node ID: ``path/to/test_file.py::Class::test_name[param]``."""

    raw: str
    path: str
    parts: tuple  # class names (if any) followed by the test name, params included

    @property
    def junit_key(self) -> tuple:
        """The (classname, name) pair pytest writes for this node in JUnit XML."""
        module = self.path[:-3] if self.path.endswith(".py") else self.path
        dotted = module.replace("/", ".")
        return (".".join((dotted,) + self.parts[:-1]), self.parts[-1])

    @property
    def module_key(self) -> str:
        module = self.path[:-3] if self.path.endswith(".py") else self.path
        return module.replace("/", ".")

    @property
    def function(self) -> str:
        return self.parts[-1].split("[", 1)[0]


def parse_node_id(raw) -> NodeId:
    if not isinstance(raw, str) or raw != raw.strip() or "::" not in raw:
        raise ValueError(f"not a pytest node ID (expected 'path.py::test_name'): {raw!r}")
    path, *parts = raw.split("::")
    if (not path.endswith(".py") or path.startswith("/") or "\\" in path
            or ".." in Path(path).parts or not parts or any(not p for p in parts)):
        raise ValueError(f"not a rootdir-relative pytest node ID: {raw!r}")
    return NodeId(raw=raw, path=path, parts=tuple(parts))


@dataclass(frozen=True)
class Bindings:
    by_claim: dict  # claim id -> tuple of NodeId
    source: Optional[str] = None

    def required(self, claim_id: str) -> tuple:
        return self.by_claim.get(claim_id, ())


def _defined(tree: ast.Module, parts: tuple) -> bool:
    """True when Class::...::function names exist in the parsed test module."""
    body = tree.body
    for name in parts[:-1]:
        cls = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == name), None)
        if cls is None:
            return False
        body = cls.body
    func = parts[-1].split("[", 1)[0]
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func for n in body)


def parse_bindings(data, inventory: Inventory, project_root=None, source: Optional[str] = None) -> Bindings:
    """Validate bindings against the inventory and, if given, the project tree.

    With ``project_root`` every bound test file must exist and define the bound
    test, so a typo is an accounting error rather than silently missing evidence.
    """
    problems: list = []
    if not isinstance(data, dict):
        raise AccountingError(["bindings must be a JSON object"])
    if data.get("schema") != BINDINGS_SCHEMA:
        problems.append(f"bindings: 'schema' must be {BINDINGS_SCHEMA!r}, got {data.get('schema')!r}")
    raw = data.get("bindings")
    if not isinstance(raw, dict):
        raise AccountingError(problems + ["bindings: 'bindings' must be an object mapping claim id -> list"])

    root = Path(project_root) if project_root is not None else None
    parsed_files: dict = {}
    by_claim: dict = {}
    for cid, entries in raw.items():
        where = f"bindings[{cid}]"
        claim = inventory.by_id.get(cid)
        if claim is None:
            problems.append(f"{where}: no such claim in the inventory")
            continue
        if claim.evidence != EXECUTABLE:
            problems.append(f"{where}: claim evidence is {claim.evidence!r}; only executable claims take "
                            "bindings (live-only records and dispositions are not execution evidence)")
            continue
        if not isinstance(entries, list) or not entries:
            problems.append(f"{where}: must be a non-empty list of pytest node IDs")
            continue
        nodes = []
        seen = set()
        for entry in entries:
            try:
                node = parse_node_id(entry)
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
                continue
            if node.raw in seen:
                problems.append(f"{where}: duplicate binding {node.raw!r} (each required item counts once)")
                continue
            seen.add(node.raw)
            if root is not None:
                if node.path not in parsed_files:
                    file = root / node.path
                    try:
                        parsed_files[node.path] = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
                    except FileNotFoundError:
                        parsed_files[node.path] = None
                    except SyntaxError:
                        parsed_files[node.path] = "syntax-error"
                tree = parsed_files[node.path]
                if tree is None:
                    problems.append(f"{where}: unknown evidence {node.raw!r} (file {node.path} not found "
                                    f"under {root})")
                    continue
                if tree != "syntax-error" and not _defined(tree, node.parts):
                    problems.append(f"{where}: unknown evidence {node.raw!r} (test not defined in {node.path})")
                    continue
            nodes.append(node)
        by_claim[cid] = tuple(nodes)

    for claim in inventory.claims:
        if claim.evidence == EXECUTABLE and claim.withdrawn is None and claim.id not in raw:
            problems.append(f"claim {claim.id}: executable claim has no bound evidence; bind the tests that "
                            "establish it, or change its evidence kind")

    if problems:
        raise AccountingError(problems)
    return Bindings(by_claim=by_claim, source=source)


def load_bindings(path, inventory: Inventory, project_root=None) -> Bindings:
    return parse_bindings(load_json(path), inventory, project_root=project_root, source=str(path))

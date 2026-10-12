"""Deterministic fingerprint of the constitutional (Kardia) meaning-bearing code.

Meaning-bearing = the class constants, axiom prototypes, destination/trajectory
validators and delta measure of ``ConstitutionalMind`` in both protocol modules,
plus the whole ``linguistic_pipeline`` module that renders Kardia into the
generation preamble.  Infrastructure edits (embedding loader, an additional
optional provenance field) do not touch these nodes; any change to axiom
constants or validators does, and must therefore be an explicit owner decision
(update the baseline deliberately), never a side effect of unrelated work.
"""

from __future__ import annotations

import ast
import hashlib

MEANING_FUNCTIONS = (
    "_init_violation_prototypes", "_max_sim_to_prototypes", "_validate_axioms_destination",
    "_kardia_delta", "_validate_axioms_trajectory", "_kardia_text",
)


def _digest(nodes) -> str:
    material = "\n".join(ast.dump(node, include_attributes=False) for node in nodes)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def protocol_fingerprint(source: str) -> dict:
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ConstitutionalMind")
    constants = [n for n in cls.body if isinstance(n, (ast.Assign, ast.AnnAssign))]
    functions = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in MEANING_FUNCTIONS}
    result = {"class_constants": _digest(constants)}
    for name in MEANING_FUNCTIONS:
        result[name] = _digest([functions[name]]) if name in functions else None
    return result


def whole_module_fingerprint(source: str) -> str:
    return _digest([ast.parse(source)])

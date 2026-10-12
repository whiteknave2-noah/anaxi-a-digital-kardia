"""
Boundary Inspector v1 -- operator CLI.

Read-only host inspection surface for the four closed boundary-query
kinds defined by the BOUNDARY INSPECTOR v1 gate:

    capability:<target>                 e.g. capability:library
    boundary_id:<target>                e.g. boundary_id:private_space.public_rule
    host_rule:<target>                  e.g. host_rule:participation.requires_reason
    recent_rejected_action:<target>     e.g. recent_rejected_action:budget.most_recent
                                        or recent_rejected_action:outward.<26-char Crockford ULID>

Behavior contract:
  * READ-ONLY. This CLI never writes to the provenance DB, the trace,
    or any other state -- it calls only boundary_inspector.
    evaluate_boundary_query() (+) validation, and displays the result.
  * Validation rejections (unknown kind/target, malformed inquiry, a
    structurally impossible session-scoped request) exit non-zero with
    a diagnostic on stderr -- never a fabricated result.
  * --internal-detail is a CLI-only audit aid: it may render repo
    file:line anchors (via inspect) for the rationale-registry entries
    backing each established boundary. The engine and its persisted
    results NEVER include paths or this annotation (see
    boundary_inspector.render_boundary_result_delivery()).

Usage:
    python3 -B anaxi_final/boundary_inspect_cli.py "capability:library" \
        [--data-dir anaxi_final] [--trace-path conversation_direction_trace.jsonl] \
        [--session-id <id>] [--internal-detail]
"""

import argparse
import inspect
import os
import sys

import boundary_inspector
import boundary_rationale_registry as brr
import conversation_direction_trace as cdt


def _parse_inquiry(inquiry: str) -> dict:
    """Split the single positional `kind:target` into the closed query
    dict the engine validates. Returns None-ish errors as exits rather
    than guesses -- the engine's own validate_boundary_query() remains
    the authoritative gate."""
    if not isinstance(inquiry, str) or not inquiry:
        return None
    kind, sep, target = inquiry.partition(":")
    if not sep or not kind or not target:
        return None
    return {"query_kind": kind, "query_target": target}


def _render_iso_timestamp(value) -> str:
    if not isinstance(value, int):
        return str(value)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _internal_detail_lines(result) -> list:
    """CLI-only source-anchor audit lines. Never part of the engine's
    result model; renders rationale-registry definition file:line anchors
    only. inspect is allowed here because this is a CLI-only audit aid --
    the evaluation checkers themselves never introspect source."""
    try:
        file = inspect.getsourcefile(brr)
        source_lines = inspect.getsource(brr).split("\n")
    except (OSError, TypeError):
        file, source_lines = "(source unavailable)", []

    def _line_for(needle):
        for i, line in enumerate(source_lines, 1):
            if needle in line:
                return i
        return 0

    lines = [f"internal-detail: rationale registry anchor = {file}:{_line_for('BOUNDARY_DEFINITIONS =') or 0}"]
    for chosen in result["boundaries"]:
        entry = brr.BOUNDARY_DEFINITIONS.get(chosen["boundary_id"])
        if entry is None:
            lines.append(f"internal-detail:   {chosen['boundary_id']}: (registry entry missing)")
            continue
        needle = '"' + chosen["boundary_id"] + '":'
        lines.append(
            f"internal-detail:   {chosen['boundary_id']} -> "
            f"boundary_type={entry['boundary_type']} anchor={file}:{_line_for(needle) or 0}"
        )
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="boundary_inspect_cli.py",
        description="Read-only Boundary Inspector v1 host inspection (never writes).",
    )
    parser.add_argument("inquiry", help="closed kind:target inquiry, e.g. capability:library")
    parser.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="directory containing anaxi_provenance.db (default: this package)")
    parser.add_argument("--trace-path", default=cdt.DEFAULT_TRACE_PATH,
                        help=f"conversation-direction trace path (default: {cdt.DEFAULT_TRACE_PATH})")
    parser.add_argument("--session-id", default=None,
                        help="current lawful session_id; REQUIRED for recent_rejected_action")
    parser.add_argument("--internal-detail", action="store_true",
                        help="emit CLI-only registry source anchors for audit")
    args = parser.parse_args(argv)

    query = _parse_inquiry(args.inquiry)
    if query is None:
        print(
            f"error: malformed inquiry {args.inquiry!r} -- expected exactly 'kind:target' "
            f"with a non-empty target (e.g. capability:library)",
            file=sys.stderr,
        )
        return 2

    env = boundary_inspector.BoundaryInspectionEnv(args.data_dir, trace_path=args.trace_path)
    try:
        query = boundary_inspector.validate_boundary_query(query)
    except boundary_inspector.BoundaryInspectionError as exc:
        print(f"error: boundary inquiry rejected: {exc}", file=sys.stderr)
        return 2

    try:
        result = boundary_inspector.evaluate_boundary_query(
            env, query, session_id=args.session_id,
        )
        result = boundary_inspector.validate_result(result)
    except boundary_inspector.BoundaryInspectionError as exc:
        print(f"error: evaluation refused: {exc}", file=sys.stderr)
        return 1

    print(f"query: {result['query']['query_kind']}:{result['query']['query_target']}")
    print(f"classification: {result['classification']}")
    print(f"evidence_complete: {result['evidence_complete']}")
    print(f"evaluated_at: {_render_iso_timestamp(result['evaluated_at'])}")
    print(f"scope: {result['scope_note']}")
    if result["boundaries"]:
        print(f"boundaries: {len(result['boundaries'])}")
        for b in result["boundaries"]:
            print(f"  - {b['boundary_id']} [{b['boundary_type']}]")
            print(f"      why: {b['operational_why']}")
            print(f"      changes: {b['what_changes_it']}")
            print(f"      evidence: {', '.join(b['evidence_source_ids'])}")
    else:
        print("boundaries: none")
    print(f"checks: {len(result['checks'])}")
    if args.internal_detail:
        for line in _internal_detail_lines(result):
            print(line)
        for c in result["checks"]:
            print(
                f"  - evidence_source_id={c['evidence_source_id']} result={c['result']} "
                f"boundary_id={c['boundary_id']} observed_at={_render_iso_timestamp(c['observed_at'])}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
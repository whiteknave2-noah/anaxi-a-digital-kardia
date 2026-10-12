"""
Anaxi -- Claude sleep cycle: memory consolidation + identity reflection.

Reads recent turns from anaxi_log.jsonl, asks Claude (twice, low temperature,
strict-JSON prompts) to propose memory updates and a possible Kardia
revision, then runs both through the protocol's own governance -- the layer
that actually decides whether anything takes effect immediately, waits for
your review, or gets blocked outright.

Setup: same folder, same dependencies, same ANTHROPIC_API_KEY as
claude_anaxi.py. Run claude_anaxi.py a few times first so there's something
in anaxi_log.jsonl worth consolidating.

Usage:
    python anaxi_sleep.py                   run a sleep cycle
    python anaxi_sleep.py --list            show pending proposals
    python anaxi_sleep.py --resolve 3 accept
    python anaxi_sleep.py --resolve 3 reject
"""

import os
import re
import sqlite3
import sys
import json

import anthropic
from orchestration import AnaxiOrchestrator
from migrate_historical_data import PIPELINE_STRUCTURE, matches_legacy_pipeline

PIPELINE_KEY = PIPELINE_STRUCTURE["claude"]["pipeline_key"]

MODEL = "claude-sonnet-5"
DB_PATH = "anaxi_mind.db"          # same file claude_anaxi.py writes to
USER_ID = "nate"
LOG_FILE = "anaxi_log.jsonl"
PROVENANCE_DB_PATH = "anaxi_provenance.db"
RECENT_TURNS = 20                  # how many past log entries to consolidate

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def to_claude_format(messages: list[dict]) -> tuple[str, list[dict]]:
    if messages and messages[0].get("role") == "system":
        return messages[0]["content"], messages[1:]
    return "", messages


def ask_claude_for_json(messages: list[dict]) -> str:
    """This call fills out a schema, it isn't having a personality --
    but claude-sonnet-5 rejects temperature outright (400: "temperature
    is deprecated for this model"), so determinism has to come from the
    prompt itself rather than a low temperature."""
    system, rest = to_claude_format(messages)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=rest,
    )
    text = response.content[0].text.strip()
    # The prompts ask for raw JSON, but the model still wraps it in a
    # ```json fence often enough that the protocol's strict json.loads()
    # rejects the payload outright -- strip the fence here rather than
    # loosen the protocol's own parsing.
    fenced = _JSON_FENCE_RE.match(text)
    return fenced.group(1) if fenced else text


def load_recent_conversation(n: int, provenance_conn: sqlite3.Connection) -> str:
    """Filtering goes through matches_legacy_pipeline()
    (migrate_historical_data.py, reused unedited) instead of a naive
    `e.get("substrate") == "claude"` check -- correctly separates this
    pipeline's entries from a shared log that may also contain entries
    carrying a pipeline_id key this historical filter never had to
    account for. Falls back to the historical substrate label for
    entries that predate the native write path."""
    if not os.path.exists(LOG_FILE):
        return "(no prior turns logged yet)"
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    claude_entries = [e for e in entries if matches_legacy_pipeline(provenance_conn, e, PIPELINE_KEY)]
    recent = claude_entries[-n:]
    if not recent:
        return "(no prior turns logged yet)"
    return "\n\n".join(
        f"User: {e['prompt']}\nClaude: {e['response']}" for e in recent
    )


def run_sleep(orch: AnaxiOrchestrator) -> None:
    provenance_conn = sqlite3.connect(PROVENANCE_DB_PATH)
    try:
        conversation = load_recent_conversation(RECENT_TURNS, provenance_conn)
    finally:
        provenance_conn.close()

    print("--- asking Claude to propose memory updates (REM) ---")
    rem_json = ask_claude_for_json(orch.build_rem_prompt(conversation, "(none yet)"))
    print(rem_json)

    print("\n--- asking Claude to propose an identity reflection ---")
    reflection_json = ask_claude_for_json(
        orch.build_reflection_prompt(
            conversation_summary=conversation,
            identity_signals="(none noted)",
            user_id=USER_ID,
        )
    )
    print(reflection_json)

    print("\n--- running both through governance ---")
    result = orch.run_sleep_cycle(
        user_id=USER_ID,
        rem_json_payload=rem_json,
        reflection_json_payload=reflection_json,
    )

    print(f"\nMemory consolidation: {result['sleep_status']}")
    if result["sleep_status"] == "failed":
        print(f"  reason: {result['reason']}")
    print(f"Identity: {result['identity_status']}")

    proposals = result["pending_proposals"]
    if proposals:
        print(f"\n{len(proposals)} pending proposal(s) waiting on you:")
        for p in proposals:
            print(f"  #{p.get('id')} [{p.get('proposal_type')}] {p.get('reason')}")
        print("Resolve with: python anaxi_sleep.py --resolve <id> accept|reject")
    else:
        print("\nNo pending proposals.")


def list_proposals(orch: AnaxiOrchestrator) -> None:
    proposals = orch.list_pending_proposals(USER_ID)
    if not proposals:
        print("No pending proposals.")
        return
    for p in proposals:
        print(json.dumps(p, indent=2, default=str))


def resolve(orch: AnaxiOrchestrator, proposal_id: int, accept: bool) -> None:
    print(orch.resolve_proposal(USER_ID, proposal_id, accept))


LEGACY_SLEEP_DISABLED_MESSAGE = (
    "LEGACY_SLEEP_DISABLED\n"
    "This Claude-substrate Sleep pathway (anaxi_sleep.py) has been "
    "mechanically disabled -- SLP1-D1B established that the only "
    "authoritative Clark Sleep pathway is the local Llama SLP1 pathway; "
    "running this file too would be a second, uncoordinated Sleep "
    "authority. No model call was made, nothing was written. The source "
    "remains on disk for historical reference only."
)


def main():
    # SLP1-S1 section 15: this guard sits ahead of EVERY branch below
    # (no-args, --list, --resolve, and the help fallback) with a single
    # unconditional check -- not one guard per branch -- so there is no
    # public invocation path through this file's own CLI that can reach
    # AnaxiOrchestrator/ConstitutionalMind, ask_claude_for_json, or any
    # Sleep write. Checked before the ANTHROPIC_API_KEY check too, so
    # the disable message is unconditional regardless of environment.
    print(LEGACY_SLEEP_DISABLED_MESSAGE)
    sys.exit(1)


if __name__ == "__main__":
    main()

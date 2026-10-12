"""
Anaxi -- Claude substrate test script.
Proves the loop closes: send a prompt to Claude, get a response, log it.

Setup:
    pip install anthropic
    setx ANTHROPIC_API_KEY "sk-ant-..."      (Windows -- then reopen your terminal)
    Get a key at console.anthropic.com (API Keys -> Create Key). Needs a payment
    method on file, but a session of test calls like this runs well under $1.

Run:
    python claude_agent.py "your prompt here"
"""

import os
import sys
import json
from datetime import datetime, timezone
import anthropic


class ClaudeWakingDeferredError(Exception):
    """Amendment A1 (Scoped Production Activation) Sec.A1.3 fail-closed
    guard. No module implementing the Claude native waking-write
    contract (frozen Identity/Provenance Schema Sec.4) exists yet, so
    this executable -- one of the three enumerated in A1.3.1 -- must
    refuse before any generation or persistence. Distinct and
    non-swallowable per A1.3(2). Defined locally, not imported from
    anaxi_final/, because this file is structurally independent of
    both anaxi_final/ modules (A1.3.1) and must not gain a new
    cross-package dependency merely to share four lines of guard code.
    Not a permanent exclusion: the deferral ends upon separate
    authorization supported by its own evidence."""


def refuse_claude_waking(entry_point: str) -> None:
    """Call as the FIRST action of this executable's entry point --
    before any Anthropic/API invocation, before any persistence.
    Always raises; never returns."""
    raise ClaudeWakingDeferredError(
        f"{entry_point}: Claude native waking is deferred per Amendment A1 "
        f"(Identity/Provenance Schema, frozen spec Sec.4 as narrowed by A1.3). "
        f"No Claude native waking-write implementation exists yet. This is a "
        f"structural backstop, not a permanent exclusion -- refusing before "
        f"any generation or persistence."
    )

MODEL = "claude-sonnet-5"  # cheaper/faster for pure connectivity tests: "claude-haiku-4-5-20251001"
                            # strongest available if you want it: "claude-opus-5"
LOG_FILE = "anaxi_log.jsonl"


def call_claude(prompt: str) -> str:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def log_entry(substrate: str, model: str, prompt: str, response: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "substrate": substrate,
        "model": model,
        "prompt": prompt,
        "response": response,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    refuse_claude_waking("claude_agent.py::main")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print('ANTHROPIC_API_KEY isn\'t set. Get a key at console.anthropic.com, then:')
        print('  setx ANTHROPIC_API_KEY "sk-ant-..."   (and reopen your terminal)')
        sys.exit(1)

    prompt = " ".join(sys.argv[1:]) or "Say hello and confirm you're online."
    print(f"[claude:{MODEL}] sending: {prompt}")
    reply = call_claude(prompt)
    print(f"[claude:{MODEL}] received: {reply}")
    log_entry("claude", MODEL, prompt, reply)
    print(f"Logged to {LOG_FILE}. Loop closed.")


if __name__ == "__main__":
    main()

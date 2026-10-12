"""
Anaxi -- Claude substrate, wired to the Anaxi Protocol's waking path.
prepare_context (Kardia + memory) -> Claude -> print + append to a running log.

Setup:
    Save this file INSIDE the unzipped anaxi_final/ folder, next to
    orchestration.py, anaxi_protocol.py, linguistic_pipeline.py, rem_prompts.py.
    (The imports below are relative to that folder.)

    pip install anthropic sentence-transformers faiss-cpu numpy
    setx ANTHROPIC_API_KEY "sk-ant-..."   (Windows -- then reopen your terminal)

    First run downloads the sentence-transformers embedding model (small,
    one-time, needs internet). If faiss-cpu gives you install trouble on
    this machine, swap the import at the top of orchestration.py from
    anaxi_protocol to anaxi_protocol_sqlite instead -- same interface,
    linear search instead of an index, no faiss dependency.

Run:
    python claude_anaxi.py "your prompt here"
"""

import os
import sys
import json
from datetime import datetime, timezone

import anthropic
from orchestration import AnaxiOrchestrator
from claude_native_waking_guard import refuse_claude_waking

MODEL = "claude-sonnet-5"
DB_PATH = "anaxi_mind.db"
USER_ID = "nate"          # the human steward this Kardia/memory belongs to
LOG_FILE = "anaxi_log.jsonl"


def to_claude_format(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anaxi builds messages OpenAI-style, with the system prompt as
    messages[0]. Claude's API wants the system prompt as its own
    parameter, not inside the messages list -- this split IS the
    'Claude adapter' the original proposal was describing."""
    if messages and messages[0].get("role") == "system":
        return messages[0]["content"], messages[1:]
    return "", messages


def call_claude(messages: list[dict], controls: dict) -> str:
    # claude-sonnet-5 rejects temperature/top_p outright (400: "temperature is
    # deprecated for this model") -- Kardia's aesthetic tuning still shapes the
    # system prompt's style_instruction, it just can't reach the API as sampling params.
    system, rest = to_claude_format(messages)
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=rest,
    )
    return response.content[0].text


def log_entry(prompt: str, reply: str, kardia: dict) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "substrate": "claude",
        "model": MODEL,
        "prompt": prompt,
        "response": reply,
        "kardia": kardia,
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def main():
    refuse_claude_waking("claude_anaxi.py::main")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY isn't set -- see the setup notes at the top of this file.")
        sys.exit(1)

    prompt = " ".join(sys.argv[1:]) or "Introduce yourself, Kardia and all."
    orch = AnaxiOrchestrator(DB_PATH)

    prepared = orch.prepare_context(USER_ID, prompt)
    print(f"[kardia] {prepared['kardia']}")
    if prepared["memory_context"]:
        print(f"[memory] {prepared['memory_context']}")

    reply = call_claude(prepared["messages"], prepared["controls"])
    print(f"\n[claude] {reply}")

    log_entry(prompt, reply, prepared["kardia"])
    orch.close()


if __name__ == "__main__":
    main()

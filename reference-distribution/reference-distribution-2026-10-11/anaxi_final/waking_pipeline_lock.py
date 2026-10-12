"""The one process-wide serialization point for the waking pipeline.

An ordinary Mac-originated waking turn and a Caret-originated waking occasion
both run Pass 1 / Pass 2 and mutate the same session state (working set,
direction owner) and canonical writers.  They must never overlap, so every
entry into ``llama_anaxi.run_waking_turn`` holds this lock, and so does every
owner control that rewrites the same session state.  Reentrant so a recovery
handler that itself calls ``run_waking_turn`` does not deadlock.
"""
import threading

LOCK = threading.RLock()

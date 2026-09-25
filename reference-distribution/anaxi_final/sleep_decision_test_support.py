"""Test support: Clark's typed post-reply Sleep choice (llama_anaxi.dispatch_clark_sleep_decision) is its own
third model call after Pass 1 and Pass 2. Fake chat functions that record "the model calls of a turn" call
``sleep_decision_response`` first: the choice is answered "none" (null/non-use) and kept out of the
Pass-1/Pass-2 call log those tests inspect. test_clark_sleep_request_from_reply.py scripts real choices."""
import json


def is_sleep_decision(fmt):
    return isinstance(fmt, dict) and "sleep_timing_request" in fmt.get("properties", {}) \
        and "act" not in fmt.get("properties", {})


def sleep_decision_response():
    return {"message": {"content": json.dumps({"alexs_words_asking_me_to_request_sleep": "", "my_words_asking_alex_for_sleep": "", "sleep_timing_request": "none"})},
            "prompt_eval_count": 1, "done": True, "done_reason": "stop", "eval_count": 5}

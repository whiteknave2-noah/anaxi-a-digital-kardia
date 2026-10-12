"""Exact, content-minimizing receipts for history-bearing provider calls.

The compositor can say what it intended to send; only the provider dispatch
boundary can say what was actually supplied.  This module keeps the intended
evidence text only in thread-local memory until ``begin_provider_call`` checks
it against the final call arguments.  Durable receipts contain hashes, byte
ranges, source identities and transport facts, never prompt or response text.

The caller persists a successful receipt atomically with the waking event.  A
failed/incomplete call is deliberately not returned by ``take_successful`` and
therefore cannot become a canonical delivery claim.
"""

import hashlib
import json
import threading


SCHEMA = "anaxi.provider_boundary_history_receipt.v1"
_state = threading.local()


class ProviderBoundaryReceiptError(RuntimeError):
    pass


def _sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha_text(value):
    return _sha_bytes(value.encode("utf-8"))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reset():
    _state.pending = None
    _state.active = None
    _state.successful = None
    _state.last_failure = None
    _state.ordinal = 0


def prepare(*, purpose, session_id, human_input_event_id, evidence):
    """Prepare one provider call without claiming that it has happened.

    Evidence entries may carry ``exact_text`` transiently.  Each entry marked
    ``expected_delivery`` must match exactly one final provider message at the
    boundary or the call fails closed before dispatch.
    """
    if getattr(_state, "pending", None) is not None or getattr(_state, "active", None) is not None:
        raise ProviderBoundaryReceiptError("a provider-boundary receipt is already pending")
    if not isinstance(purpose, str) or not purpose:
        raise ProviderBoundaryReceiptError("provider-boundary purpose is required")
    prepared = []
    for ordinal, raw in enumerate(evidence or ()):
        if not isinstance(raw, dict):
            raise ProviderBoundaryReceiptError("provider-boundary evidence must be mappings")
        item = dict(raw)
        item["evidence_ordinal"] = ordinal
        expected = item.get("expected_delivery") is True
        exact = item.get("exact_text")
        if expected and (not isinstance(exact, str) or not exact):
            raise ProviderBoundaryReceiptError("delivered evidence requires exact transient text")
        if exact is not None:
            item["evidence_sha256"] = _sha_text(exact)
            item["evidence_utf8_bytes"] = len(exact.encode("utf-8"))
        prepared.append(item)
    _state.pending = {
        "purpose": purpose,
        "session_id": session_id,
        "human_input_event_id": human_input_event_id,
        "evidence": prepared,
    }


def _message_projection(message, index):
    if not isinstance(message, dict):
        try:
            message = dict(message)
        except Exception as exc:
            raise ProviderBoundaryReceiptError("provider message is not a mapping") from exc
    content = message.get("content", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ProviderBoundaryReceiptError("provider message content is not text")
    images = message.get("images") or []
    image_digests = []
    for image in images:
        if isinstance(image, str):
            image = image.encode("utf-8")
        elif not isinstance(image, (bytes, bytearray, memoryview)):
            image = repr(image).encode("utf-8")
        else:
            image = bytes(image)
        image_digests.append(_sha_bytes(image))
    return {
        "index": index,
        "role": message.get("role"),
        "content_sha256": _sha_text(content),
        "content_utf8_bytes": len(content.encode("utf-8")),
        "image_sha256": image_digests,
    }, content


def begin_provider_call(*, messages, model, provider, options, format_value, think):
    """Bind prepared evidence to the exact final provider arguments.

    Called after all message/image/options transformations and immediately
    before dispatch.  The full request exists only long enough to hash it.
    """
    pending = getattr(_state, "pending", None)
    if pending is None:
        return None
    _state.pending = None
    _state.ordinal = int(getattr(_state, "ordinal", 0)) + 1

    projections = []
    contents = []
    exact_messages = []
    for index, message in enumerate(messages):
        projection, content = _message_projection(message, index)
        projections.append(projection)
        contents.append(content)
        # This exact-content representation is transient and used only for
        # the request digest.  Images are represented by their exact byte
        # digest so raw image material never enters JSON or durable state.
        exact_messages.append({
            "index": index,
            "role": projection["role"],
            "content": content,
            "image_sha256": projection["image_sha256"],
        })
    request_for_digest = {
        "provider": provider,
        "model": model,
        "messages": exact_messages,
        "format": format_value,
        "think": think,
        "options": options,
    }
    request_sha256 = _sha_text(_canonical(request_for_digest))

    bound_evidence = []
    for item in pending["evidence"]:
        durable = {k: v for k, v in item.items() if k != "exact_text"}
        exact = item.get("exact_text")
        expected = item.get("expected_delivery") is True
        matches = []
        if isinstance(exact, str):
            expected_role = item.get("expected_role")
            expected_index = item.get("expected_message_index")
            for index, content in enumerate(contents):
                if expected_index is not None and index != expected_index:
                    continue
                if expected_role is not None and projections[index]["role"] != expected_role:
                    continue
                start = 0
                while True:
                    found = content.find(exact, start)
                    if found < 0:
                        break
                    byte_start = len(content[:found].encode("utf-8"))
                    byte_end = byte_start + len(exact.encode("utf-8"))
                    matches.append((index, byte_start, byte_end))
                    start = found + max(1, len(exact))
        if expected:
            if len(matches) != 1:
                raise ProviderBoundaryReceiptError(
                    f"expected evidence ordinal {item['evidence_ordinal']} matched {len(matches)} provider ranges"
                )
            message_index, byte_start, byte_end = matches[0]
            durable.update({
                "delivered": True,
                "message_index": message_index,
                "message_role": projections[message_index]["role"],
                "message_content_sha256": projections[message_index]["content_sha256"],
                "utf8_byte_start": byte_start,
                "utf8_byte_end": byte_end,
            })
        else:
            durable["delivered"] = False
            durable.pop("expected_message_index", None)
            durable.pop("expected_role", None)
        bound_evidence.append(durable)

    receipt = {
        "schema": SCHEMA,
        "purpose": pending["purpose"],
        "call_ordinal": _state.ordinal,
        "binding": {
            "session_id": pending["session_id"],
            "human_input_event_id": pending["human_input_event_id"],
            "waking_event": "owning_event_component",
        },
        "request": {
            "request_sha256": request_sha256,
            "provider": provider,
            "model": model,
            "format_mode": "json_schema" if format_value is not None else "plain",
            "think": think,
            "options": dict(options),
            "message_count": len(projections),
            "messages": projections,
        },
        "evidence": bound_evidence,
    }
    token = request_sha256
    _state.active = (token, receipt)
    return token


def complete_provider_call(token, response):
    if token is None:
        return
    active = getattr(_state, "active", None)
    if active is None or active[0] != token:
        raise ProviderBoundaryReceiptError("provider-boundary completion token mismatch")

    def get(name, default=None):
        if isinstance(response, dict):
            return response.get(name, default)
        return getattr(response, name, default)

    message = get("message", {}) or {}
    content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
    content = content if isinstance(content, str) else ""
    receipt = active[1]
    receipt["completion"] = {
        "status": "completed",
        "done": get("done"),
        "done_reason": get("done_reason"),
        "prompt_eval_count": get("prompt_eval_count"),
        "eval_count": get("eval_count"),
        "response_content_chars": len(content),
    }
    _state.active = None
    _state.successful = receipt


def fail_provider_call(token, exc):
    if token is None:
        return
    _state.active = None
    _state.successful = None
    _state.last_failure = {
        "status": "failed_not_canonical",
        "exception_type": type(exc).__name__,
    }


def take_successful():
    receipt = getattr(_state, "successful", None)
    _state.successful = None
    return receipt


def last_failure_for_test():
    return getattr(_state, "last_failure", None)


def canonical_json(receipt):
    """Strict-enough public canonicalization used by the native writer."""
    if receipt is None:
        return None
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ProviderBoundaryReceiptError("invalid provider-boundary receipt schema")
    if receipt.get("purpose") != "pass2" or receipt.get("call_ordinal") != 1:
        raise ProviderBoundaryReceiptError("provider-boundary receipt is not the final ordinary pass2 call")
    binding = receipt.get("binding")
    request = receipt.get("request")
    completion = receipt.get("completion")
    evidence = receipt.get("evidence")
    if not isinstance(binding, dict) or binding.get("waking_event") != "owning_event_component":
        raise ProviderBoundaryReceiptError("invalid provider-boundary waking binding")
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        raise ProviderBoundaryReceiptError("invalid provider-boundary request")
    digest = request.get("request_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ProviderBoundaryReceiptError("invalid provider-boundary request digest")
    if (not isinstance(completion, dict)
            or completion.get("status") != "completed"
            or completion.get("done") is not True
            or completion.get("done_reason") != "stop"):
        raise ProviderBoundaryReceiptError("provider-boundary call did not complete")
    if not isinstance(evidence, list):
        raise ProviderBoundaryReceiptError("invalid provider-boundary evidence list")
    for ordinal, item in enumerate(evidence):
        if not isinstance(item, dict) or item.get("evidence_ordinal") != ordinal:
            raise ProviderBoundaryReceiptError("provider-boundary evidence order is invalid")
        if item.get("delivered") is True:
            if not all(isinstance(item.get(k), int) for k in (
                    "message_index", "utf8_byte_start", "utf8_byte_end")):
                raise ProviderBoundaryReceiptError("delivered evidence lacks exact provider range")
            if item["utf8_byte_end"] < item["utf8_byte_start"]:
                raise ProviderBoundaryReceiptError("delivered evidence range is invalid")
            message_index = item["message_index"]
            if not 0 <= message_index < len(request["messages"]):
                raise ProviderBoundaryReceiptError("delivered evidence message index is invalid")
            message = request["messages"][message_index]
            if (item.get("message_role") != message.get("role")
                    or item.get("message_content_sha256") != message.get("content_sha256")
                    or item["utf8_byte_end"] > message.get("content_utf8_bytes", -1)):
                raise ProviderBoundaryReceiptError("delivered evidence does not bind its provider message")
            evidence_digest = item.get("evidence_sha256")
            if (not isinstance(evidence_digest, str) or len(evidence_digest) != 64
                    or item.get("evidence_utf8_bytes") != item["utf8_byte_end"] - item["utf8_byte_start"]):
                raise ProviderBoundaryReceiptError("delivered evidence digest/size is invalid")
        elif item.get("delivered") is not False:
            raise ProviderBoundaryReceiptError("provider-boundary evidence delivery state is invalid")
        if "exact_text" in item:
            raise ProviderBoundaryReceiptError("provider-boundary receipt retained private evidence text")
    text = _canonical(receipt)
    if len(text.encode("utf-8")) > 262144:
        raise ProviderBoundaryReceiptError("provider-boundary receipt exceeds 262144 UTF-8 bytes")
    return text

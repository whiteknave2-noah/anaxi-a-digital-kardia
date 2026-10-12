"""PRIVATE DISCORD CORRESPONDENCE V0 -- network boundary.

The ONLY module in this capability that may touch the network. Exactly
two bounded, injectable operations against Discord's documented REST
API v10:

  fetch_channel_messages() -- GET one already-authorized channel's
                               recent messages (read-only).
  send_channel_message()   -- POST one message to one already-authorized
                               channel.

Matches this codebase's established network convention
(external_information_net.py, llama_anaxi.py's Ollama probe): stdlib
`urllib` only, always an explicit timeout, every outcome classified into
a closed status vocabulary and returned as a typed dict -- never a raw
exception escaping to a canonical write path, never a fabricated result
on failure.

Boundaries this module exists to hold:

  - The bot token is read from ANAXI_DISCORD_BOT_TOKEN (or an explicit
    caller override) and is only ever placed in the Authorization
    request header. It is never persisted, never returned, never logged,
    never embedded in an exception message.
  - There is no endpoint/environment override that could redirect the
    token: a custom api_base is honored only together with an injected
    test transport (never a real network call), exactly mirroring
    external_information_net.search_web()'s own rule.
  - No attachments, no embeds, no reactions, no edits, no deletes, no
    joins, no role/webhook/admin operations. The surface is exactly
    "list messages in one channel" and "post one text message".
  - A send result is honest about ambiguity: `definitive` is True only
    when Discord's own HTTP response proves the message was accepted
    (2xx) or definitively rejected without being sent (4xx/429); a
    timeout/connection error or a 5xx is reported with definitive=False,
    because this client cannot know whether the request was processed.
    The caller must map that to OUTCOME_NOT_ESTABLISHED, never resend.
"""
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from external_information_net import verified_ssl_context

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_FETCH_LIMIT = 25
MAX_FETCH_LIMIT = 100
MAX_RESPONSE_BYTES = 2_000_000
MAX_MESSAGE_CONTENT_CHARS = 2000

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_BOT_TOKEN_ENV_VAR = "ANAXI_DISCORD_BOT_TOKEN"

_USER_AGENT = "AnaxiPrivateDiscordCorrespondenceV0/1.0"
_SNOWFLAKE_RE = re.compile(r"\A[0-9]{5,32}\Z")

# ------------------------------------------------------------ statuses

STATUS_SUCCESS = "success"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_INVALID_TARGET = "invalid_target"
STATUS_HTTP_FAILURE = "http_failure"
STATUS_PROVIDER_FAILURE = "provider_failure"
STATUS_NETWORK_FAILURE = "network_failure"
STATUS_MALFORMED_RESPONSE = "malformed_response"

FAILURE_STATUSES = frozenset({
    STATUS_NOT_CONFIGURED, STATUS_INVALID_TARGET, STATUS_HTTP_FAILURE,
    STATUS_PROVIDER_FAILURE, STATUS_NETWORK_FAILURE, STATUS_MALFORMED_RESPONSE,
})


class DiscordTransportError(Exception):
    """Raised only for a genuine caller/programming error (a non-string
    target). Ordinary remote/network outcomes are returned as classified
    status dicts, never raised."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Authenticated Discord requests never follow redirects."""

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


def get_bot_token(explicit=None):
    """Read-only. Explicit value wins; otherwise the environment. Returns
    None when unconfigured. The returned token is never logged here."""
    if explicit is not None:
        return explicit if isinstance(explicit, str) and explicit.strip() else None
    value = os.environ.get(DISCORD_BOT_TOKEN_ENV_VAR)
    return value if isinstance(value, str) and value.strip() else None


def _require_snowflake(name, value):
    if not isinstance(value, str) or not _SNOWFLAKE_RE.match(value):
        raise DiscordTransportError(f"{name} must be a digits-only Discord snowflake string")


def _default_request(method, url, headers, body, timeout):
    """One HTTP request via stdlib urllib. Returns (status_code, body_bytes).
    An HTTPError is a valid response object and is returned as such; a
    transport-level failure raises (urllib.error.URLError/OSError/TimeoutError)."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    # Verified TLS with the pinned certifi bundle added (a python.org macOS
    # build ships no CA bundle: the bare default context failed every
    # Discord request with CERTIFICATE_VERIFY_FAILED, surfaced only as
    # "network request failed (URLError)"). Verification stays ON.
    opener = urllib.request.build_opener(
        _NoRedirectHandler(), urllib.request.HTTPSHandler(context=verified_ssl_context()),
    )
    try:
        # Authenticated Discord calls must never follow a provider or
        # intermediary redirect.  urllib's default opener follows 30x and
        # may carry caller headers to the new request; a fixed initial
        # origin is not sufficient protection by itself.
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            return response.getcode(), b""
        return response.getcode(), payload


def _resolve_request_fn(request_fn, api_base):
    if request_fn is not None:
        return request_fn
    if api_base is not None and api_base != DISCORD_API_BASE:
        raise DiscordTransportError(
            "a custom Discord api_base requires an injected test transport"
        )
    return _default_request


def _headers(token):
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }


def _classify_transport_exception(exc):
    if isinstance(exc, (urllib.error.URLError, OSError, TimeoutError, socket.timeout)):
        # Do not serialize exception text.  An injected adapter (or a
        # future stdlib error) could echo request headers, including the
        # credential, into its message.
        return {"status": STATUS_NETWORK_FAILURE, "definitive": False,
                "detail": f"network request failed ({type(exc).__name__})"}
    raise exc


def fetch_channel_messages(
    channel_id, *, token=None, after=None, limit=DEFAULT_FETCH_LIMIT,
    timeout=DEFAULT_TIMEOUT_SECONDS, request_fn=None, api_base=None,
) -> dict:
    """GET one channel's messages, oldest-first as Discord returns them.
    `after` (a snowflake) requests only messages newer than that cursor.
    Returns a closed-vocabulary dict; `messages` is always a list (empty
    on any failure). Never raises for an ordinary remote/network failure."""
    _require_snowflake("channel_id", channel_id)
    if not isinstance(limit, int) or limit < 1:
        raise DiscordTransportError("limit must be a positive integer")

    resolved_token = get_bot_token(token)
    if not resolved_token:
        return {"status": STATUS_NOT_CONFIGURED, "messages": [],
                "detail": f"no bot token configured ({DISCORD_BOT_TOKEN_ENV_VAR} unset)"}

    bounded_limit = min(int(limit), MAX_FETCH_LIMIT)
    params = {"limit": str(bounded_limit)}
    if after is not None:
        _require_snowflake("after", after)
        params["after"] = after
    base = api_base or DISCORD_API_BASE
    url = f"{base}/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}"

    sender = _resolve_request_fn(request_fn, api_base)
    try:
        status_code, body = sender("GET", url, _headers(resolved_token), None, timeout)
    except Exception as exc:  # noqa: BLE001 -- classified, never leaked
        return _classify_transport_exception(exc)

    if status_code is None or not (200 <= status_code < 300):
        return {
            "status": STATUS_HTTP_FAILURE, "messages": [],
            "http_status": status_code, "definitive": True,
        }
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        return {"status": STATUS_MALFORMED_RESPONSE, "messages": [],
                "detail": f"non-JSON response: {exc}"}
    if not isinstance(payload, list):
        return {"status": STATUS_MALFORMED_RESPONSE, "messages": [],
                "detail": "response is not a JSON array"}

    messages = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        message_id = item.get("id")
        if not isinstance(message_id, str) or not _SNOWFLAKE_RE.match(message_id):
            continue
        messages.append(item)
    return {"status": STATUS_SUCCESS, "messages": messages}


def send_channel_message(
    channel_id, content, *, token=None, timeout=DEFAULT_TIMEOUT_SECONDS,
    request_fn=None, api_base=None,
) -> dict:
    """POST exactly one text message. Returns a closed-vocabulary dict.
    On success: {"status": "success", "message_id", "definitive": True}.
    On a definitive rejection: {"status": "http_failure"/"provider_failure",
    "definitive": True}. On an ambiguous outcome: {"status":
    "network_failure"/"provider_failure", "definitive": False}.

    `content` is the exact payload -- this function never appends
    conversation/session context of any kind. Only transport fields
    (nonce omitted; no embeds/attachments/tts) accompany it."""
    _require_snowflake("channel_id", channel_id)
    if not isinstance(content, str) or not content.strip():
        return {"status": STATUS_INVALID_TARGET, "definitive": True,
                "detail": "content must be a nonempty string"}
    if len(content) > MAX_MESSAGE_CONTENT_CHARS:
        return {"status": STATUS_INVALID_TARGET, "definitive": True,
                "detail": f"content exceeds {MAX_MESSAGE_CONTENT_CHARS} characters"}

    resolved_token = get_bot_token(token)
    if not resolved_token:
        return {"status": STATUS_NOT_CONFIGURED, "definitive": True,
                "detail": f"no bot token configured ({DISCORD_BOT_TOKEN_ENV_VAR} unset)"}

    base = api_base or DISCORD_API_BASE
    url = f"{base}/channels/{channel_id}/messages"
    headers = _headers(resolved_token)
    headers["Content-Type"] = "application/json"
    body = json.dumps({"content": content}).encode("utf-8")

    sender = _resolve_request_fn(request_fn, api_base)
    try:
        status_code, response_body = sender("POST", url, headers, body, timeout)
    except Exception as exc:  # noqa: BLE001 -- classified, never leaked
        return _classify_transport_exception(exc)

    if status_code is not None and 200 <= status_code < 300:
        try:
            payload = json.loads(response_body)
        except (ValueError, TypeError) as exc:
            # Discord returned an accepting HTTP class, so automatic
            # resend is forbidden, but without a valid response identity
            # this client cannot truthfully establish the exact message.
            return {"status": STATUS_MALFORMED_RESPONSE, "definitive": False,
                    "accepted_http_status": True,
                    "detail": f"accepted response was non-JSON ({type(exc).__name__})"}
        message_id = payload.get("id") if isinstance(payload, dict) else None
        response_channel_id = payload.get("channel_id") if isinstance(payload, dict) else None
        if (
            not isinstance(message_id, str) or not _SNOWFLAKE_RE.match(message_id)
            or not isinstance(response_channel_id, str)
            or response_channel_id != channel_id
        ):
            return {"status": STATUS_MALFORMED_RESPONSE, "definitive": False,
                    "accepted_http_status": True,
                    "detail": "accepted response did not identify the expected channel/message"}
        return {
            "status": STATUS_SUCCESS,
            "message_id": message_id,
            "channel_id": response_channel_id,
            "definitive": True,
        }

    if status_code is not None and 400 <= status_code < 500:
        # Includes 429: Discord definitively did not accept the message.
        return {"status": STATUS_HTTP_FAILURE, "http_status": status_code, "definitive": True}
    # 5xx or a None status: ambiguous -- the request may or may not have
    # been processed.
    return {"status": STATUS_PROVIDER_FAILURE, "http_status": status_code, "definitive": False}

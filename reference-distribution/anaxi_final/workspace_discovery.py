"""Discovery -> selection for a supervised workspace action whose target the
subject has not (yet) named.

WHY THIS EXISTS. An ordinary request such as "read something from the library"
reaches the workspace selector with no way for the subject to know what the
library holds: it has never seen a listing. Live, the subject's Pass 1 answered
with ``read`` and an EMPTY ``relative_path`` (and likewise for a photograph and
for music); the host correctly refused to open nothing, and the subject was left
with "the path was empty" -- a capability that could only work if the owner
supplied a raw host path.

THE LAW. The host establishes facts; the subject chooses. When an action that
acts on ONE named item arrives without an existing item, the host does what the
subject could not: it LISTS (read-only, the existing ``list`` action, same
authority, same bounds), shows the subject the listing as a truthful window,
and lets the SUBJECT choose the item (or navigate: open a directory, ask for the
next page). The host never picks the item, never substitutes one, and never
invents one; a choice that still names nothing real ends the step with a
truthful receipt that carries the listing, so nothing the host obtained is lost.

This module is pure decision logic (no model call, no I/O beyond resolving
paths inside the class root). The supervisor owns the prompt and the calls.
"""
import json
import os

import workspace_capability as wc
import workspace_direction as wd

# Bounded, not a loop of unbounded autonomy: the subject may look at a listing,
# open a directory / turn a page, and choose. Each selection is one model call.
MAX_SELECTION_ROUNDS = 4

# Outcomes of interpreting the subject's answer to a listing.
RESOLVED = "resolved"        # an existing item was chosen; act on it
NAVIGATE = "navigate"        # subject asked to see another directory/page/query
UNRESOLVED = "unresolved"    # nothing real was chosen
FINISHED = "finished"        # the subject chose to stop after seeing the listing
STOP_ACTION = "none"         # the subject's neutral "stop here" answer to a listing

REASON_TEXT = {
    wc.TARGET_NOT_SPECIFIED: "no item was named",
    wc.TARGET_NOT_FOUND: "the named item does not exist",
    wc.TARGET_IS_DIRECTORY: "the named path is a directory, not an item",
}


def discovery_needed(paths, validated_action):
    """(needed, status, normalized_action). Needed only for an action on ONE
    item whose target is absent / not an existing file / a directory. A path
    that escapes the class root is NOT redirected to discovery: it is refused
    truthfully by the executor (authority is never widened by being helpful)."""
    action = wd.normalize_workspace_action(paths, validated_action)
    resource_class, name = action["resource_class"], action["action"]
    if resource_class not in wc.RESOURCE_CLASSES or not wc.is_target_bearing(resource_class, name):
        return False, wc.TARGET_RESOLVED, action
    status, _normalized = wc.classify_target(paths, resource_class, action["relative_path"])
    if status in (wc.TARGET_RESOLVED, wc.TARGET_OUTSIDE_ROOT):
        return False, status, action
    return True, status, action


def _existing_parent_directory(paths, resource_class, relative_path):
    """Nearest existing directory named by (a prefix of) ``relative_path``;
    "" (the class root) when there is none. Journals are flat."""
    if resource_class == wc.JOURNAL or not isinstance(relative_path, str):
        return ""
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p]
    while parts:
        candidate = "/".join(parts)
        try:
            real = wc.resolve_workspace_path(paths, resource_class, candidate)
        except wc.PathEscapeError:
            parts.pop()
            continue
        if os.path.isdir(real):
            return candidate
        parts.pop()
    return ""


def discovery_list_action(paths, validated_action):
    """The read-only ``list`` that shows the subject what exists where it was
    looking: the directory it named, else the nearest existing parent, else the
    class root."""
    resource_class = validated_action["resource_class"]
    directory = _existing_parent_directory(paths, resource_class, validated_action["relative_path"])
    named = _named_text(validated_action["relative_path"], directory)
    if named and resource_class != wc.JOURNAL:
        # What the subject NAMED narrows the listing to the items whose path contains it (plain,
        # case-insensitive -- never semantic); the subject still chooses.  Live 2026-09-24: "255520" named
        # one scanned book exactly, the host listed the whole library, and the subject picked the book the
        # conversation had just been about (3/3).  Nothing matching -> the ordinary listing, unchanged.
        try:
            matches = wc._browse_entries(paths, resource_class, directory, named)
        except (wc.PathEscapeError, NotADirectoryError, OSError):
            matches = []
        if matches:
            return {"resource_class": resource_class, "action": wc.LIST, "relative_path": directory,
                    "content": json.dumps({"query": named})}
    return {"resource_class": resource_class, "action": wc.LIST, "relative_path": directory, "content": ""}


def _named_text(relative_path, directory):
    """The part of a non-resolving target the subject actually named beyond its existing folder."""
    if not isinstance(relative_path, str):
        return ""
    cleaned = relative_path.strip().replace("\\", "/").strip("/")
    if directory and cleaned.startswith(directory.rstrip("/") + "/"):
        cleaned = cleaned[len(directory.rstrip("/")) + 1:]
    return cleaned.strip() if 2 <= len(cleaned.strip()) <= 120 else ""


def selection_task_text(original_action, status, listing_text, allowed_surface_text):
    """The task text for a selection call. Purely mechanical framing of the
    fact and the options; it names no item, expresses no preference and does not
    ask the subject to act: stopping (``none``) is always an equal answer.

    ``status`` is a TARGET_* kind when the host listed because the subject's
    item action named no existing item; ``None`` when the subject itself chose
    ``list`` and is being offered the chance to continue from what it saw."""
    resource_class, action = original_action["resource_class"], original_action["action"]
    if status is None:
        opening = (
            f"You listed {resource_class}"
            + (f" ({original_action['relative_path']})" if original_action.get("relative_path") else "")
            + ". The result:\n\n"
        )
        item_step = "To act on a listed item, set resource_class and action to the item action you want and "
    else:
        reason = REASON_TEXT.get(status, "no item was named")
        opening = (
            f"You chose to {action} an item in {resource_class}, but {reason}. "
            "The host listed what is there (read-only; nothing was opened):\n\n"
        )
        item_step = f"To {action}, set resource_class to {resource_class}, action to {action}, and "
    return (
        f"{opening}{listing_text}\n\n"
        "Choose your next step with the same JSON shape. "
        f"{item_step}relative_path to one listed entry exactly as written. "
        "An entry ending in \"/\" is a directory: to look inside, list it (relative_path). "
        "For another resource (e.g. your journal), list it (resource_class). "
        "To see more entries, set action to list with its next_request. "
        f"To stop here, set action to \"{STOP_ACTION}\".\n\n"
        f"Available workspace capabilities: {allowed_surface_text}"
    )


def _is_stop(raw_selection):
    """The subject's explicit "stop here". Nothing else is read as a stop: an
    unparseable or unlisted answer is a failure, never a silent no-op."""
    raw = raw_selection
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return False
    return isinstance(raw, dict) and raw.get("action") == STOP_ACTION


def _relative_to_listing(paths, action, listing_action):
    """A listing of folder D shows its entries relative to D, and the subject is told to use an entry exactly
    as written -- but actions resolve from the class root.  Live 2026-09-24: inside photographs/Family/ the
    subject chose "Blair/" as written; it was refused LIST_NOT_DIRECTORY (the folder is Family/Blair/) and no
    photograph was ever viewed.  An entry that exists relative to the listed folder, and not from the root,
    is resolved against that folder -- only when that exact path exists; nothing is guessed."""
    directory = (listing_action.get("relative_path") or "").strip().strip("/")
    named = (action.get("relative_path") or "").strip()
    if (not directory or not named or action["resource_class"] != listing_action["resource_class"]
            or action["resource_class"] not in wc.RESOURCE_CLASSES or action["resource_class"] == wc.JOURNAL):
        return action
    def exists(rel):
        try:
            real = wc.resolve_workspace_path(paths, action["resource_class"], rel.rstrip("/") or rel)
        except (wc.PathEscapeError, wc.EmptyTargetError):
            return False
        return os.path.exists(real)
    if exists(named):
        return action
    joined = f"{directory}/{named.lstrip('/')}"
    return dict(action, relative_path=joined) if exists(joined) else action


def interpret_selection(paths, original_action, listing_action, raw_selection, allowed_surface):
    """Interpret the subject's answer to a listing. Returns
    ``(outcome, action_or_None, failure_code_or_None)``.

    - An action on an existing item of the SAME class -> RESOLVED with that
      exact (normalized) action. The subject may also change its mind to a
      different item-level action (e.g. inspect_metadata); the host does not
      constrain the subject's choice beyond the allowed surface and the class.
    - ``list`` in the same class -> NAVIGATE (a directory, a page, a query).
    - Anything else (malformed, other class, still no real item) -> UNRESOLVED.
    """
    if _is_stop(raw_selection):
        return FINISHED, None, None
    validated, failure = wd.validate_pass1_workspace_action(raw_selection, allowed_surface)
    if failure is not None:
        return UNRESOLVED, None, failure
    validated = wd.normalize_workspace_action(paths, validated)
    validated = _relative_to_listing(paths, validated, listing_action)
    if validated["resource_class"] != listing_action["resource_class"]:
        # The subject may look at ANOTHER kind of resource (a list of what it wrote rather
        # than the reference library); it must see that listing before acting on an item
        # there, so only a list is followed across classes.
        if validated["action"] == wc.LIST and validated["resource_class"] in wc.RESOURCE_CLASSES:
            return NAVIGATE, dict(validated, relative_path=""), None
        return UNRESOLVED, None, "SELECTION_WRONG_RESOURCE_CLASS"
    if validated["action"] == wc.LIST:
        return NAVIGATE, validated, None
    if wc.is_target_bearing(validated["resource_class"], validated["action"]):
        status, normalized = wc.classify_target(paths, validated["resource_class"], validated["relative_path"])
        if status == wc.TARGET_RESOLVED:
            return RESOLVED, dict(validated, relative_path=normalized), None
        return UNRESOLVED, None, status
    return UNRESOLVED, None, "SELECTION_NOT_AN_ITEM_ACTION"


def unresolved_boundary_result(original_action, status, last_listing_boundary_result, failure_code):
    """Truthful receipt for a step that ended with no real item chosen. It
    carries the last listing the host obtained (so the subject still receives
    real material) and says plainly that nothing was opened."""
    reason = failure_code or status
    listing = last_listing_boundary_result.get("result") if last_listing_boundary_result else None
    return {
        "action": original_action["action"],
        "scope": f"local_workspace/{original_action['resource_class']}",
        "boundary": (last_listing_boundary_result or {}).get("boundary"),
        "consequence": "No item was opened; source remains unchanged.",
        "rationale": f"TARGET_NOT_SELECTED ({reason}): the host listed what exists; no existing item was chosen.",
        "result": listing,
    }


# ------------------------------------------------ text-less PDF -> page image
#
# A scanned / image-only PDF has no text layer, so ``read`` truthfully fails
# with PDF_TEXT_UNAVAILABLE and names the alternative (``view_page``: real
# pixels of a chosen page). Owner law: that document must still be usable by
# the subject, so the subject is offered ONE neutral follow-up choice (view a
# page of it, or stop) instead of the turn ending on the failure. The host
# never picks the page.


def text_layer_alternative_available(validated_action, performed, boundary_result):
    return (
        not performed
        and validated_action["resource_class"] == wc.LIBRARY
        and validated_action["action"] == wc.READ
        and str((boundary_result or {}).get("rationale", "")).startswith(wc.PDF_TEXT_UNAVAILABLE)
    )


def alternative_boundary_result(boundary_result):
    """The failed receipt, shaped so the ordinary windowing/costing treats it
    like any other observation the subject is shown."""
    return dict(boundary_result, result={"receipt": boundary_result["rationale"]})


def alternative_task_text(validated_action, observation_text, allowed_surface_text):
    return (
        f"You chose to {validated_action['action']} {validated_action['relative_path']} in library. The result:\n\n"
        f"{observation_text}\n\n"
        "Choose your next step with the same JSON shape. To view a page of this same document as an image, set "
        f"resource_class to library, action to {wc.VIEW_PAGE}, relative_path to {validated_action['relative_path']}, and "
        "content to {\"page\": <number>}. "
        f"To stop here, set action to \"{STOP_ACTION}\".\n\n"
        f"Available workspace capabilities: {allowed_surface_text}"
    )


def interpret_alternative(paths, original_action, raw_selection, allowed_surface):
    """(outcome, action|None, failure|None): RESOLVED only for view_page on the
    SAME document; everything else (including an explicit stop) leaves the
    original truthful receipt as the result."""
    if _is_stop(raw_selection):
        return FINISHED, None, None
    validated, failure = wd.validate_pass1_workspace_action(raw_selection, allowed_surface)
    if failure is not None:
        return UNRESOLVED, None, failure
    validated = wd.normalize_workspace_action(paths, validated)
    same_document = (
        validated["resource_class"] == wc.LIBRARY and validated["action"] == wc.VIEW_PAGE
        and validated["relative_path"] == original_action["relative_path"]
    )
    if not same_document:
        return UNRESOLVED, None, "ALTERNATIVE_NOT_OFFERED"
    return RESOLVED, validated, None


# ---------------------------------------------------- a required parameter was not chosen
#
# LIVE FINDING (scanned/image-PDF checkout): the subject selected the document and chose
# ``view_page`` -- and left ``content`` empty. The executor read "no page" as a malformed payload,
# so no page image ever reached waking (rejected at validation: not even an action-log record).
# It is the same failure class as an item action with no item: the subject chose an action but
# lacks a fact it needs (which pages exist; which views exist). The host states the fact and the
# SUBJECT chooses the parameter; the host never picks a page or a view.


def _page_choice_needed(paths, action):
    if action["resource_class"] != wc.LIBRARY or action["action"] != wc.VIEW_PAGE:
        return None
    count = wc.pdf_page_count(paths, action["relative_path"])
    if not count:
        return None
    payload, failure = wd.parse_pdf_page_content(action["content"])
    if failure is None and payload["page"] <= count:
        return None
    return {
        "reason": "no valid page number was given" if failure is not None else f"page {payload['page']} does not exist",
        "facts": {"document": action["relative_path"], "page_count": count, "valid_pages": f"1-{count}",
                  "how": 'set content to {"page": 1} (any page number from valid_pages)'},
    }


def _audio_view_choice_needed(paths, action):
    import workspace_audio as wa

    if action["resource_class"] != wc.MUSIC or action["action"] != wc.INSPECT_AUDIO:
        return None
    _payload, failure = wa.parse_inspect_audio_payload(action["content"])
    if failure is None:
        return None
    return {
        "reason": "no valid view was given",
        "facts": {"item": action["relative_path"], "available_views": list(wa.ALL_KNOWN_VIEWS),
                  "how": 'set content to {"view": "<one of available_views>"}; optional "start_seconds" and "end_seconds"'},
    }


def parameter_choice_needed(paths, action):
    """None, or ``{"reason", "facts", "boundary_result"}`` for a RESOLVED item action whose required
    parameter is absent/invalid and for which the host has real facts to show the subject."""
    if not wc.is_target_bearing(action["resource_class"], action["action"]):
        return None
    if wc.classify_target(paths, action["resource_class"], action["relative_path"])[0] != wc.TARGET_RESOLVED:
        return None
    need = _page_choice_needed(paths, action) or _audio_view_choice_needed(paths, action)
    if need is None:
        return None
    need["boundary_result"] = {
        "action": action["action"], "scope": f"local_workspace/{action['resource_class']}",
        "boundary": None, "consequence": "Nothing was opened yet; source remains unchanged.",
        "rationale": f"PARAMETER_NOT_CHOSEN: {need['reason']}.", "result": need["facts"],
    }
    return need


def parameter_task_text(action, need, observation_text, allowed_surface_text):
    return (
        f"You chose to {action['action']} {action['relative_path']} in {action['resource_class']}, but "
        f"{need['reason']}. The host reports:\n\n{observation_text}\n\n"
        "Choose your next step with the same JSON shape: keep resource_class, action and relative_path, "
        f"and give content as described. To stop here, set action to \"{STOP_ACTION}\".\n\n"
        f"Available workspace capabilities: {allowed_surface_text}"
    )


def interpret_parameter_choice(paths, original_action, raw_selection, allowed_surface):
    """(outcome, action|None, failure|None): RESOLVED only for the SAME action on the SAME item with
    a parameter that now validates; an explicit stop is FINISHED; anything else UNRESOLVED."""
    if _is_stop(raw_selection):
        return FINISHED, None, None
    validated, failure = wd.validate_pass1_workspace_action(raw_selection, allowed_surface)
    if failure is not None:
        return UNRESOLVED, None, failure
    if not validated.get("relative_path") and (validated["resource_class"], validated["action"]) == (
            original_action["resource_class"], original_action["action"]):
        # The question asks only for the parameter and the item was the subject's own earlier choice; a
        # reply that leaves relative_path blank names no other item, so it stays on the chosen one.
        validated = dict(validated, relative_path=original_action["relative_path"])
    validated = wd.normalize_workspace_action(paths, validated)
    same = all(validated[k] == original_action[k] for k in ("resource_class", "action", "relative_path"))
    if not same:
        return UNRESOLVED, None, "PARAMETER_CHOICE_CHANGED_THE_ACTION"
    if parameter_choice_needed(paths, validated) is not None:
        return UNRESOLVED, None, "PARAMETER_STILL_INVALID"
    return RESOLVED, validated, None


class SelectionUnavailable(Exception):
    """The selection call could not be made honestly (e.g. even the fixed text
    of the call exceeds the prompt budget). Ends discovery as UNRESOLVED with
    this code; it is never turned into a guessed choice."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def resolve_target(paths, requester_actor_id, original_action, status, ask_selection, allowed_surface, *,
                   session_id=None, source_event_id=None, initial_listing=None):
    """Run the bounded discovery -> selection exchange.

    ``ask_selection(listing_boundary_result, original_action, status)`` makes
    ONE selection call and returns its raw JSON text (or raises
    SelectionUnavailable). Returns a dict:
    ``{"outcome": RESOLVED|UNRESOLVED, "action": <resolved action or None>,
    "listing_steps": int, "last_listing": <last successful listing or None>,
    "failure": <code or None>}``.
    """
    if initial_listing is None:
        listing_action = discovery_list_action(paths, original_action)
    else:
        # The subject itself chose ``list``: that listing is already in hand.
        listing_action = original_action
    listing_steps = 0
    last_listing = None
    for _ in range(MAX_SELECTION_ROUNDS):
        if initial_listing is not None and listing_steps == 0:
            listing, performed = initial_listing, True
        else:
            listing, performed = wd.execute_workspace_action(
                paths, listing_action, requester_actor_id,
                session_id=session_id, source_event_id=source_event_id,
            )
        listing_steps += 1
        if not performed:
            return {"outcome": UNRESOLVED, "action": None, "listing_steps": listing_steps,
                    "last_listing": last_listing, "failure": listing.get("rationale") or "LISTING_FAILED"}
        last_listing = listing
        try:
            raw = ask_selection(listing, listing_action if status is None else original_action, status)
        except SelectionUnavailable as exc:
            return {"outcome": UNRESOLVED, "action": None, "listing_steps": listing_steps,
                    "last_listing": last_listing, "failure": exc.code}
        outcome, action, failure = interpret_selection(paths, original_action, listing_action, raw, allowed_surface)
        if outcome == RESOLVED:
            return {"outcome": RESOLVED, "action": action, "listing_steps": listing_steps,
                    "last_listing": last_listing, "failure": None}
        if outcome == NAVIGATE:
            if action["resource_class"] != listing_action["resource_class"]:
                # The subject moved to another kind of resource: from here it is simply
                # looking at a listing it asked for, not a failed item action.
                status = None
            listing_action = action
            continue
        if outcome == FINISHED:
            return {"outcome": FINISHED, "action": None, "listing_steps": listing_steps,
                    "last_listing": last_listing, "failure": None}
        return {"outcome": UNRESOLVED, "action": None, "listing_steps": listing_steps,
                "last_listing": last_listing, "failure": failure}
    return {"outcome": UNRESOLVED, "action": None, "listing_steps": listing_steps,
            "last_listing": last_listing, "failure": "SELECTION_ROUNDS_EXHAUSTED"}


def describe_discovery(steps, resolved, subject_initiated=False):
    """One mechanical sentence for the Pass 2 receipt."""
    if not steps:
        return ""
    if subject_initiated:
        return (
            f"You listed this resource earlier in this turn"
            + (" and then chose the item acted on." if resolved else "; you then chose to stop, so nothing was opened.")
        )
    if resolved:
        return (
            f"Before acting, the host listed this resource {steps} time(s) so you could choose an item; "
            "the item acted on is the one you chose."
        )
    return (
        f"The host listed this resource {steps} time(s) so you could choose an item; "
        "you did not choose an existing item, so nothing was opened."
    )


__all__ = [
    "MAX_SELECTION_ROUNDS", "RESOLVED", "NAVIGATE", "UNRESOLVED", "FINISHED", "STOP_ACTION",
    "discovery_needed", "discovery_list_action", "selection_task_text",
    "interpret_selection", "unresolved_boundary_result", "describe_discovery",
    "SelectionUnavailable", "resolve_target",
    "parameter_choice_needed", "parameter_task_text", "interpret_parameter_choice",
    "text_layer_alternative_available", "alternative_boundary_result", "alternative_task_text", "interpret_alternative",
]

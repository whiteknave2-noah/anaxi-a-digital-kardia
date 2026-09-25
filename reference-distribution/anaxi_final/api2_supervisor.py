"""API2-LR1-S1: one-shot supervisor execution surface for the future
live authoritative decision gate (API2-S2, spec section 22/23).

NOT imported by ordinary waking (orchestration.py/llama_anaxi.py/
run_waking_turn()). Production default authority remains OFF --
`run_one_authoritative_decision()` requires an explicit `enabled=True`
override passed to BOTH api1_control_plane's and api2_control_plane's
gated functions on this one call; it never mutates
`api1_control_plane.AUTHORITATIVE_PDE_ENABLED` or
`api2_control_plane.AUTHORITATIVE_DECISION_PATH_ENABLED`, which remain
their persistent `False` module defaults before, during, and after
every call -- there is nothing to restore in a `finally`, by design
(spec section 24: no persistent flip, ever). `_in_process_authority_
scope()` exists anyway, as a syntactically explicit, narrow window
around the one place authority is exercised, rather than leaving that
implicit across the whole function body -- and it re-asserts the
persistent defaults are still False on the way out.

Capability boundary (spec section 25, stated plainly so it is never
overclaimed elsewhere): the `Capability` this module mints is an
OPERATIONAL boundary against accidental or ordinary-path activation --
it proves a call went through this supervisor's own one-shot flow, not
through some incidental code path, since nothing outside this module
constructs a valid `Capability` for a real decision. It is NOT a
security boundary against an attacker who already has arbitrary
trusted-process Python execution: such an attacker could call
`api1_control_plane`/`api2_control_plane` functions directly with
`enabled=True`, exactly as this module does. AAB's own authentication
threat model (OS-principal/session binding) is separate and
unaffected by any of this.
"""
import contextlib

import api1_control_plane as api1
import api2_control_plane as api2


@contextlib.contextmanager
def _in_process_authority_scope():
    try:
        yield
    finally:
        assert api1.AUTHORITATIVE_PDE_ENABLED is False
        assert api2.AUTHORITATIVE_DECISION_PATH_ENABLED is False


def run_one_authoritative_decision(
    prov_conn, aab_store, tx1_raw_request, auth_context_id, session_id,
    clark_actor_id, production_session_id, prepare_callable, pass1_callable,
    pass2_callable, model_name, think,
):
    """The ONE supervisor-only entry point chaining TX1 through TX2 for
    exactly one decision. Requires a real, currently-valid AAB
    authenticated session for TX1 (api1_control_plane's own
    `_revalidate_aab`, unchanged -- this module does not weaken or
    bypass it). Mints exactly one decision-scoped Capability
    internally; there is no parameter through which a caller could
    hand this function a pre-existing Capability to reuse.

    Returns (result, None) on a fully committed TX2, or
    (None, {"stage": <str>, "failure": <original ApiFailure/Api2Failure>})
    on any failure -- nothing partial is ever treated as success.
    """
    if api1.AUTHORITATIVE_PDE_ENABLED is not False:
        return None, {"stage": "preflight", "failure": "AUTHORITATIVE_PDE_ENABLED_NOT_DEFAULT_FALSE"}
    if api2.AUTHORITATIVE_DECISION_PATH_ENABLED is not False:
        return None, {"stage": "preflight", "failure": "AUTHORITATIVE_DECISION_PATH_ENABLED_NOT_DEFAULT_FALSE"}

    with _in_process_authority_scope():
        tx1_result, tx1_failure = api1.submit_authoritative_create_and_assign(
            prov_conn, aab_store, tx1_raw_request, auth_context_id, session_id,
            clark_actor_id, production_session_id, enabled=True,
        )
        if tx1_failure is not None:
            return None, {"stage": "tx1", "failure": tx1_failure}

        decision = tx1_result["decision"]
        decision_id = decision["decision_id"]
        capability = api2.mint_capability(decision_id)

        result, api2_failure = api2.execute_authoritative_owner_route(
            prov_conn, capability, decision_id, clark_actor_id,
            decision["last_transition_event_id"], prepare_callable, pass1_callable,
            pass2_callable, model_name, think, enabled=True,
        )
        if api2_failure is not None:
            return None, {"stage": "api2", "failure": api2_failure}

    return result, None

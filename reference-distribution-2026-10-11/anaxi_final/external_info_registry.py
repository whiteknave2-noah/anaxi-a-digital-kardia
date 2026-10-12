"""READ-ONLY EXTERNAL INFORMATION CAPABILITY V0 -- event-model registry.

Mirrors boundary_rationale_registry.py's own "event model" section: a
tiny, static, host-metadata module naming the canonical event type and
component kinds this capability writes. No evaluation logic, no DB
access, no network access -- purely named constants so
external_information.py, native_provenance_writer.py, and this
capability's tests all agree on the exact same literal strings rather
than each hand-rolling their own copy.
"""

# ------------------------------------------------------------- event model

EXTERNAL_INFO_EVENT_TYPE = "clark_external_info_query"

EXTERNAL_INFO_QUERY_SPEC_COMPONENT_KIND = "external_info_query_spec"
EXTERNAL_INFO_DISPATCH_STARTED_COMPONENT_KIND = "external_info_dispatch_started"
EXTERNAL_INFO_RESULT_COMPONENT_KIND = "external_info_result"
EXTERNAL_INFO_DELIVERED_COMPONENT_KIND = "external_info_delivered"

# The canonical CARRIAGE component kind, written onto a genuine
# waking_turn event (never a clark_external_info_query event) at the
# exact canonical waking-turn transaction that persists the real Pass-2
# composition's surviving external-info source id -- exactly mirroring
# boundary_rationale_registry.BOUNDARY_INSPECTION_RESULT_CARRIAGE_COMPONENT_KIND.
# record_external_info_delivered() can only read this canonical fact;
# it cannot write or independently assert carriage.
EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND = "external_info_result_carriage"

# The two component kinds a Pass-1 structured external-info request can
# create on the TRIGGERING waking_turn event itself (never on the
# clark_external_info_query event) -- exactly mirroring
# native_provenance_writer.OPERATIVE_DIRECTIVE_REQUEST_COMPONENT_KIND /
# OPERATIVE_DIRECTIVE_TEXT_COMPONENT_KIND. This is the durable, atomic
# proof that Clark himself explicitly chose this operation on this
# exact turn -- never inferred from prose, never fabricated after the
# fact.
EXTERNAL_INFO_REQUEST_COMPONENT_KIND = "external_info_request"
EXTERNAL_INFO_TARGET_COMPONENT_KIND = "external_info_target"

# The four component kinds a clark_external_info_query event can carry,
# in append order:
#   sequence 0 -- external_info_query_spec (the canonical query occurrence)
#   sequence 1 -- external_info_dispatch_started (durable before network;
#                 prevents an uncertain crash from silently redispatching)
#   sequence 2 -- external_info_result (the persisted, possibly-bounded
#                 result -- search results or fetched-page text)
#   sequence 3 -- external_info_delivered (delivery marker, only ever
#                 appended AFTER a waking turn that actually carried the
#                 result in its real Pass-2 input committed)
QUERY_EVENT_COMPONENT_KINDS = (
    EXTERNAL_INFO_QUERY_SPEC_COMPONENT_KIND,
    EXTERNAL_INFO_DISPATCH_STARTED_COMPONENT_KIND,
    EXTERNAL_INFO_RESULT_COMPONENT_KIND,
    EXTERNAL_INFO_DELIVERED_COMPONENT_KIND,
)

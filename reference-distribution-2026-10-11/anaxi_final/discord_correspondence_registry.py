"""PRIVATE DISCORD CORRESPONDENCE V0 -- event-model registry.

Mirrors external_info_registry.py's own tiny, static, host-metadata
shape: named constants only, no DB access, no network access, no
evaluation logic. discord_correspondence.py, native_provenance_writer.py,
and this capability's tests all agree on the exact same literal strings
through this module rather than each hand-rolling its own copy.

This capability never contacts Discord from here; the narrow REST
transport lives in discord_correspondence_net.py and is invoked only by
discord_correspondence.py's own dispatch/recovery functions.
"""

# ------------------------------------------------- destination registry

# A destination is a Discord CHANNEL or a DM channel, named by its own
# numeric snowflake. destination_id is an ANAXI-local stable id derived
# from (kind, snowflake) -- never the raw snowflake itself in any
# canonical reference field.
DESTINATION_KIND_CHANNEL = "channel"
DESTINATION_KIND_DM_CHANNEL = "dm_channel"
DESTINATION_KINDS = (DESTINATION_KIND_CHANNEL, DESTINATION_KIND_DM_CHANNEL)

REGISTRY_ACTION_AUTHORIZE = "authorize"
REGISTRY_ACTION_REVOKE = "revoke"
REGISTRY_ACTIONS = (REGISTRY_ACTION_AUTHORIZE, REGISTRY_ACTION_REVOKE)

# The ANAXI-local recipient reference recorded on every clark_outward_act
# this capability projects. OC0's own invariant: recipient_reference is
# never a transport account/channel identity -- this keeps that true
# while still naming the exact destination unambiguously.
RECIPIENT_REFERENCE_PREFIX = "discord_destination:"

# ------------------------------------------------------------- inbound

# A canonical occurrence event for one received Discord message. Owned
# structurally by the host, never by Clark and never by the remote
# author -- auth_context_id is NULL (see discord_correspondence.py), so
# no human/agent authentication is ever borrowed by external content.
DISCORD_INBOUND_MESSAGE_EVENT_TYPE = "discord_inbound_message"

# Inbound event component kinds, in append order:
#   sequence 0 -- discord_inbound_content  (the exact received text;
#                 untrusted external data, never an instruction)
#   sequence 1 -- discord_inbound_metadata (JSON: destination id/kind,
#                 discord message id, author id/name, channel id, ts)
#   sequence 2 -- discord_inbound_delivered (delivery marker, appended
#                 only AFTER a genuine waking turn that actually carried
#                 this message in its real Pass-2 composition committed)
DISCORD_INBOUND_CONTENT_COMPONENT_KIND = "discord_inbound_content"
DISCORD_INBOUND_METADATA_COMPONENT_KIND = "discord_inbound_metadata"
DISCORD_INBOUND_DELIVERED_COMPONENT_KIND = "discord_inbound_delivered"

# The canonical CARRIAGE component kind, written onto a genuine
# waking_turn event (never a discord_inbound_message event) at the exact
# canonical waking-turn transaction that persisted the real Pass-2
# composition's surviving inbound source event id -- exactly mirroring
# external_info_registry.EXTERNAL_INFO_RESULT_CARRIAGE_COMPONENT_KIND.
# record_inbound_delivered() can only read this canonical fact.
DISCORD_INBOUND_CARRIAGE_COMPONENT_KIND = "discord_inbound_carriage"

# -------------------------------------------- outbound typed request

# The three component kinds Clark's explicit Pass-1 structured
# correspondence request can create on the TRIGGERING waking_turn event
# itself (never on the clark_outward_act event) -- exactly mirroring
# external_info_registry.EXTERNAL_INFO_REQUEST_COMPONENT_KIND. This is
# the durable, atomic proof that Clark himself explicitly chose to send
# this exact text to this exact destination on this exact turn.
DISCORD_CORRESPONDENCE_REQUEST_COMPONENT_KIND = "discord_correspondence_request"
DISCORD_DESTINATION_ID_COMPONENT_KIND = "discord_destination_id"
DISCORD_MESSAGE_TEXT_COMPONENT_KIND = "discord_message_text"

# Caret waking OCCASION turns only (new turns; never fabricated for history):
#   caret_correspondent_principal  the canonical ANAXI principal the inbound author id resolved to
#                                  at delivery (host-authored; text = principal actor id)
#   caret_reply_route_intent       Clark's explicit Pass-1 reply_through_caret choice, recorded
#                                  BEFORE body validation, so "chose the route, body unusable"
#                                  is durably distinguishable from "chose nothing".  It is not an
#                                  obligation to dispatch; the send components exist only when the
#                                  body was usable.
CARET_CORRESPONDENT_PRINCIPAL_COMPONENT_KIND = "caret_correspondent_principal"
CARET_REPLY_ROUTE_INTENT_COMPONENT_KIND = "caret_reply_route_intent"

# ------------------------------------- outbound side-effect states

# D10's closed side-effect state vocabulary, mapped onto OC0's existing
# append-only projection-attempt statuses (outward_communication.py).
# These are OBSERVED states reported by dispatch/recovery; they are never
# persisted as a new status string -- the projection ledger's own
# attempted/succeeded/failed/not_established values remain canonical.
DISPATCH_AUTHORIZED_READY = "AUTHORIZED_READY"          # act committed, no attempt row yet
DISPATCH_STARTED = "DISPATCH_STARTED"                    # an 'attempted' row exists, no outcome yet
DISPATCH_CONFIRMED_SENT = "CONFIRMED_SENT"               # a 'succeeded' row exists
DISPATCH_OUTCOME_NOT_ESTABLISHED = "OUTCOME_NOT_ESTABLISHED"  # a 'not_established' row exists
DISPATCH_FAILED_BEFORE_DISPATCH = "FAILED_BEFORE_DISPATCH"    # a 'failed' row exists

DISPATCH_NOT_AUTHORIZED = "NOT_AUTHORIZED"               # nothing was ever committed

# Multipart transport of ONE canonical Caret reply (derived from the per-part ledger; never persisted
# as a status string).  A definite failure of a later part after earlier parts were confirmed is
# "partially sent"; a plan with no confirmed part yet, or an interrupted run awaiting resume, is pending.
DISPATCH_MULTIPART_PENDING = "MULTIPART_PENDING"
DISPATCH_MULTIPART_PARTIALLY_SENT = "MULTIPART_PARTIALLY_SENT"

# Terminal projection statuses -- presence of any of these means an
# attempt has resolved and must never be auto-redispatched.
TERMINAL_PROJECTION_STATUSES = ("succeeded", "failed", "not_established")

"""What reaches the subject, and whether advertised capabilities work as shown."""

from claim_harness.checks import check_affordance, check_delivered
from toy_agent import item

_FILL = {"<text>": "remember the keys", "<destination>": "ops-desk"}


def invocations_from_help(outcome, sender="owner"):
    """Turn the !help text into the commands a reader would type, filling the placeholders."""
    usages = outcome["text"].split("You can use: ", 1)[1].split(" ; ", 1)[0].split(" | ")
    commands = []
    for n, usage in enumerate(usages):
        for placeholder, value in _FILL.items():
            usage = usage.replace(placeholder, value)
        commands.append(item(f"use{n}-{sender}", usage, sender=sender))
    return commands


def test_recall_reply_reaches_subject(agent):
    agent.submit(item("i1", "!note meet at noon"))
    check_delivered(agent, agent.channel, item("i2", "!recall"), subject="owner")
    assert agent.channel.delivered("owner")[-1] == "notes: meet at noon"


def test_send_confirmation_reaches_subject(agent):
    check_delivered(agent, agent.channel, item("i1", "!send ops-desk hi"), subject="owner")


def test_owner_can_use_everything_help_shows(agent):
    check_affordance(agent, item("h1", "!help"), invocations_from_help)


def test_guest_can_use_everything_help_shows(agent):
    shown = agent.submit(item("h1", "!help", sender="guest"))
    assert "!send" not in shown["text"]
    check_affordance(agent, item("h2", "!help", sender="guest"),
                     lambda outcome: invocations_from_help(outcome, sender="guest"))


def test_nothing_is_advertised_to_a_stranger(agent):
    assert agent.submit(item("h1", "!help", sender="stranger")) == {"kind": "null",
                                                                    "reason": "no_commands_available"}

"""Presentation-only canonical message timestamps. Never an inference input."""
from datetime import datetime, timezone
from html import escape


# Scoped to the conversation, using the installed Chatbot's semantic classes.
# Separate text blocks protect Markdown; their footer rows are visually plain.
TIMESTAMP_CSS = """
.anaxi-conversation .message-row:has(.anaxi-message-time) {
    margin-top: -8px !important;
    margin-bottom: 8px !important;
}
.anaxi-conversation .message:has(.anaxi-message-time) {
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 10px !important;
}
.anaxi-conversation .message-row:has(.anaxi-message-time) + .message-buttons {
    display: none !important;
}
.anaxi-message-time { opacity: .72; font-size: .78rem; }
"""


def timestamp_footer(occurred_at, *, previous_day=None, tz=None, host=False):
    """Format an existing occurrence time; never invent a time from the clock.

    The Desktop backend's local timezone is the default. First message and each
    local-day boundary include a date. Tooltip retains seconds and UTC offset.
    """
    if occurred_at is None:
        return None, previous_day
    local = datetime.fromtimestamp(occurred_at, timezone.utc).astimezone(tz)
    label = local.strftime('%I:%M %p').lstrip('0')
    if local.date() != previous_day:
        label = local.strftime('%b %d, %Y').replace(' 0', ' ') + ' · ' + label
    if host:
        label = 'Host · ' + label
    footer = ('<small class="anaxi-message-time" title="' + escape(local.isoformat(), quote=True)
              + '">' + escape(label) + '</small>')
    return footer, local.date()


NO_REPLY_DISPLAY_TEXT = "*(Host: Clark chose not to reply to this message.)*"


def display_messages(events, *, tz=None):
    """Separate text blocks keep footer markup outside even unclosed code fences.

    Input events/text remain unchanged. Only the UI consumes this projection;
    model context and canonical writers continue to consume their original text.
    """
    output = []
    previous_day = None
    for event in events:
        footer, previous_day = timestamp_footer(event.get('occurred_at'), previous_day=previous_day, tz=tz,
                                                host=event.get('reply_choice') == 'no_reply')
        # LAWFUL NULL: a host-rendered fact about Clark's typed choice, never words attributed to him.
        text = NO_REPLY_DISPLAY_TEXT if event.get('reply_choice') == 'no_reply' else event['content']
        blocks = [{'type':'text', 'text':text}]
        if footer is not None:
            blocks.append({'type':'text', 'text':footer})
        output.append({'role':event['role'], 'content':blocks})
    return output


def display_host_status(text, timestamp, *, tz=None):
    footer, _ = timestamp_footer(timestamp, tz=tz, host=True)
    return text + ('\n\n' + footer if footer is not None else '')

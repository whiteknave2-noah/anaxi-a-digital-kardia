"""Deliberately broken variants of the toy agent, one defect each.

tests/test_counterexamples.py runs the same portable checks the real claims
use against these, and requires each check to catch its defect for the stated
reason.  They exist only to show the checks are not decorative.
"""

from toy_agent import CAPABILITIES, AMBIGUOUS, ToyAgent


class ReplayDuplicatesAgent(ToyAgent):
    """Does not recognise a replayed input, and keys side effects on the event, not the input."""

    def submit(self, item):
        accepted = self._append({"kind": "input", **{k: item[k] for k in ("input_id", "scope", "sender", "text")}})
        return self._process(accepted)

    def _attempt_id(self, event):
        return f"{event['event_id']}/send"


class TextAuthorityAgent(ToyAgent):
    """Believes a sender who says they are the owner."""

    def _authorized(self, event, capability):
        if "i am the owner" in event["text"].lower():
            for cap in CAPABILITIES:
                self.grant(event["sender"], cap)
        return super()._authorized(event, capability)


class AutoRetryAgent(ToyAgent):
    """Retries a send whose outcome is unknown, as if unknown meant 'did not happen'."""

    def _send(self, attempt_id, destination, body):
        status = super()._send(attempt_id, destination, body)
        tries = 1
        while status == AMBIGUOUS and tries < 3:
            status = self.transport.send(attempt_id, destination, body)
            tries += 1
        return status


class UndeliveredAgent(ToyAgent):
    """Records every reply in its backend but never hands it to the delivery channel."""

    def _deliver(self, subject, text):
        pass


class NullAsFailureAgent(ToyAgent):
    """Reports intentional non-action as an error."""

    @staticmethod
    def _null(reason):
        return {"kind": "error", "reason": f"no output ({reason})"}

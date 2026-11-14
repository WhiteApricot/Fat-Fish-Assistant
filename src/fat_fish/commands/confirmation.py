"""Time-limited confirmation for commands with side effects."""
import time


class PendingActions:
    """Confirmation binds actor, conversation, operation and exact arguments."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.pending = clock, {}

    def issue(self, actor, scope, operation, arguments):
        self.pending[actor] = (scope, operation, tuple(arguments), self.clock() + 90)

    def peek(self, actor, scope):
        item = self.pending.get(actor)
        if item and self.clock() >= item[3]:
            self.pending.pop(actor)
            return None
        return item if item and item[0] == scope else None

    def consume_latest(self, actor, scope):
        item = self.peek(actor, scope)
        if item is None:
            return None
        self.pending.pop(actor)
        return item[1], item[2]

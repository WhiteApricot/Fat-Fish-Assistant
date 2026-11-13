"""Time-limited confirmation for commands with side effects."""
import hmac
import secrets
import time


class PendingActions:
    """Confirmation binds actor, conversation, operation and exact arguments."""
    def __init__(self, clock=time.monotonic):
        self.clock, self.pending = clock, {}

    def issue(self, actor, scope, operation, arguments):
        code = secrets.token_hex(4)
        self.pending[(str(actor), scope)] = (code, operation, tuple(arguments), self.clock()+90)
        return code

    def peek(self, actor, scope):
        key=(str(actor),scope)
        item=self.pending.get(key)
        if item and self.clock() >= item[3]:
            self.pending.pop(key)
            return None
        return item

    def consume_latest(self, actor, scope):
        item=self.pending.get((str(actor),scope))
        return self.consume(actor,scope,item[0]) if item else None

    def consume(self, actor, scope, code):
        key = (str(actor), scope)
        item = self.pending.get(key)
        if not item:
            return None
        if self.clock() >= item[3]:
            self.pending.pop(key, None)
            return None
        if not hmac.compare_digest(item[0], str(code)):
            return None
        self.pending.pop(key)
        return item[1], item[2]

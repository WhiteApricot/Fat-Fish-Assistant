"""Errors reported by the management interface."""


class Fault(Exception):
    def __init__(self, kind, message, hint=''):
        super().__init__(message)
        self.kind = kind
        self.hint = hint

"""Compatibility callback used by the pinned continuous-message patch."""


def supersede_reply(event, task):
    event.stop_event()
    task.cancel()

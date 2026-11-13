"""Authorization for canonical communication events and deliveries."""
from dataclasses import dataclass

from fat_fish.core.identity import Role


@dataclass(frozen=True)
class Admission:
    principal_id: str
    role: int
    mode: str
    room_moderator: bool = False


class PermissionService:
    def __init__(self, directory):
        self.directory = directory

    def conversation(self, account, room, *, mentions=None):
        _, role, mode, principal_id, room_moderator = self.directory.conversation_permissions(account, room)
        if not principal_id or not mode:
            return None
        if mentions is not None and mode == "mention" and room.bot not in mentions:
            return None
        return Admission(principal_id, role, mode, room_moderator)

    @staticmethod
    def is_global_admin(admission):
        return admission.role >= Role.ADMIN

    @staticmethod
    def can_moderate(admission):
        return admission.room_moderator

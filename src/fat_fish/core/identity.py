"""Persistent identity bindings and scope grants for authenticated adapters."""
from contextlib import closing, contextmanager
from enum import IntEnum
import hashlib
import secrets
import sqlite3
import time
import json
from threading import Lock
from uuid import uuid4

from fat_fish.communication.model import Account, ChatKind, ProviderId


class Role(IntEnum):
    BLOCKED = -1
    VISITOR = 0
    MEMBER = 1
    ADMIN = 2
    OWNER = 3


class IdentityDirectory:
    def __init__(self, path, *, clock=time.time):
        self.path = path
        self.clock = clock
        self._schema_lock = Lock()
        self._ready = False

    @contextmanager
    def transaction(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            if not self._ready:
                with self._schema_lock:
                    if not self._ready:
                        self._initialize(db)
                        db.commit()
                        self._ready = True
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db

    def _initialize(self, db):
        db.executescript(f"""
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY CHECK(id=principal_id),
                principal_id TEXT NOT NULL UNIQUE,
                role INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS sole_owner ON people(role) WHERE role={int(Role.OWNER)};
            CREATE TABLE IF NOT EXISTS accounts (address TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS identity_bindings (
                binding_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL REFERENCES people(principal_id),
                address TEXT NOT NULL REFERENCES accounts(address),
                verification_status TEXT NOT NULL CHECK(verification_status='verified'),
                verification_method TEXT NOT NULL,
                verified_at REAL NOT NULL,
                created_at REAL NOT NULL,
                revoked_at REAL,
                issued_by TEXT,
                revoked_by TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_identity_binding
                ON identity_bindings(address) WHERE revoked_at IS NULL;
            CREATE TABLE IF NOT EXISTS retired_accounts (address TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS owner_challenges (
                digest TEXT PRIMARY KEY, address TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identity_audit (
                audit_id TEXT PRIMARY KEY, action TEXT NOT NULL,
                actor TEXT, target TEXT NOT NULL, principal_id TEXT, occurred_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                digest TEXT PRIMARY KEY, address TEXT NOT NULL,
                person TEXT NOT NULL REFERENCES people(id), issuer TEXT NOT NULL,
                expires REAL NOT NULL, issuer_address TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                address TEXT PRIMARY KEY, mode TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_members (
                room TEXT NOT NULL REFERENCES rooms(address) ON DELETE CASCADE,
                address TEXT NOT NULL REFERENCES accounts(address) ON DELETE CASCADE,
                moderator INTEGER NOT NULL DEFAULT 0 CHECK(moderator IN (0,1)),
                PRIMARY KEY(room, address)
            );
            CREATE TABLE IF NOT EXISTS account_room_blocks (
                room TEXT NOT NULL REFERENCES rooms(address) ON DELETE CASCADE,
                address TEXT NOT NULL,
                PRIMARY KEY(room, address)
            );
        """)

    def _room_admin(self, db, actor, room, global_role):
        if actor.provider != room.provider:
            return False
        row = db.execute("""SELECT moderator FROM account_members WHERE room=? AND address=?
            AND NOT EXISTS (SELECT 1 FROM account_room_blocks b
                            WHERE b.room=account_members.room AND b.address=account_members.address)""",
                         (room.storage_key, actor.storage_key)).fetchone()
        return bool(row and global_role >= Role.MEMBER and (global_role >= Role.ADMIN or row[0]))

    def change_room_moderator(self, actor, room, target, *, grant, from_global=False, expected=None):
        if room.kind is not ChatKind.GROUP or target.provider != room.provider:
            raise ValueError("Invalid group account")
        with self.transaction(write=True) as db:
            _, actor_role, principal = self._lookup_identity(db, actor)
            _, target_role = self._lookup(db, target)
            permitted = (actor.provider == room.provider and db.execute(
                "SELECT 1 FROM rooms WHERE address=?", (room.storage_key,)).fetchone()) if from_global else (
                self._room_admin(db, actor, room, actor_role))
            if actor_role < Role.ADMIN or not permitted or target_role >= Role.OWNER:
                raise PermissionError("Room moderator assignment denied")
            if target_role < Role.MEMBER:
                raise PermissionError("Target has no verified member role")
            if expected is not None:
                latest = db.execute("SELECT max(rowid) FROM identity_audit").fetchone()[0]
                if latest != expected:
                    raise ValueError("Authorization changed after the grant")
            prior = db.execute("SELECT moderator FROM account_members WHERE room=? AND address=?",
                               (room.storage_key, target.storage_key)).fetchone()
            if from_global and grant:
                db.execute("""INSERT INTO account_members VALUES (?,?,1)
                    ON CONFLICT(room,address) DO UPDATE SET moderator=1""", (room.storage_key, target.storage_key))
            elif not db.execute("UPDATE account_members SET moderator=? WHERE room=? AND address=?",
                                (grant, room.storage_key, target.storage_key)).rowcount:
                raise PermissionError("Target is outside this room")
            self._audit(db, "room-moderator-grant" if grant else "room-moderator-revoke",
                        target.storage_key, actor=actor.storage_key, principal=principal)
            return db.execute("SELECT last_insert_rowid()").fetchone()[0] if not prior or bool(prior[0]) != grant else None

    def room_moderators(self, actor, room):
        with self.transaction() as db:
            _, role = self._lookup(db, actor)
            if actor.provider != room.provider or (
                    role < Role.ADMIN and not self._room_admin(db, actor, room, role)):
                raise PermissionError("Room administrator required")
            rows = db.execute("""SELECT m.address FROM account_members m JOIN identity_bindings b USING(address)
                JOIN people p USING(principal_id) WHERE m.room=? AND b.revoked_at IS NULL
                AND (p.role>=? OR (p.role=? AND m.moderator=1))
                AND NOT EXISTS (SELECT 1 FROM account_room_blocks x WHERE x.room=m.room AND x.address=m.address)
                ORDER BY m.address""",
                              (room.storage_key, Role.ADMIN, Role.MEMBER))
            return [json.loads(row[0])[2] for row in rows]

    def remove_room_moderator_grants(self, actor, provider, bot, target):
        if actor.provider != provider or target.provider != provider:
            raise ValueError("Room grants belong to a different provider")
        with self.transaction(write=True) as db:
            _, role, principal = self._lookup_identity(db, actor)
            if role < Role.ADMIN or self._lookup(db, target)[1] == Role.OWNER:
                raise PermissionError("Group moderator assignment denied")
            rooms = [address for address, in db.execute("""SELECT room FROM account_members
                WHERE address=? AND moderator=1""", (target.storage_key,))
                     if json.loads(address)[:3] == [provider.platform, provider.instance, bot]]
            db.executemany("UPDATE account_members SET moderator=0 WHERE room=? AND address=?",
                           ((room, target.storage_key) for room in rooms))
            if rooms:
                self._audit(db, "room-moderator-revoke-all", target.storage_key,
                            actor=actor.storage_key, principal=principal)
            return rooms

    def is_room_blocked(self, room, account):
        with self.transaction() as db:
            return bool(db.execute("SELECT 1 FROM account_room_blocks WHERE room=? AND address=?",
                                   (room.storage_key, account.storage_key)).fetchone())

    def change_room_block(self, actor, room, target, *, blocked):
        if room.kind is not ChatKind.GROUP or target.provider != room.provider or target == actor:
            raise ValueError("Invalid room block target")
        with self.transaction(write=True) as db:
            _, role, principal = self._lookup_identity(db, actor)
            _, target_role = self._lookup(db, target)
            if (not self._room_admin(db, actor, room, role) or target_role >= Role.ADMIN
                    or (role < Role.ADMIN and db.execute(
                        "SELECT 1 FROM account_members WHERE room=? AND address=? AND moderator=1",
                        (room.storage_key, target.storage_key)).fetchone())):
                raise PermissionError("Room block authorization denied")
            params = room.storage_key, target.storage_key
            if blocked:
                db.execute("INSERT OR IGNORE INTO account_room_blocks VALUES (?,?)", params)
            else:
                db.execute("DELETE FROM account_room_blocks WHERE room=? AND address=?", params)
            self._audit(db, "room-block" if blocked else "room-unblock", target.storage_key,
                        actor=actor.storage_key, principal=principal)

    def room_blocked_accounts(self, actor, room):
        with self.transaction() as db:
            _, role = self._lookup(db, actor)
            if not self._room_admin(db, actor, room, role):
                raise PermissionError("Room moderator required")
            return [json.loads(row[0])[2] for row in db.execute(
                "SELECT address FROM account_room_blocks WHERE room=? ORDER BY address", (room.storage_key,))]

    def owner_account(self):
        with self.transaction() as db:
            row = db.execute("""SELECT address FROM identity_bindings JOIN people USING(principal_id)
                WHERE role=? AND revoked_at IS NULL ORDER BY identity_bindings.created_at, binding_id LIMIT 1""",
                             (Role.OWNER,)).fetchone()
        if row is None:
            return None
        platform, instance, subject = json.loads(row[0])
        return Account(ProviderId(platform, instance), subject)

    def issue_owner_challenge(self, target):
        """Issue a one-time challenge for an authenticated private account."""
        token = secrets.token_urlsafe(32)
        with self.transaction(write=True) as db:
            if db.execute("SELECT 1 FROM people WHERE role=?", (Role.OWNER,)).fetchone():
                raise PermissionError("Owner setup is unavailable")
            now = self.clock()
            db.execute("DELETE FROM owner_challenges WHERE expires<=? OR address=?",
                       (now, target.storage_key))
            db.execute("INSERT INTO owner_challenges VALUES (?,?,?)",
                       (self._digest(token), target.storage_key, now + 300))
            self._audit(db, "owner-setup-issued", target.storage_key, actor=target.storage_key)
        return token

    def confirm_owner_challenge(self, target, token):
        """Activate the sole Owner under the same transaction as account binding."""
        with self.transaction(write=True) as db:
            row = db.execute("SELECT address,expires FROM owner_challenges WHERE digest=?",
                             (self._digest(token),)).fetchone()
            if not row or row[0] != target.storage_key or row[1] <= self.clock():
                raise PermissionError("Invalid or expired owner challenge")
            if db.execute("SELECT 1 FROM people WHERE role=?", (Role.OWNER,)).fetchone():
                raise PermissionError("Owner setup is unavailable")
            if self._lookup(db, target)[0] is not None:
                raise PermissionError("Target already has an identity")
            principal = self._create_person(db, Role.OWNER)
            self._bind(db, row[0], principal, "provider-owner-setup")
            db.execute("DELETE FROM owner_challenges")
            self._audit(db, "owner-setup", row[0], actor=row[0], principal=principal)
            return principal

    def _create_person(self, db, role):
        principal = str(uuid4())
        db.execute("INSERT INTO people (id,role,principal_id,created_at) VALUES (?,?,?,?)",
                   (principal, role, principal, self.clock()))
        return principal

    def _audit(self, db, action, target, *, actor=None, principal=None):
        db.execute("INSERT INTO identity_audit VALUES (?,?,?,?,?,?)",
                   (str(uuid4()), action, actor, target, principal, self.clock()))

    def observe(self, account):
        """Register an authenticated first contact with no chat permissions."""
        with self.transaction(write=True) as db:
            person, _, principal = self._lookup_identity(db, account)
            if person or db.execute("SELECT 1 FROM retired_accounts WHERE address=?",
                                    (account.storage_key,)).fetchone():
                return principal
            if not db.execute("SELECT 1 FROM people WHERE role=?", (Role.OWNER,)).fetchone():
                return None
            principal = self._create_person(db, Role.VISITOR)
            self._bind(db, account.storage_key, principal, "provider-first-contact")
            return principal

    def _bind(self, db, address, person, method, *, issuer=None):
        now = self.clock()
        db.execute("INSERT OR IGNORE INTO accounts(address) VALUES (?)", (address,))
        db.execute("""INSERT INTO identity_bindings
            (binding_id,principal_id,address,verification_status,verification_method,
             verified_at,created_at,issued_by) VALUES (?,?,?,'verified',?,?,?,?)""",
                   (str(uuid4()), person, address, method, now, now, issuer))

    @staticmethod
    def _lookup_identity(db, account):
        row = db.execute(
            """SELECT id,role,principal_id FROM identity_bindings JOIN people USING(principal_id)
               WHERE address=? AND revoked_at IS NULL""",
            (account.storage_key,),
        ).fetchone()
        return (row[0], Role(row[1]), row[2]) if row else (None, Role.VISITOR, None)

    def _lookup(self, db, account):
        return self._lookup_identity(db, account)[:2]

    def resolve(self, account):
        with self.transaction() as db:
            return self._lookup(db, account)

    def principal_id(self, account):
        with self.transaction() as db:
            return self._lookup_identity(db, account)[2]

    def resolve_principal(self, account):
        with self.transaction() as db:
            _, role, principal = self._lookup_identity(db, account)
            if principal is not None:
                return principal, role
            retired = db.execute("SELECT 1 FROM retired_accounts WHERE address=?", (account.storage_key,)).fetchone()
            return (None, Role.BLOCKED if retired else Role.VISITOR)

    def accounts_for(self, principal, provider):
        with self.transaction() as db:
            addresses = db.execute("""SELECT address FROM identity_bindings JOIN people USING(principal_id)
                WHERE principal_id=? AND revoked_at IS NULL""", (principal,)).fetchall()
        accounts = []
        for (address,) in addresses:
            platform, instance, subject = json.loads(address)
            if (platform, instance) == (provider.platform, provider.instance):
                accounts.append(Account(provider, subject))
        return accounts

    def accounts_with_role(self, provider, role):
        with self.transaction() as db:
            addresses = db.execute("""SELECT address FROM identity_bindings JOIN people USING(principal_id)
                WHERE role=? AND revoked_at IS NULL ORDER BY address""", (role,)).fetchall()
        accounts = []
        for (address,) in addresses:
            platform, instance, subject = json.loads(address)
            if (platform, instance) == (provider.platform, provider.instance):
                accounts.append(Account(provider, subject))
        return accounts

    def provision(self, actor, account, role, *, expected=None, capture_revision=False):
        """Local operator entry point. Actor comes from a trusted auth boundary."""
        role = Role(role)
        if role not in {Role.MEMBER, Role.ADMIN, Role.BLOCKED} and not (role == Role.VISITOR and expected is not None):
            raise ValueError("Invalid provisioned role")
        with self.transaction(write=True) as db:
            authority = self._lookup(db, actor)[1]
            principal, current = self._lookup(db, account)
            if authority != Role.OWNER and (authority != Role.ADMIN
                    or current >= Role.ADMIN or role == Role.ADMIN):
                raise PermissionError("Insufficient authority to grant this role")
            if current == Role.OWNER:
                raise PermissionError("Owner binding is protected")
            if expected is not None and authority != Role.OWNER:
                raise PermissionError("Owner authorization required to revoke a global grant")
            if capture_revision and current == Role.BLOCKED:
                raise PermissionError("Blocked accounts cannot become global administrators")
            if principal is None:
                raise PermissionError("Account has not verified its identity")
            if expected is not None and db.execute("SELECT max(rowid) FROM identity_audit").fetchone()[0] != expected:
                raise ValueError("Authorization changed after the grant")
            db.execute("UPDATE people SET role=? WHERE id=?", (role, principal))
            db.execute("DELETE FROM retired_accounts WHERE address=?", (account.storage_key,))
            self._audit(db, "grant-" + role.name.lower(), account.storage_key, actor=actor.storage_key,
                        principal=self._lookup_identity(db, account)[2])
            if capture_revision:
                return current, db.execute("SELECT last_insert_rowid()").fetchone()[0]
            return principal

    def authorization_revision(self):
        with self.transaction() as db:
            return db.execute("SELECT max(rowid) FROM identity_audit").fetchone()[0]

    def invite(self, actor, existing, target):
        """Authorize an exact target. Give the secret only to the intended recipient."""
        with self.transaction(write=True) as db:
            issuer, authority = self._lookup(db, actor)
            principal, role = self._lookup(db, existing)
            if issuer is None or principal is None or authority < Role.VISITOR or role < Role.VISITOR or (
                    issuer != principal and authority != Role.OWNER):
                raise PermissionError("Binding authorization required")
            if self._lookup(db, target)[0] is not None:
                raise ValueError("Target already has an identity")
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM invitations WHERE expires <= ? OR (address=? AND issuer_address=?)",
                       (self.clock(), target.storage_key, actor.storage_key))
            db.execute("INSERT INTO invitations VALUES (?,?,?,?,?,?)",
                       (self._digest(token), target.storage_key, principal, issuer, self.clock() + 300,
                        actor.storage_key))
            self._audit(db, "binding-issued", target.storage_key, actor=actor.storage_key,
                        principal=self._lookup_identity(db, existing)[2])
            return token

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def confirm(self, authenticated_target, token):
        with self.transaction(write=True) as db:
            row = db.execute("SELECT address, person, issuer, expires, issuer_address FROM invitations WHERE digest=?",
                             (self._digest(token),)).fetchone()
            if row is None or row[0] != authenticated_target.storage_key or row[3] <= self.clock():
                raise PermissionError("Invalid or expired invitation")
            roles = dict(db.execute("SELECT id,role FROM people WHERE id IN (?,?)", (row[1], row[2])))
            if roles.get(row[1], Role.BLOCKED) < Role.VISITOR or roles.get(row[2], Role.BLOCKED) < Role.VISITOR:
                raise PermissionError("Binding authorization revoked")
            if not db.execute("""SELECT 1 FROM identity_bindings b JOIN people p USING(principal_id)
                WHERE b.address=? AND p.id=? AND b.revoked_at IS NULL""", (row[4], row[2])).fetchone():
                raise PermissionError("Binding issuer account revoked")
            if row[1] != row[2] and roles[row[2]] != Role.OWNER:
                raise PermissionError("Binding authorization revoked")
            if self._lookup(db, authenticated_target)[0] is not None:
                raise ValueError("Target already has an identity")
            self._bind(db, row[0], row[1], "adapter-invitation", issuer=row[4])
            db.execute("DELETE FROM retired_accounts WHERE address=?", (row[0],))
            db.execute("DELETE FROM invitations WHERE address=?", (row[0],))
            self._audit(db, "binding-confirmed", row[0], actor=row[0],
                        principal=self._lookup_identity(db, authenticated_target)[2])
            return row[1]

    def unlink(self, actor, account):
        with self.transaction(write=True) as db:
            principal, _ = self._lookup(db, account)
            issuer, authority = self._lookup(db, actor)
            if principal is None or issuer is None or authority < Role.VISITOR or (
                    issuer != principal and authority != Role.OWNER):
                raise PermissionError("Unlink authorization required")
            if self._lookup(db, account)[1] == Role.OWNER:
                count = db.execute("""SELECT count(*) FROM identity_bindings JOIN people USING(principal_id)
                    WHERE role=? AND revoked_at IS NULL""", (Role.OWNER,)).fetchone()[0]
                if count <= 1:
                    raise PermissionError("Last owner account cannot be unlinked")
            self._revoke(db, account.storage_key, actor.storage_key)

    def _revoke(self, db, address, actor):
        principal = db.execute("SELECT principal_id FROM identity_bindings WHERE address=? AND revoked_at IS NULL",
                               (address,)).fetchone()
        db.execute("UPDATE identity_bindings SET revoked_at=?,revoked_by=? WHERE address=? AND revoked_at IS NULL",
                   (self.clock(), actor, address))
        db.execute("DELETE FROM account_members WHERE address=?", (address,))
        db.execute("INSERT OR IGNORE INTO retired_accounts VALUES (?)", (address,))
        db.execute("DELETE FROM invitations WHERE address=? OR issuer_address=?", (address, address))
        if principal:
            self._audit(db, "binding-revoked", address, actor=actor, principal=principal[0])

    def set_room(self, actor, room, mode, members=()):
        if room.kind is not ChatKind.GROUP or mode not in {None, "mention", "smart", "open"}:
            raise ValueError("Invalid group policy")
        with self.transaction(write=True) as db:
            if self._lookup(db, actor)[1] != Role.OWNER:
                raise PermissionError("Owner authorization required")
            db.execute("DELETE FROM rooms WHERE address=?", (room.storage_key,))
            if mode is None:
                return
            db.execute("INSERT INTO rooms VALUES (?,?)", (room.storage_key, mode))
            for account in members:
                principal, role = self._lookup(db, account)
                if account.provider != room.provider or role < Role.MEMBER:
                    raise ValueError("Group member must be an authorized account on this provider")
                db.execute("INSERT OR IGNORE INTO account_members(room,address) VALUES (?,?)", (room.storage_key, account.storage_key))

    def change_room_mode(self, actor, room, mode):
        if room.kind is not ChatKind.GROUP or mode not in {"mention", "smart"}:
            raise ValueError("Invalid room mode")
        with self.transaction(write=True) as db:
            principal, role = self._lookup(db, actor)
            if not principal or not self._room_admin(db, actor, room, role):
                raise PermissionError("Room administrator required")
            db.execute("UPDATE rooms SET mode=? WHERE address=?", (mode, room.storage_key))
            self._audit(db, "room-mode-" + mode, room.storage_key, actor=actor.storage_key,
                        principal=self._lookup_identity(db, actor)[2])

    def change_room_member(self, actor, room, target, *, grant):
        if room.kind != ChatKind.GROUP or target.provider != room.provider:
            raise ValueError("Invalid group member")
        with self.transaction(write=True) as db:
            _, actor_role, principal = self._lookup_identity(db, actor)
            _, target_role = self._lookup(db, target)
            room_grant = db.execute("SELECT 1 FROM rooms WHERE address=?", (room.storage_key,)).fetchone()
            if not room_grant or actor.provider != room.provider or (actor_role != Role.OWNER and (
                    not self._room_admin(db, actor, room, actor_role) or target_role >= Role.ADMIN
                    or self._room_admin(db, target, room, target_role))):
                raise PermissionError("Room membership modification denied")
            if grant:
                if target_role < Role.MEMBER:
                    raise PermissionError("Target has no verified member role")
                db.execute("INSERT OR IGNORE INTO account_members(room,address) VALUES (?,?)",
                           (room.storage_key, target.storage_key))
            else:
                db.execute("DELETE FROM account_members WHERE room=? AND address=?",
                           (room.storage_key, target.storage_key))
            self._audit(db, "room-grant" if grant else "room-revoke", target.storage_key,
                        actor=actor.storage_key, principal=principal)

    def change_room_admission(self, actor, room, *, grant):
        if room.kind != ChatKind.GROUP or actor.provider != room.provider:
            raise ValueError("Invalid room")
        with self.transaction(write=True) as db:
            _, role, principal = self._lookup_identity(db, actor)
            if role < Role.ADMIN:
                raise PermissionError("Global administrator authorization required")
            if grant:
                db.execute("INSERT OR IGNORE INTO rooms VALUES (?,'mention')", (room.storage_key,))
                db.execute("INSERT OR IGNORE INTO account_members(room,address) VALUES (?,?)",
                           (room.storage_key, actor.storage_key))
            else:
                db.execute("DELETE FROM rooms WHERE address=?", (room.storage_key,))
            self._audit(db, "room-open" if grant else "room-close", room.storage_key,
                        actor=actor.storage_key, principal=principal)

    def rooms_for_admin(self, actor):
        with self.transaction() as db:
            if self._lookup(db, actor)[1] < Role.ADMIN:
                raise PermissionError("Global administrator authorization required")
            rooms = [json.loads(address) for (address,) in db.execute("SELECT address FROM rooms ORDER BY address")]
        return [room[4] for room in rooms if room[:2] == [actor.provider.platform, actor.provider.instance]]

    def access(self, account, room):
        return self.access_identity(account, room)[:3]

    def room_membership_signature(self, room):
        with self.transaction() as db:
            return tuple(db.execute("""SELECT m.address,b.binding_id,p.role,m.moderator
                FROM account_members m JOIN identity_bindings b USING(address)
                JOIN people p USING(principal_id)
                WHERE m.room=? AND b.revoked_at IS NULL AND p.role>=?
                AND NOT EXISTS (SELECT 1 FROM account_room_blocks x WHERE x.room=m.room AND x.address=m.address)
                ORDER BY m.address""",
                                    (room.storage_key, Role.MEMBER)))

    def access_identity(self, account, room):
        return self.conversation_permissions(account, room)[:4]

    def conversation_permissions(self, account, room):
        with self.transaction() as db:
            principal, role, principal_id = self._lookup_identity(db, account)
            if account.provider != room.provider or role < Role.MEMBER:
                return principal, role, None, principal_id, False
            if room.kind is ChatKind.PRIVATE:
                return principal, role, "open" if account.subject == room.peer else None, principal_id, False
            row = db.execute("""SELECT mode FROM rooms JOIN account_members ON account_members.room=rooms.address
                              WHERE rooms.address=? AND account_members.address=?
                              AND NOT EXISTS (SELECT 1 FROM account_room_blocks b
                                  WHERE b.room=rooms.address AND b.address=account_members.address)""",
                             (room.storage_key, account.storage_key)).fetchone()
            return (principal, role, row[0] if row else None, principal_id,
                    self._room_admin(db, account, room, role))

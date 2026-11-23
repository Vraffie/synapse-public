"""Ephemeral, bounded agent session credentials and exact resource grants.

Only trusted daemon code may issue or revoke sessions. Credentials never carry
agent-supplied identity; transports must authenticate before calling services.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hmac
import secrets
from threading import RLock
from typing import Iterable

from synapse.application.messaging import AgentIdentity, MessageAccessDenied
from synapse.domain.contracts import validate_id


@dataclass(frozen=True, slots=True)
class SessionCredential:
    identity: AgentIdentity
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Session:
    identity: AgentIdentity
    secret: bytes = field(repr=False)
    grants: frozenset[tuple[str, str]]


class SessionAuthority:
    """Exact action/resource policy registry; restart invalidates all sessions."""

    def __init__(self, *, max_sessions: int = 64):
        if type(max_sessions) is not int or not 1 <= max_sessions <= 4096:
            raise ValueError("max_sessions must be between 1 and 4096")
        self._maximum = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._lock = RLock()

    def issue(self, *, instance_id: str, run_id: str,
              grants: Iterable[tuple[str, str]]) -> SessionCredential:
        validate_id(instance_id)
        validate_id(run_id)
        permissions = set()
        for index, grant in enumerate(grants):
            if index >= 4096:
                raise ValueError("Too many grants")
            if not isinstance(grant, tuple) or len(grant) != 2:
                raise ValueError("Each grant must be an action/resource pair")
            action, resource = grant
            validate_id(action)
            validate_id(resource)
            if action == '*' or resource == '*':
                raise ValueError("Wildcard grants are not supported")
            permissions.add((action, resource))
        with self._lock:
            if len(self._sessions) >= self._maximum:
                raise RuntimeError("Session capacity exhausted")
            principal = secrets.token_urlsafe(24)
            while principal in self._sessions:
                principal = secrets.token_urlsafe(24)
            identity = AgentIdentity(principal=principal, instance_id=instance_id, run_id=run_id)
            token = secrets.token_urlsafe(32)
            self._sessions[principal] = _Session(identity, token.encode('ascii'), frozenset(permissions))
            return SessionCredential(identity, token)

    def authenticate(self, credential: str) -> AgentIdentity | None:
        """Derive the identity solely from a secret; compare every active secret."""
        if not isinstance(credential, str) or not credential.isascii() or len(credential) != 43:
            candidate = b'\x00' * 43
        else:
            candidate = credential.encode('ascii')
        found = None
        with self._lock:
            for session in self._sessions.values():
                if hmac.compare_digest(session.secret, candidate):
                    found = session.identity
        return found

    @contextmanager
    def authenticated_operation(self, credential: str):
        """Hold revocation lock until a trusted synchronous operation completes."""
        with self._lock:
            identity = self.authenticate(credential)
            if identity is None:
                raise MessageAccessDenied("Message access denied")
            yield identity

    @contextmanager
    def daemon_operation(self, principal: str):
        """Hold an existing session stable for trusted daemon policy work."""
        validate_id(principal)
        with self._lock:
            session = self._sessions.get(principal)
            if session is None:
                raise MessageAccessDenied("Message access denied")
            yield session.identity

    def _add_delivery_grants(self, identity: AgentIdentity, delivery_id: str) -> None:
        """Trusted daemon helper; caller must authorize ownership before calling."""
        validate_id(delivery_id)
        with self._lock:
            session = self._sessions.get(identity.principal)
            if session is None or session.identity != identity:
                raise MessageAccessDenied("Message access denied")
            grants = session.grants | {('delivery.read', delivery_id), ('delivery.complete', delivery_id)}
            if len(grants) > 4096:
                raise ValueError("Too many grants")
            self._sessions[identity.principal] = _Session(session.identity, session.secret, frozenset(grants))

    def revoke(self, principal: str) -> bool:
        validate_id(principal)
        with self._lock:
            return self._sessions.pop(principal, None) is not None

    def identity(self, principal: str) -> AgentIdentity | None:
        with self._lock:
            session = self._sessions.get(principal)
            return session.identity if session else None

    def allows(self, identity: AgentIdentity, action: str, resource: str) -> bool:
        with self._lock:
            session = self._sessions.get(identity.principal)
            return bool(session and session.identity == identity and (action, resource) in session.grants)

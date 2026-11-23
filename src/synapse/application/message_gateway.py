"""In-process authenticated entry point for scoped agent messaging.

No transport is supplied. Callers must not expose the service or authority to
untrusted agents; credentials must arrive over a protected channel.
"""
from __future__ import annotations

import json
import math

from synapse.application.messaging import MessageAccessDenied, MessageService
from synapse.application.session_authority import SessionAuthority
from synapse.domain.contracts import MessageType, validate_id
from synapse.persistence.message_scope import delivery_scope, instance_in_run


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate JSON object key')
        obj[key] = value
    return obj


def _reject_constant(_value):
    raise ValueError('Non-finite JSON number')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Non-finite JSON number')
    return number


class MessageGateway:
    def __init__(self, connection, authority: SessionAuthority, *, max_request_bytes: int = 131072):
        if not isinstance(authority, SessionAuthority):
            raise TypeError('A trusted session authority is required')
        if type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 1048576:
            raise ValueError('Invalid request size limit')
        self._authority = authority
        self._connection = connection
        self._service = MessageService(connection, authority)
        self._max_bytes = max_request_bytes

    def grant_owned_delivery(self, principal: str, delivery_id: str) -> None:
        """Trusted daemon provisioning only; never expose this as an agent operation."""
        validate_id(delivery_id)
        with self._authority.daemon_operation(principal) as identity:
            scope = delivery_scope(self._connection, delivery_id, identity.instance_id)
            if (not instance_in_run(self._connection, identity.instance_id, identity.run_id)
                    or scope is None or scope[0] != identity.run_id
                    or not self._authority.allows(identity, 'inbox.claim', identity.instance_id)
                    or not self._authority.allows(identity, 'thread.access', scope[1])):
                raise MessageAccessDenied('Message access denied')
            self._authority._add_delivery_grants(identity, delivery_id)

    def claim_exact_for_mock(self, credential: str, delivery_id: str, message_id: str):
        """Trusted in-process mock helper; not an agent-invokable operation."""
        with self._authority.authenticated_operation(credential) as identity:
            return self._service.claim_exact_for_mock(principal=identity.principal, delivery_id=delivery_id, message_id=message_id)

    def invoke(self, credential: str, operation: str, raw_request: bytes):
        """Authenticate and execute one synchronous service call under revocation lock."""
        with self._authority.authenticated_operation(credential) as identity:
            if not isinstance(raw_request, bytes):
                raise TypeError('Request must be bytes')
            if len(raw_request) > self._max_bytes:
                raise ValueError('Request exceeds size limit')
            try:
                request = json.loads(raw_request, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant, parse_float=_finite_float)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError('Invalid JSON request') from exc
            if not isinstance(request, dict):
                raise ValueError('Request must be a JSON object')
            principal = identity.principal
            if operation == 'send':
                required = {'idempotency_key', 'message_id', 'thread_id', 'type', 'payload', 'created_at', 'recipients'}
                optional = {'artifact_refs', 'correlation_id', 'causation_id'}
                self._validate_keys(request, required, optional)
                request['type'] = MessageType(request['type'])
                return self._service.send(principal=principal, **request)
            if operation == 'claim':
                self._validate_keys(request, {'idempotency_key'}, {'limit'})
                validate_id(request['idempotency_key'])
                return self._service.claim(principal=principal, **request)
            if operation == 'read':
                self._validate_keys(request, {'delivery_id'}, set())
                return self._service.read_delivery(principal=principal, **request)
            if operation == 'complete':
                self._validate_keys(request, {'idempotency_key', 'delivery_id', 'token', 'success'},
                                    {'result_artifact_id'})
                return self._service.complete(principal=principal, **request)
            raise ValueError('Unknown operation')

    @staticmethod
    def _validate_keys(request, required, optional):
        if not required <= request.keys() or request.keys() - required - optional:
            raise ValueError('Invalid request fields')

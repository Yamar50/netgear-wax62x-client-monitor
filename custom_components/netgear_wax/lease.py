"""Atomic, durable fleet-wide access reservation shared by HA and external clients."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import secrets
import time

from .const import MAX_LEASE_SECONDS


class BusyError(Exception):
    pass


class LeaseError(Exception):
    pass


class LeaseManager:
    """Persist before granting access. Clients must stop BEFORE their lease expires.

    A fleet-wide lease is deliberately conservative: no AP login from HA may
    overlap an external operation, even if different APs are involved.
    """

    def __init__(self, save, state=None, now=time.time):
        self._save = save
        self._state = state or {}
        self._now = now
        self._lock = asyncio.Lock()

    def status(self):
        state = self._state
        active = state.get("expires_at", 0) > self._now()
        return {
            "busy": active,
            "owner": state.get("owner") if active else None,
            "kind": state.get("kind") if active else None,
            "expires_at": state.get("expires_at") if active else None,
        }

    async def acquire(self, owner, ttl=600, kind="external"):
        if kind == "external" and not 120 <= ttl <= MAX_LEASE_SECONDS:
            raise LeaseError("invalid_duration")
        async with self._lock:
            if self.status()["busy"]:
                raise BusyError("collector_or_external_client_busy")
            token = secrets.token_urlsafe(32)
            state = {
                "owner": owner,
                "kind": kind,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": self._now() + ttl,
            }
            # A failed disk write must never grant an unrecorded reservation.
            await self._save(state)
            self._state = state
            return {"token": token, "expires_at": state["expires_at"], "duration": ttl}

    def _check(self, owner, token):
        if (
            not self.status()["busy"]
            or self._state.get("owner") != owner
            or not secrets.compare_digest(self._state.get("token_hash", ""), sha256(token.encode()).hexdigest())
        ):
            raise LeaseError("invalid_or_expired_reservation")

    async def renew(self, owner, token, ttl=600):
        if not 120 <= ttl <= MAX_LEASE_SECONDS:
            raise LeaseError("invalid_duration")
        async with self._lock:
            self._check(owner, token)
            state = {**self._state, "expires_at": self._now() + ttl}
            await self._save(state)
            self._state = state
            return {"expires_at": state["expires_at"], "duration": ttl}

    async def release(self, owner, token):
        async with self._lock:
            self._check(owner, token)
            await self._save({})
            self._state = {}

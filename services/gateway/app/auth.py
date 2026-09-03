"""API key authentication and route authorisation for the gateway.

Two roles, because a fleet has two very different callers:

  admin  -- an operator or the dashboard. Full access.
  device -- an enrolled endpoint agent. Allowed only to enroll itself, send
            heartbeats, post its own compliance reports, and read the policy
            set for its group.

Keeping device credentials this narrow matters: they are the credentials that
live on hundreds of thousands of machines outside the operator's control, so a
leaked one must not be able to author policy or enumerate the fleet.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

ROLE_ADMIN = "admin"
ROLE_DEVICE = "device"

# Routes a device-role key may reach, as (allowed methods, path pattern).
DEVICE_SCOPES: list[tuple[set[str], re.Pattern[str]]] = [
    ({"POST"}, re.compile(r"^/api/registry/devices$")),
    ({"POST"}, re.compile(r"^/api/registry/devices/[^/]+/heartbeat$")),
    ({"POST"}, re.compile(r"^/api/compliance/reports$")),
    ({"GET"}, re.compile(r"^/api/policy/policies/for-group/[^/]+$")),
]


@dataclass(frozen=True)
class Principal:
    key_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


class KeyStore:
    """Resolves a presented API key to a principal.

    Comparison is constant-time so the endpoint does not leak key material
    through response timing.
    """

    def __init__(self, admin_keys: list[str], device_keys: list[str]) -> None:
        self._keys: list[tuple[str, str, str]] = []
        for index, key in enumerate(admin_keys):
            self._keys.append((key, f"admin-{index + 1}", ROLE_ADMIN))
        for index, key in enumerate(device_keys):
            self._keys.append((key, f"device-{index + 1}", ROLE_DEVICE))

    def resolve(self, presented: str | None) -> Principal | None:
        if not presented:
            return None
        for secret, key_id, role in self._keys:
            if hmac.compare_digest(secret, presented):
                return Principal(key_id=key_id, role=role)
        return None

    def __len__(self) -> int:
        return len(self._keys)


def authorize(principal: Principal, method: str, path: str) -> bool:
    if principal.is_admin:
        return True
    return any(
        method.upper() in methods and pattern.match(path) for methods, pattern in DEVICE_SCOPES
    )

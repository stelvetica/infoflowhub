from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthDescriptor:
    auth_key: str
    platform: str
    auth_mode: str
    storage_ref: str
    renew_strategy: str
    display_name: str
    description: str
    status_text: str = ""
    status_level: str = "warn"
    hint: str = ""
    is_available: bool = False
    is_expired: bool = False
    is_expiring_soon: bool = False
    remaining_hours: int = -1


@dataclass(frozen=True, slots=True)
class AuthRegistration:
    auth_key: str
    platform: str
    auth_mode: str
    storage_ref: str
    renew_strategy: str
    display_name: str
    description: str

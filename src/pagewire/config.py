"""Relay configuration: every tunable the design names, read from the environment.

Nothing here is hardcoded, deliberately. The design lists these as things that must be
adjustable without touching code — the stitch window, the destination switches, both
service tokens, the device address, the throttle. Defaults exist so the relay starts, but
each one can be overridden by an environment variable, which is what the NixOS module will
set from a state file rather than from the flake (the config repo's AGENTS.md forbids new
plaintext secrets in `flake.nix`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

PREFIX = "PAGEWIRE_"


def _str(env: Mapping[str, str], name: str, default: str = "") -> str:
    return env.get(PREFIX + name, default)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(PREFIX + name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{PREFIX}{name} must be an integer, got {raw!r}") from exc


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(PREFIX + name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """One immutable settings object, built once at startup."""

    # Where the SQLite file lives. On the deployed box this is /var/app/data/pagewire/.
    db_path: str = "state/pagewire.db"

    # The shared secret the device sends as a bearer token. Empty means the relay refuses
    # every event rather than silently accepting unauthenticated writes onto the LAN.
    device_token: str = ""

    # Gaps shorter than this are pauses within a sitting; at or above it they end one.
    # Vivek's ruling, 2026-10-31: "short gaps are pauses, long pauses end sittings".
    stitch_window_seconds: int = 30 * 60

    # The two destinations, independently switchable, each with its own credential.
    readwise_enabled: bool = False
    readwise_token: str = ""
    hardcover_enabled: bool = False
    hardcover_token: str = ""

    # What a delivered session says about the device that produced it.
    device_type: str = "E-Reader"
    device_vendor: str = "Xteink"
    device_model: str = "X4"

    # Local zone for the sessions we send; Readwise takes an IANA name.
    timezone: str = "America/New_York"

    # How often the finalisation sweep looks for sittings that have gone quiet.
    sweep_interval_seconds: int = 60

    # Minimum spacing between progress writes to Hardcover, per book. The KOReader plugin
    # uses one minute; Hardcover's free plan allows 60 requests a minute.
    hardcover_throttle_seconds: int = 60

    # The device's address for the metadata sweep. Empty until that ticket lands.
    device_address: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            db_path=_str(env, "DB", cls.db_path),
            device_token=_str(env, "TOKEN"),
            stitch_window_seconds=_int(env, "STITCH_WINDOW_SECONDS", cls.stitch_window_seconds),
            readwise_enabled=_flag(env, "READWISE_ENABLED"),
            readwise_token=_str(env, "READWISE_TOKEN"),
            hardcover_enabled=_flag(env, "HARDCOVER_ENABLED"),
            hardcover_token=_str(env, "HARDCOVER_TOKEN"),
            device_type=_str(env, "DEVICE_TYPE", cls.device_type),
            device_vendor=_str(env, "DEVICE_VENDOR", cls.device_vendor),
            device_model=_str(env, "DEVICE_MODEL", cls.device_model),
            timezone=_str(env, "TIMEZONE", cls.timezone),
            sweep_interval_seconds=_int(env, "SWEEP_INTERVAL_SECONDS", cls.sweep_interval_seconds),
            hardcover_throttle_seconds=_int(
                env, "HARDCOVER_THROTTLE_SECONDS", cls.hardcover_throttle_seconds
            ),
            device_address=_str(env, "DEVICE_ADDRESS"),
        )

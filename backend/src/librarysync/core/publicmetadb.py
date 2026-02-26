from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PUBLICMETADB_PROVIDER = "publicmetadb"
PUBLICMETADB_METADATA_ENABLED_KEY = "metadata_enabled"
PUBLICMETADB_SYNC_ENABLED_KEY = "sync_enabled"
PUBLICMETADB_LEGACY_ENABLED_KEY = "enabled"


def _coerce_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def is_publicmetadb_metadata_enabled(config: Mapping[str, Any] | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    if PUBLICMETADB_METADATA_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_METADATA_ENABLED_KEY))
    if PUBLICMETADB_LEGACY_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_LEGACY_ENABLED_KEY))
    if PUBLICMETADB_SYNC_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_SYNC_ENABLED_KEY))
    return _coerce_enabled(config.get(PUBLICMETADB_LEGACY_ENABLED_KEY))


def is_publicmetadb_sync_enabled(config: Mapping[str, Any] | None) -> bool:
    if not isinstance(config, Mapping):
        return False
    if PUBLICMETADB_SYNC_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_SYNC_ENABLED_KEY))
    if PUBLICMETADB_LEGACY_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_LEGACY_ENABLED_KEY))
    if PUBLICMETADB_METADATA_ENABLED_KEY in config:
        return _coerce_enabled(config.get(PUBLICMETADB_METADATA_ENABLED_KEY))
    return _coerce_enabled(config.get(PUBLICMETADB_LEGACY_ENABLED_KEY))


def set_publicmetadb_metadata_enabled(config: dict[str, Any], enabled: bool) -> None:
    config[PUBLICMETADB_METADATA_ENABLED_KEY] = bool(enabled)


def set_publicmetadb_sync_enabled(config: dict[str, Any], enabled: bool) -> None:
    config[PUBLICMETADB_SYNC_ENABLED_KEY] = bool(enabled)

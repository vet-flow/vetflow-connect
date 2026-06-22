"""Plugin manifest loading and signature verification."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from .crypto import hash_plugin_files, verify

DEV_MODE_HOST_MARKERS = ("localhost", "127.0.0.1", "gruzalab", "test.vet-flow.pl", "vet-flow-demo")


class PluginStatus(str, Enum):
    OK = "ok"
    NO_MANIFEST = "no_manifest"
    INVALID_SIGNATURE = "invalid_signature"
    EXPIRED = "expired"
    TAMPERED = "tampered"
    DEV_MODE = "dev_mode"


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    display_name: str
    author: str
    partner_id: str
    device_serial: str
    license_type: str
    expires_at: datetime | None
    signed_at: datetime | None
    files_hash: str


@dataclass(frozen=True)
class PluginVerification:
    status: PluginStatus
    manifest: PluginManifest | None = None

    @property
    def is_load_allowed(self) -> bool:
        return self.status in {PluginStatus.OK, PluginStatus.EXPIRED, PluginStatus.DEV_MODE}


def is_dev_mode(url: str) -> bool:
    normalized = url.lower()
    return any(marker in normalized for marker in DEV_MODE_HOST_MARKERS)


def load_manifest(path: Path) -> PluginManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return PluginManifest(
        name=raw["name"],
        version=raw["version"],
        display_name=raw.get("display_name", raw["name"]),
        author=raw["author"],
        partner_id=raw["partner_id"],
        device_serial=raw.get("device_serial", "*"),
        license_type=raw["license_type"],
        expires_at=_parse_datetime(raw.get("expires_at")),
        signed_at=_parse_datetime(raw.get("signed_at")),
        files_hash=raw["files_hash"],
    )


def verify_plugin(plugin_dir: Path, public_key: bytes | str, mode: str) -> PluginVerification:
    if mode == "dev":
        manifest = _load_manifest_if_present(plugin_dir / "manifest.json")
        return PluginVerification(status=PluginStatus.DEV_MODE, manifest=manifest)

    manifest_path = plugin_dir / "manifest.json"
    signature_path = plugin_dir / "signature.sig"
    if not manifest_path.exists() or not signature_path.exists():
        return PluginVerification(status=PluginStatus.NO_MANIFEST)

    manifest_bytes = manifest_path.read_bytes()
    manifest = load_manifest(manifest_path)
    signature_bytes = _load_signature(signature_path)
    public_key_bytes = public_key.encode("utf-8") if isinstance(public_key, str) else public_key

    if not verify(manifest_bytes, signature_bytes, public_key_bytes):
        return PluginVerification(status=PluginStatus.INVALID_SIGNATURE, manifest=manifest)

    if manifest.expires_at and datetime.now(UTC) > manifest.expires_at:
        return PluginVerification(status=PluginStatus.EXPIRED, manifest=manifest)

    if hash_plugin_files(plugin_dir) != manifest.files_hash:
        return PluginVerification(status=PluginStatus.TAMPERED, manifest=manifest)

    return PluginVerification(status=PluginStatus.OK, manifest=manifest)


def serialize_manifest(payload: dict) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _load_manifest_if_present(path: Path) -> PluginManifest | None:
    if not path.exists():
        return None
    return load_manifest(path)


def _load_signature(path: Path) -> bytes:
    return base64.b64decode(path.read_text(encoding="utf-8").strip())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

"""Canonical cryptographic binding for one verified media artifact set."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .platforms import BuildTarget

_SCHEMA_VERSION = 1


def artifact_set_path(wheel: Path) -> Path:
    """Return the deterministic binding path adjacent to a media wheel."""
    return wheel.with_name(f"{wheel.name}.artifact-set.json")


def create_artifact_set(
    wheel: Path,
    provenance: Path,
    report: Path,
    compliance_archive: Path,
    *,
    identity: str,
    manifest_sha256: str,
    target: BuildTarget,
    destination: Path | None = None,
) -> Path:
    """Write a canonical manifest binding every generated media artifact."""
    payload = _artifact_payload(
        wheel,
        provenance,
        report,
        compliance_archive,
        identity=identity,
        manifest_sha256=manifest_sha256,
        target=target,
    )
    _validate_document_identities(payload, identity)
    path = destination or artifact_set_path(wheel)
    path.write_text(_canonical_json(payload), encoding="utf-8")
    return path


def validate_artifact_set(
    binding: Path,
    wheel: Path,
    provenance: Path,
    report: Path,
    compliance_archive: Path,
    *,
    verified_report: Mapping[str, object],
    target: BuildTarget,
) -> None:
    """Require a canonical binding matching files and fresh verifier evidence."""
    try:
        raw = binding.read_text(encoding="utf-8")
        actual = json.loads(raw)
        identity = _required_string(verified_report, "identity")
        manifest_sha256 = _required_string(verified_report, "manifest_sha256")
        expected = _artifact_payload(
            wheel,
            provenance,
            report,
            compliance_archive,
            identity=identity,
            manifest_sha256=manifest_sha256,
            target=target,
        )
        errors = _verification_metadata_errors(verified_report, wheel, target)
        errors.extend(_document_identity_errors(expected, identity))
        if raw != _canonical_json(actual):
            errors.append("binding JSON is not canonical")
        if actual != expected:
            errors.append("binding fields or artifact digests do not match")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError(f"artifact set validation failed: {error}") from error
    if errors:
        raise ValueError(f"artifact set validation failed: {'; '.join(errors)}")


def _artifact_payload(
    wheel: Path,
    provenance: Path,
    report: Path,
    compliance_archive: Path,
    *,
    identity: str,
    manifest_sha256: str,
    target: BuildTarget,
) -> dict[str, object]:
    provenance_payload = _load_mapping(provenance)
    report_payload = _load_mapping(report)
    return {
        "compliance_archive": _file_record(compliance_archive),
        "identity": identity,
        "manifest_sha256": manifest_sha256,
        "provenance": _identity_file_record(provenance, provenance_payload),
        "python_abi": target.python_tag,
        "schema_version": _SCHEMA_VERSION,
        "target_id": target.target_id,
        "verification_report": _identity_file_record(report, report_payload),
        "wheel": _file_record(wheel),
    }


def _verification_metadata_errors(
    report: Mapping[str, object], wheel: Path, target: BuildTarget
) -> list[str]:
    expected = {
        "python_tag": target.python_tag,
        "target_id": target.target_id,
        "wheel_filename": wheel.name,
        "wheel_sha256": _sha256(wheel),
    }
    return [
        f"verifier {key} does not match"
        for key, value in expected.items()
        if report.get(key) != value
    ]


def _validate_document_identities(payload: Mapping[str, object], identity: str) -> None:
    errors = _document_identity_errors(payload, identity)
    if errors:
        raise ValueError("; ".join(errors))


def _document_identity_errors(
    payload: Mapping[str, object], identity: str
) -> list[str]:
    errors: list[str] = []
    for name in ("provenance", "verification_report"):
        record = payload[name]
        if not isinstance(record, dict) or record.get("identity") != identity:
            errors.append(f"{name} identity does not match")
    return errors


def _file_record(path: Path) -> dict[str, str]:
    return {"filename": path.name, "sha256": _sha256(path)}


def _identity_file_record(
    path: Path, payload: Mapping[str, object]
) -> dict[str, str]:
    return {
        **_file_record(path),
        "identity": _required_string(payload, "identity"),
    }


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"

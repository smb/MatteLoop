"""Structured, JSON-safe errors that can cross worker boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable codes for application and worker-boundary failures."""

    INVALID_SAMPLING = "invalid_sampling"
    INVALID_CROP = "invalid_crop"
    INVALID_SEGMENTATION = "invalid_segmentation"
    INVALID_FRAMING = "invalid_framing"
    INVALID_OUTPUT = "invalid_output"
    INVALID_RENDER_REQUEST = "invalid_render_request"
    INVALID_FINAL_DIMENSIONS = "invalid_final_dimensions"
    INVALID_ERROR = "invalid_error"
    IMPOSSIBLE_SIZE = "impossible_size"
    JOB_ALREADY_RUNNING = "job_already_running"
    SEGMENTATION_PROCESS_CRASHED = "segmentation_process_crashed"
    MODEL_CHECKSUM_MISMATCH = "model_checksum_mismatch"
    SOURCE_CHANGED = "source_changed"
    CUTS_CHANGED_DURING_SNAPSHOT = "cuts_changed_during_snapshot"
    CLOUD_CONSENT_REQUIRED = "cloud_consent_required"
    SOURCE_NOT_LOCAL = "source_not_local"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_NO_VIDEO = "source_no_video"
    SOURCE_CORRUPT = "source_corrupt"
    SOURCE_ZERO_DURATION = "source_zero_duration"
    SOURCE_HDR_UNSUPPORTED = "source_hdr_unsupported"
    SOURCE_DIMENSIONS_UNSUPPORTED = "source_dimensions_unsupported"
    SOURCE_FPS_UNSUPPORTED = "source_fps_unsupported"
    SOURCE_DURATION_UNSUPPORTED = "source_duration_unsupported"
    SOURCE_FORMAT_UNSUPPORTED = "source_format_unsupported"
    INVALID_THUMBNAIL = "invalid_thumbnail"
    JOB_CANCELLED = "job_cancelled"


class AppError(Exception):
    """A stable error payload suitable for JSON transport and UI presentation."""

    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        message_key: str,
        technical_detail: str,
        retry_action: str,
        job_id: str | None = None,
    ) -> None:
        self.code = code
        self.stage = stage
        self.message_key = message_key
        self.technical_detail = technical_detail
        self.retry_action = retry_action
        self.job_id = job_id
        super().__init__(technical_detail)

    def to_primitives(self) -> dict[str, str | None]:
        """Return JSON primitives only; no enum or path objects cross a boundary."""
        return {
            "code": self.code.value,
            "stage": self.stage,
            "message_key": self.message_key,
            "technical_detail": self.technical_detail,
            "retry_action": self.retry_action,
            "job_id": self.job_id,
        }

    @classmethod
    def from_primitives(cls, payload: object) -> AppError:
        """Recreate an error emitted by a worker from JSON-decoded primitives."""
        if not isinstance(payload, Mapping):
            raise _invalid_primitives("payload must be an object")
        try:
            code = ErrorCode(_required_string(payload, "code"))
            stage = _required_string(payload, "stage")
            message_key = _required_string(payload, "message_key")
            technical_detail = _required_string(payload, "technical_detail")
            retry_action = _required_string(payload, "retry_action")
            job_id = payload.get("job_id")
            if job_id is not None and not isinstance(job_id, str):
                raise ValueError("job_id must be a string or null")
        except ValueError as error:
            raise _invalid_primitives(
                "payload fields do not match the AppError contract"
            ) from error

        if message_key == "error.validation" and retry_action == "correct-input":
            return ValidationError(
                code,
                stage,
                technical_detail,
                message_key,
                retry_action,
                job_id,
            )
        return AppError(
            code,
            stage,
            message_key,
            technical_detail,
            retry_action,
            job_id,
        )


class ValidationError(AppError):
    """An AppError raised when a value violates an immutable domain contract."""

    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        technical_detail: str,
        message_key: str = "error.validation",
        retry_action: str = "correct-input",
        job_id: str | None = None,
    ) -> None:
        super().__init__(
            code=code,
            stage=stage,
            message_key=message_key,
            technical_detail=technical_detail,
            retry_action=retry_action,
            job_id=job_id,
        )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _invalid_primitives(technical_detail: str) -> AppError:
    return AppError(
        ErrorCode.INVALID_ERROR,
        "error-deserialization",
        "error.protocol.invalid-payload",
        technical_detail,
        "restart-worker",
    )

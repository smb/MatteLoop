from __future__ import annotations

import json

import pytest

from rembggui.core.errors import AppError, ErrorCode, ValidationError


def test_app_error_round_trips_json_primitives() -> None:
    error = AppError(
        code=ErrorCode.INVALID_OUTPUT,
        stage="validate-output",
        message_key="error.output.invalid",
        technical_detail="filename must end in .webp",
        retry_action="choose-output",
        job_id="job-42",
    )

    encoded = json.dumps(error.to_primitives())
    restored = AppError.from_primitives(json.loads(encoded))

    assert restored.code is ErrorCode.INVALID_OUTPUT
    assert restored.stage == "validate-output"
    assert restored.message_key == "error.output.invalid"
    assert restored.technical_detail == "filename must end in .webp"
    assert restored.retry_action == "choose-output"
    assert restored.job_id == "job-42"
    assert restored.to_primitives() == error.to_primitives()


def test_validation_error_exposes_code_directly() -> None:
    error = ValidationError(ErrorCode.INVALID_SAMPLING, "sampling", "fps out of range")

    assert isinstance(error, AppError)
    assert error.code is ErrorCode.INVALID_SAMPLING


def test_validation_error_round_trips_when_payload_has_validation_contract() -> None:
    error = ValidationError(ErrorCode.INVALID_SAMPLING, "sampling", "fps out of range")

    restored = AppError.from_primitives(error.to_primitives())

    assert isinstance(restored, ValidationError)
    assert restored.to_primitives() == error.to_primitives()


@pytest.mark.parametrize("payload", [{}, {"job_id": None}])
def test_app_error_accepts_absent_or_null_job_id(payload: dict[str, object]) -> None:
    restored = AppError.from_primitives(
        {
            "code": "invalid_output",
            "stage": "output",
            "message_key": "error.output.invalid",
            "technical_detail": "bad output",
            "retry_action": "choose-output",
            **payload,
        }
    )

    assert restored.job_id is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"code": "invalid_sampling"},
        {
            "code": "not-a-code",
            "stage": "x",
            "message_key": "x",
            "technical_detail": "x",
            "retry_action": "x",
        },
        {
            "code": "invalid_sampling",
            "stage": 1,
            "message_key": "x",
            "technical_detail": "x",
            "retry_action": "x",
        },
        {
            "code": "invalid_sampling",
            "stage": "x",
            "message_key": "x",
            "technical_detail": "x",
            "retry_action": "x",
            "job_id": 1,
        },
        None,
        [],
    ],
)
def test_app_error_malformed_primitives_raise_structured_validation_error(
    payload: object,
) -> None:
    with pytest.raises(ValidationError) as exc:
        AppError.from_primitives(payload)  # type: ignore[arg-type]

    assert isinstance(exc.value, AppError)
    assert exc.value.code is ErrorCode.INVALID_ERROR


def test_app_error_uses_normal_exception_string() -> None:
    error = AppError(
        ErrorCode.INVALID_OUTPUT,
        "output",
        "error.output.invalid",
        "bad output",
        "choose-output",
    )

    assert str(error) == "bad output"

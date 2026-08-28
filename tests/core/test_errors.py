from __future__ import annotations

import json

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

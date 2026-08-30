from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _manifest_error, _set_error, _unsafe_error
    from ._manifest import CutFrame, FrozenJsonMap

__all__ = (
    "_bool",
    "_bounded_int",
    "_bounded_text",
    "_canonical_json",
    "_exact_keys",
    "_fraction_payload",
    "_frame_filename",
    "_frame_from_payload",
    "_freeze_json",
    "_frozen_object",
    "_int",
    "_object",
    "_parse_png_header",
    "_reject_json_constant",
    "_strict_object",
    "_string",
    "_thaw_json",
    "_validate_cache_inputs",
    "_validate_cache_key",
    "_validate_component",
    "_validate_dimensions",
    "_validate_frame_index",
    "_validate_job_id",
    "_validate_path_value",
    "_validate_sha256",
)


def _parse_png_header(header: bytes, filename: str) -> tuple[int, int]:
    if (
        len(header) < 33
        or header[:8] != _PNG_SIGNATURE
        or header[12:16] != b"IHDR"
        or int.from_bytes(header[8:12], "big") != 13
    ):
        raise _set_error(f"frame {filename} is not a canonical PNG")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if header[24] != 8 or header[25] != 6:
        raise _set_error(f"frame {filename} must be 8-bit RGBA PNG")
    if header[26] != 0 or header[27] != 0 or header[28] not in {0, 1}:
        raise _set_error(f"frame {filename} has unsupported PNG encoding")
    return width, height


def _frame_from_payload(value: object) -> CutFrame:
    payload = _object(value, "frame")
    _exact_keys(
        payload,
        {"filename", "height", "index", "mtime_ns", "sha256", "size_bytes", "width"},
        "frame",
    )
    return CutFrame(
        index=_int(payload["index"], "frame index"),
        filename=_string(payload["filename"], "frame filename"),
        width=_int(payload["width"], "frame width"),
        height=_int(payload["height"], "frame height"),
        size_bytes=_int(payload["size_bytes"], "frame size"),
        mtime_ns=_int(payload["mtime_ns"], "frame mtime"),
        sha256=_string(payload["sha256"], "frame sha256"),
    )


def _validate_cache_inputs(inputs: FrozenJsonMap) -> None:
    _exact_keys(
        inputs,
        {
            "crop",
            "edge_settings",
            "model",
            "orientation_color_version",
            "pipeline_schema_version",
            "rembg_version",
            "sampling",
            "source_sha256",
        },
        "cache_key_inputs",
    )
    _validate_sha256(_string(inputs["source_sha256"], "source sha256"), "source sha256")
    sampling = _frozen_object(inputs["sampling"], "sampling")
    _exact_keys(sampling, {"end", "fps", "start"}, "sampling")
    start = _fraction_payload(sampling["start"], "sampling start")
    end = _fraction_payload(sampling["end"], "sampling end")
    if start < 0 or end <= start:
        raise _manifest_error("sampling interval must be positive and half-open")
    _bounded_int(_int(sampling["fps"], "fps"), "fps", minimum=1, maximum=240)
    crop = _frozen_object(inputs["crop"], "crop")
    _exact_keys(crop, {"height", "width", "x", "y"}, "crop")
    _bounded_int(
        _int(crop["x"], "crop x"), "crop x", minimum=0, maximum=MAX_CUT_DIMENSION
    )
    _bounded_int(
        _int(crop["y"], "crop y"), "crop y", minimum=0, maximum=MAX_CUT_DIMENSION
    )
    _validate_dimensions(
        _int(crop["width"], "crop width"), _int(crop["height"], "crop height")
    )
    model = _frozen_object(inputs["model"], "model")
    _exact_keys(model, {"id", "weight_sha256"}, "model")
    _bounded_text(_string(model["id"], "model id"), "model id")
    _validate_sha256(
        _string(model["weight_sha256"], "model weight sha256"),
        "model weight sha256",
    )
    for field in (
        "rembg_version",
        "pipeline_schema_version",
        "orientation_color_version",
    ):
        _bounded_text(_string(inputs[field], field), field)
    edge = _frozen_object(inputs["edge_settings"], "edge_settings")
    _exact_keys(edge, {"alpha_matting", "mode"}, "edge_settings")
    edge_mode = _bounded_text(_string(edge["mode"], "edge mode"), "edge mode")
    if edge_mode not in {"standard", "decontaminate", "alpha_matting"}:
        raise _manifest_error("edge mode is not supported by the pinned catalog")
    matting = _frozen_object(edge["alpha_matting"], "alpha matting")
    _exact_keys(
        matting,
        {"background_threshold", "erode_size", "foreground_threshold"},
        "alpha matting",
    )
    foreground = _bounded_int(
        _int(matting["foreground_threshold"], "matting foreground threshold"),
        "matting foreground threshold",
        minimum=1,
        maximum=255,
    )
    background = _bounded_int(
        _int(matting["background_threshold"], "matting background threshold"),
        "matting background threshold",
        minimum=0,
        maximum=255,
    )
    if background >= foreground:
        raise _manifest_error("matting background must be below foreground")
    _bounded_int(
        _int(matting["erode_size"], "matting erosion size"),
        "matting erosion size",
        minimum=0,
        maximum=_MAX_INT64,
    )


def _fraction_payload(value: FrozenJsonValue, field: str) -> float:
    payload = _frozen_object(value, field)
    _exact_keys(payload, {"denominator", "numerator"}, field)
    numerator = _int(payload["numerator"], f"{field} numerator")
    denominator = _int(payload["denominator"], f"{field} denominator")
    _bounded_int(numerator, field, minimum=0, maximum=_MAX_INT64)
    _bounded_int(denominator, field, minimum=1, maximum=_MAX_INT64)
    return numerator / denominator


def _freeze_json(value: object, *, field: str, depth: int = 0) -> FrozenJsonValue:
    if depth > 16:
        raise _manifest_error(f"{field} nesting exceeds the bound")
    if value is None or type(value) in {str, int, bool}:
        if isinstance(value, str) and (
            len(value) > MAX_SOURCE_PATH_CHARS or "\x00" in value
        ):
            raise _manifest_error(f"{field} contains an invalid string")
        if type(value) is int and not -_MAX_INT64 <= value <= _MAX_INT64:
            raise _manifest_error(f"{field} integer exceeds the bound")
        return cast(JsonScalar, value)
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise _manifest_error(f"{field} object has too many keys")
        items: list[tuple[str, FrozenJsonValue]] = []
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 64:
                raise _manifest_error(f"{field} contains an invalid key")
            items.append((key, _freeze_json(item, field=field, depth=depth + 1)))
        if len({key for key, _item in items}) != len(items):
            raise _manifest_error(f"{field} contains duplicate keys")
        return FrozenJsonMap(items)
    if isinstance(value, (list, tuple)):
        if len(value) > 1024:
            raise _manifest_error(f"{field} array exceeds the bound")
        return tuple(_freeze_json(item, field=field, depth=depth + 1) for item in value)
    raise _manifest_error(f"{field} contains a non-JSON value")


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if type(value) is FrozenJsonMap:
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return cast(JsonScalar, value)


def _canonical_json(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise _manifest_error("manifest data is not canonical JSON") from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _exact_keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = f"{field} keys do not match the strict schema"
        if missing:
            detail += f"; missing {missing[0]!r}"
        if unknown:
            detail += f"; unknown {unknown[0]!r}"
        raise _manifest_error(detail)


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _manifest_error(f"{field} must be an object")
    return value


def _frozen_object(value: FrozenJsonValue, field: str) -> FrozenJsonMap:
    if type(value) is not FrozenJsonMap:
        raise _manifest_error(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str:
        raise _manifest_error(f"{field} must be a string")
    return value


def _int(value: object, field: str) -> int:
    if type(value) is not int:
        raise _manifest_error(f"{field} must be an integer")
    return value


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise _manifest_error(f"{field} must be a boolean")
    return value


def _bounded_int(value: int, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _manifest_error(f"{field} must be between {minimum} and {maximum}")
    return value


def _bounded_text(value: str, field: str) -> str:
    if not value or len(value) > MAX_TEXT_CHARS or "\x00" in value:
        raise _manifest_error(f"{field} must be a bounded non-empty string")
    return value


def _validate_dimensions(width: int, height: int) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or not 1 <= width <= MAX_CUT_DIMENSION
        or not 1 <= height <= MAX_CUT_DIMENSION
        or width * height > MAX_CUT_PIXELS
    ):
        raise _set_error("cut dimensions exceed the supported allocation bound")


def _validate_frame_index(index: int) -> None:
    if type(index) is not int or not 0 <= index < MAX_FRAME_COUNT:
        raise _set_error(f"frame index must be between 0 and {MAX_FRAME_COUNT - 1}")


def _frame_filename(index: int) -> str:
    return f"frame-{index:06d}.png"


def _validate_cache_key(cache_key: str) -> None:
    if type(cache_key) is not str or _CACHE_KEY_RE.fullmatch(cache_key) is None:
        raise _unsafe_error("cache key must be canonical lowercase SHA-256")


def _validate_job_id(job_id: str) -> None:
    if (
        type(job_id) is not str
        or len(job_id) > MAX_JOB_ID_CHARS
        or _SAFE_JOB_RE.fullmatch(job_id) is None
    ):
        raise _unsafe_error("job ID is not a safe workspace component")


def _validate_component(name: str) -> None:
    if (
        type(name) is not str
        or not name
        or len(name) > 255
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or PurePath(name).name != name
        or PureWindowsPath(name).name != name
    ):
        raise _unsafe_error("workspace entry name is unsafe")


def _validate_path_value(path: Path) -> None:
    if not isinstance(path, Path):
        raise _unsafe_error("workspace paths must be Path values")
    text = str(path)
    if not text or len(text) > MAX_PATH_CHARS or "\x00" in text:
        raise _unsafe_error("workspace path exceeds the safety bound")


def _validate_sha256(value: str, field: str) -> str:
    if type(value) is not str or _CACHE_KEY_RE.fullmatch(value) is None:
        raise _manifest_error(f"{field} must be lowercase hexadecimal SHA-256")
    return value

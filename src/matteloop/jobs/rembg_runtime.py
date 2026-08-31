"""Small rembg runtime adapter for builds without alpha-matting dependencies."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from matteloop.jobs.protocol import SegmentOptions, validate_segment_options

_LOGGER = logging.getLogger(__name__)
_ALPHA_MATTING_UNAVAILABLE = (
    "Alpha matting is unavailable because pymatting is not included in this "
    "build; using the standard edge path."
)
_V1_SESSION_MODULES = (
    ("birefnet_general", "BiRefNetSessionGeneral"),
    ("birefnet_general_lite", "BiRefNetSessionGeneralLite"),
    ("birefnet_portrait", "BiRefNetSessionPortrait"),
    ("birefnet_dis", "BiRefNetSessionDIS"),
    ("birefnet_hrsod", "BiRefNetSessionHRSOD"),
    ("birefnet_cod", "BiRefNetSessionCOD"),
    ("birefnet_massive", "BiRefNetSessionMassive"),
    ("dis_anime", "DisSession"),
    ("dis_general_use", "DisSession"),
    ("silueta", "SiluetaSession"),
    ("u2net", "U2netSession"),
    ("u2netp", "U2netpSession"),
    ("u2net_human_seg", "U2netHumanSegSession"),
)

V1_SESSION_MODULE_COUNT = len(_V1_SESSION_MODULES)

type Uint8Frame = NDArray[np.uint8]


def select_edge_options(
    options: SegmentOptions,
) -> tuple[SegmentOptions, str | None]:
    """Select a runnable edge mode and explain an alpha-matting fallback."""
    validate_segment_options(options)
    if options.edge_mode != "alpha_matting" or _pymatting_is_available():
        return options, None
    return replace(options, edge_mode="standard"), _ALPHA_MATTING_UNAVAILABLE


def run_rembg(
    source: Uint8Frame, session: object, options: SegmentOptions
) -> Uint8Frame:
    """Run the standard cutout directly, and rembg only for alpha matting.

    The standard path must not depend on whether pymatting happens to be
    installed. Keying it on availability would mean the test suite exercises
    rembg.remove while every packaged build runs the direct session path — the
    covered code and the shipped code would differ. select_edge_options has
    already downgraded alpha matting when pymatting is absent, so reaching
    _run_with_rembg here proves the importer is safe to execute.
    """
    effective_options, fallback_reason = select_edge_options(options)
    if fallback_reason is not None:
        _LOGGER.warning(fallback_reason)
    if effective_options.edge_mode == "alpha_matting":
        return _run_with_rembg(source, session, effective_options)
    return _run_without_alpha_matting(source, session)


def load_rembg_session_classes() -> object:
    """Load exactly the V1 session classes, bypassing rembg's own importer.

    rembg.sessions exposes all nineteen sessions and reaching it executes
    rembg.bg, which imports pymatting at module level. A frozen build excludes
    both, so resolving through rembg there is impossible. Doing it here anyway
    would mean development resolves nineteen classes while every packaged build
    resolves thirteen — a difference no test could see.
    """
    previous_rembg = sys.modules.get("rembg")
    previous_sessions = sys.modules.get("rembg.sessions")
    rembg_stub = ModuleType("rembg")
    rembg_path = _rembg_search_path()
    sessions_stub = ModuleType("rembg.sessions")
    setattr(rembg_stub, "__path__", rembg_path)
    setattr(
        sessions_stub,
        "__path__",
        [str(Path(path) / "sessions") for path in rembg_path],
    )
    setattr(rembg_stub, "sessions", sessions_stub)
    sys.modules["rembg"] = rembg_stub
    sys.modules["rembg.sessions"] = sessions_stub
    try:
        classes: list[type[Any]] = []
        for module_name, class_name in _V1_SESSION_MODULES:
            module = importlib.import_module(f"rembg.sessions.{module_name}")
            session_class = getattr(module, class_name)
            setattr(sessions_stub, class_name, session_class)
            classes.append(session_class)
        return classes
    finally:
        if previous_rembg is None:
            sys.modules.pop("rembg", None)
        else:
            sys.modules["rembg"] = previous_rembg
        if previous_sessions is None:
            sys.modules.pop("rembg.sessions", None)
        else:
            sys.modules["rembg.sessions"] = previous_sessions


def _pymatting_is_available() -> bool:
    try:
        return importlib.util.find_spec("pymatting") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _rembg_search_path() -> list[str]:
    try:
        spec = importlib.util.find_spec("rembg")
    except (ImportError, ModuleNotFoundError, ValueError):
        return []
    if spec is None or spec.submodule_search_locations is None:
        return []
    return [str(path) for path in spec.submodule_search_locations]


def _run_with_rembg(
    source: Uint8Frame, session: object, options: SegmentOptions
) -> Uint8Frame:
    module = importlib.import_module("rembg")
    remove: Any = getattr(module, "remove")
    kwargs = _session_kwargs(session)
    kwargs["alpha_matting"] = options.edge_mode == "alpha_matting"
    if options.edge_mode == "alpha_matting":
        kwargs.update(
            {
                "alpha_matting_foreground_threshold": (
                    options.alpha_matting_foreground_threshold
                ),
                "alpha_matting_background_threshold": (
                    options.alpha_matting_background_threshold
                ),
                "alpha_matting_erode_size": options.alpha_matting_erode_size,
            }
        )
    return np.ascontiguousarray(
        np.asarray(remove(source, session=_session_object(session), **kwargs))
    )


def _run_without_alpha_matting(source: Uint8Frame, session: object) -> Uint8Frame:
    actual_session, inference_kwargs = _session_parts(session)
    image = Image.fromarray(source)
    masks = actual_session.predict(image, **inference_kwargs)
    cutouts = [
        Image.composite(image, Image.new("RGBA", image.size, 0), mask)
        for mask in masks
    ]
    if not cutouts:
        return np.ascontiguousarray(np.asarray(image))
    result = Image.new(
        "RGBA", (image.width, sum(cutout.height for cutout in cutouts))
    )
    top = 0
    for cutout in cutouts:
        result.paste(cutout, (0, top))
        top += cutout.height
    return np.ascontiguousarray(np.asarray(result))


def _session_object(session: object) -> object:
    return getattr(session, "session", session)


def _session_parts(session: object) -> tuple[Any, dict[str, object]]:
    actual_session = _session_object(session)
    return actual_session, _session_kwargs(session)


def _session_kwargs(session: object) -> dict[str, object]:
    raw_kwargs = getattr(session, "inference_kwargs", ())
    return dict(cast(tuple[tuple[str, object], ...], raw_kwargs))

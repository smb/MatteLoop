"""Allowlisted ONNX Runtime providers and platform-aware presentation."""

from __future__ import annotations

import platform
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeGuard

CPU_EXECUTION_PROVIDER = "CPUExecutionProvider"
COREML_EXECUTION_PROVIDER = "CoreMLExecutionProvider"
CUDA_EXECUTION_PROVIDER = "CUDAExecutionProvider"
ROCM_EXECUTION_PROVIDER = "ROCMExecutionProvider"
MIGRAPHX_EXECUTION_PROVIDER = "MIGraphXExecutionProvider"
DML_EXECUTION_PROVIDER = "DmlExecutionProvider"

# This is intentionally explicit. In particular, AzureExecutionProvider and
# providers added by a future ONNX Runtime release remain local-only excluded.
ALLOWED_EXECUTION_PROVIDERS = (
    CPU_EXECUTION_PROVIDER,
    COREML_EXECUTION_PROVIDER,
    CUDA_EXECUTION_PROVIDER,
    ROCM_EXECUTION_PROVIDER,
    MIGRAPHX_EXECUTION_PROVIDER,
    DML_EXECUTION_PROVIDER,
)
_ALLOWED = frozenset(ALLOWED_EXECUTION_PROVIDERS)


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """One reported, user-selectable execution provider."""

    provider: str
    label: str
    recommended: bool = False


def provider_options(
    available_providers: Iterable[object],
    *,
    system: str | None = None,
    machine: str | None = None,
    device: str | None = None,
    model_id: str = "birefnet-portrait",
    hardware: str | None = None,
) -> tuple[ProviderOption, ...]:
    """Filter, order, and label the providers reported by ONNX Runtime."""
    reported = set(available_providers)
    available = tuple(
        provider for provider in ALLOWED_EXECUTION_PROVIDERS if provider in reported
    )
    if not available:
        return ()
    kind = hardware or _hardware_kind(
        available,
        system=system or platform.system(),
        machine=machine or platform.machine(),
        device=device,
    )
    recommended = _recommended_provider(available, kind)
    ordered = _ordered_providers(available, kind, recommended)
    return tuple(
        ProviderOption(
            provider,
            _provider_label(provider, kind, model_id, recommended),
            provider == recommended,
        )
        for provider in ordered
    )


def provider_options_from_runtime(
    runtime: object | None = None, *, model_id: str = "birefnet-portrait"
) -> tuple[ProviderOption, ...]:
    """Read the installed runtime's provider list without creating a session."""
    if runtime is None:
        import onnxruntime as runtime  # type: ignore[import-untyped,no-redef]
    available = getattr(runtime, "get_available_providers")()
    device = getattr(runtime, "get_device", lambda: None)()
    return provider_options(available, device=str(device), model_id=model_id)


def select_provider(
    stored_provider: object,
    options: tuple[ProviderOption, ...],
) -> str:
    """Apply stored-choice, recommendation, then CPU fallback priority."""
    available = {option.provider for option in options}
    if isinstance(stored_provider, str) and stored_provider in available:
        return stored_provider
    for option in options:
        if option.recommended:
            return option.provider
    if CPU_EXECUTION_PROVIDER in available:
        return CPU_EXECUTION_PROVIDER
    return options[0].provider if options else CPU_EXECUTION_PROVIDER


def is_allowed_provider(value: object) -> TypeGuard[str]:
    """Return whether *value* is one of the six local-only providers."""
    return isinstance(value, str) and value in _ALLOWED


def provider_base_label(provider: str) -> str:
    """Return the human label without recommendation or experiment suffixes."""
    if provider == CPU_EXECUTION_PROVIDER:
        return "CPU"
    if provider == COREML_EXECUTION_PROVIDER:
        return "Apple CoreML"
    if provider == CUDA_EXECUTION_PROVIDER:
        return "NVIDIA CUDA"
    if provider == ROCM_EXECUTION_PROVIDER:
        return "AMD ROCm"
    if provider == MIGRAPHX_EXECUTION_PROVIDER:
        return "AMD MIGraphX"
    return "GPU over DirectML"


def _hardware_kind(
    available: tuple[str, ...], *, system: str, machine: str, device: str | None
) -> str:
    normalized_system = system.casefold()
    normalized_machine = machine.casefold()
    if normalized_system == "darwin" and normalized_machine in {
        "arm64",
        "aarch64",
    }:
        return "apple"
    if CUDA_EXECUTION_PROVIDER in available:
        return "nvidia"
    if ROCM_EXECUTION_PROVIDER in available or MIGRAPHX_EXECUTION_PROVIDER in available:
        return "amd"
    if normalized_system == "windows" and DML_EXECUTION_PROVIDER in available:
        return "directml"
    return "other"


def _recommended_provider(available: tuple[str, ...], hardware: str) -> str | None:
    if hardware == "apple":
        return CPU_EXECUTION_PROVIDER if CPU_EXECUTION_PROVIDER in available else None
    if hardware == "nvidia" and CUDA_EXECUTION_PROVIDER in available:
        return CUDA_EXECUTION_PROVIDER
    if hardware == "amd":
        for provider in (ROCM_EXECUTION_PROVIDER, MIGRAPHX_EXECUTION_PROVIDER):
            if provider in available:
                return provider
    if hardware == "directml" and DML_EXECUTION_PROVIDER in available:
        return DML_EXECUTION_PROVIDER
    if CPU_EXECUTION_PROVIDER in available:
        return CPU_EXECUTION_PROVIDER
    return None


def _ordered_providers(
    available: tuple[str, ...], hardware: str, recommended: str | None
) -> tuple[str, ...]:
    del hardware
    order = ([recommended] if recommended is not None else []) + list(
        ALLOWED_EXECUTION_PROVIDERS
    )
    return tuple(dict.fromkeys(provider for provider in order if provider in available))


def _provider_label(
    provider: str, hardware: str, model_id: str, recommended: str | None
) -> str:
    label = provider_base_label(provider)
    if provider == COREML_EXECUTION_PROVIDER and model_id.startswith("birefnet"):
        return f"{label} – experimentell"
    if provider == recommended:
        return f"{label} – recommended"
    return label

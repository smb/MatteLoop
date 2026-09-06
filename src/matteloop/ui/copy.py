"""Literal-backed translations for data-driven UI values."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication


def model_display_name(model_id: str, fallback: str | None = None) -> str:
    """Translate a model name while keeping the catalog ID as the key."""
    names = {
        "u2net": QCoreApplication.translate("ModelCopy", "U²-Net"),
        "u2netp": QCoreApplication.translate("ModelCopy", "U²-Net P"),
        "u2net_human_seg": QCoreApplication.translate(
            "ModelCopy", "U²-Net Human Segmentation"
        ),
        "u2net_cloth_seg": QCoreApplication.translate(
            "ModelCopy", "U²-Net Cloth Segmentation"
        ),
        "silueta": QCoreApplication.translate("ModelCopy", "Silueta"),
        "isnet-general-use": QCoreApplication.translate(
            "ModelCopy", "IS-Net General Use"
        ),
        "isnet-anime": QCoreApplication.translate("ModelCopy", "IS-Net Anime"),
        "birefnet-general": QCoreApplication.translate("ModelCopy", "BiRefNet General"),
        "birefnet-general-lite": QCoreApplication.translate(
            "ModelCopy", "BiRefNet General Lite"
        ),
        "birefnet-portrait": QCoreApplication.translate(
            "ModelCopy", "BiRefNet Portrait"
        ),
        "birefnet-dis": QCoreApplication.translate("ModelCopy", "BiRefNet DIS"),
        "birefnet-hrsod": QCoreApplication.translate("ModelCopy", "BiRefNet HRSOD"),
        "birefnet-cod": QCoreApplication.translate("ModelCopy", "BiRefNet COD"),
        "birefnet-massive": QCoreApplication.translate("ModelCopy", "BiRefNet Massive"),
        "bria-rmbg": QCoreApplication.translate("ModelCopy", "BRIA RMBG 2.0"),
    }
    return names.get(model_id, fallback or model_id)


def model_purpose(model_id: str, fallback: str) -> str:
    """Translate the catalog purpose associated with a model ID."""
    purposes = {
        "u2net": QCoreApplication.translate(
            "ModelCopy", "General-purpose foreground extraction."
        ),
        "u2netp": QCoreApplication.translate(
            "ModelCopy", "Small, faster general-purpose foreground extraction."
        ),
        "u2net_human_seg": QCoreApplication.translate(
            "ModelCopy", "Human-focused foreground extraction."
        ),
        "u2net_cloth_seg": QCoreApplication.translate(
            "ModelCopy", "Clothing-focused segmentation."
        ),
        "silueta": QCoreApplication.translate(
            "ModelCopy", "Compact general-purpose foreground extraction."
        ),
        "isnet-general-use": QCoreApplication.translate(
            "ModelCopy", "General salient-object segmentation."
        ),
        "isnet-anime": QCoreApplication.translate(
            "ModelCopy", "Anime and illustration foreground extraction."
        ),
        "birefnet-general": QCoreApplication.translate(
            "ModelCopy", "High-quality general foreground extraction."
        ),
        "birefnet-general-lite": QCoreApplication.translate(
            "ModelCopy", "Smaller BiRefNet general foreground extraction."
        ),
        "birefnet-portrait": QCoreApplication.translate(
            "ModelCopy", "High-quality portrait and hair foreground extraction."
        ),
        "birefnet-dis": QCoreApplication.translate(
            "ModelCopy", "Dichotomous image segmentation."
        ),
        "birefnet-hrsod": QCoreApplication.translate(
            "ModelCopy", "High-resolution salient-object detection."
        ),
        "birefnet-cod": QCoreApplication.translate(
            "ModelCopy", "Camouflaged-object detection."
        ),
        "birefnet-massive": QCoreApplication.translate(
            "ModelCopy", "Broad high-capacity foreground extraction."
        ),
        "bria-rmbg": QCoreApplication.translate(
            "ModelCopy", "High-quality general background removal."
        ),
    }
    return purposes.get(model_id, fallback)


def model_license(model_id: str, fallback: str) -> str:
    """Translate the user-facing model licence notice associated with an ID."""
    if model_id in {"u2net", "u2netp", "u2net_human_seg", "u2net_cloth_seg"}:
        return QCoreApplication.translate(
            "ModelCopy",
            "Model terms are provided by the upstream U²-Net project; review them "
            "before redistribution.",
        )
    if model_id == "silueta":
        return QCoreApplication.translate(
            "ModelCopy",
            "Review the Silueta model's upstream terms before redistribution.",
        )
    if model_id == "isnet-general-use":
        return QCoreApplication.translate(
            "ModelCopy",
            "Review the IS-Net model's upstream terms before redistribution.",
        )
    if model_id == "isnet-anime":
        return QCoreApplication.translate(
            "ModelCopy",
            "Review the IS-Net Anime model's upstream terms before redistribution.",
        )
    if model_id.startswith("birefnet"):
        return QCoreApplication.translate(
            "ModelCopy",
            "Review the BiRefNet model's upstream terms before redistribution or "
            "commercial use.",
        )
    if model_id == "bria-rmbg":
        return QCoreApplication.translate(
            "ModelCopy",
            "BRIA RMBG 2.0 has model-specific license terms; commercial use requires "
            "checking and satisfying BRIA's current license.",
        )
    return fallback


def provider_label(provider: str, *, recommended: bool, model_id: str) -> str:
    """Translate an allowlisted execution-provider label and its qualifier."""
    if not model_id:
        return {
            "CPUExecutionProvider": QCoreApplication.translate("ProviderCopy", "CPU"),
            "CoreMLExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "Core ML"
            ),
            "CUDAExecutionProvider": QCoreApplication.translate("ProviderCopy", "CUDA"),
            "ROCMExecutionProvider": QCoreApplication.translate("ProviderCopy", "ROCm"),
            "MIGraphXExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "MIGraphX"
            ),
            "DmlExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "DirectML"
            ),
        }.get(provider, provider)
    base = {
        "CPUExecutionProvider": QCoreApplication.translate("ProviderCopy", "CPU"),
        "CoreMLExecutionProvider": QCoreApplication.translate(
            "ProviderCopy", "Apple CoreML"
        ),
        "CUDAExecutionProvider": QCoreApplication.translate(
            "ProviderCopy", "NVIDIA CUDA"
        ),
        "ROCMExecutionProvider": QCoreApplication.translate("ProviderCopy", "AMD ROCm"),
        "MIGraphXExecutionProvider": QCoreApplication.translate(
            "ProviderCopy", "AMD MIGraphX"
        ),
        "DmlExecutionProvider": QCoreApplication.translate(
            "ProviderCopy", "GPU over DirectML"
        ),
    }.get(provider, provider)
    if provider == "CoreMLExecutionProvider" and model_id.startswith("birefnet"):
        return QCoreApplication.translate("ProviderCopy", "Apple CoreML – experimental")
    if recommended:
        return {
            "CPUExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "CPU – recommended"
            ),
            "CoreMLExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "Apple CoreML – recommended"
            ),
            "CUDAExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "NVIDIA CUDA – recommended"
            ),
            "ROCMExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "AMD ROCm – recommended"
            ),
            "MIGraphXExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "AMD MIGraphX – recommended"
            ),
            "DmlExecutionProvider": QCoreApplication.translate(
                "ProviderCopy", "GPU over DirectML – recommended"
            ),
        }.get(provider, base)
    return base


def provider_notice(value: str) -> str:
    """Translate the bounded provider-fallback notice shown by job dialogs."""
    return {
        "Apple CoreML could not load this model. Processing "
        "continues on the CPU.": (
            QCoreApplication.translate(
                "ProviderCopy",
                "Apple CoreML could not load this model. Processing "
                "continues on the CPU.",
            )
        ),
        "NVIDIA CUDA could not load this model. Processing "
        "continues on the CPU.": (
            QCoreApplication.translate(
                "ProviderCopy",
                "NVIDIA CUDA could not load this model. Processing "
                "continues on the CPU.",
            )
        ),
        "AMD ROCm could not load this model. Processing "
        "continues on the CPU.": (
            QCoreApplication.translate(
                "ProviderCopy",
                "AMD ROCm could not load this model. Processing "
                "continues on the CPU.",
            )
        ),
        "AMD MIGraphX could not load this model. Processing "
        "continues on the CPU.": (
            QCoreApplication.translate(
                "ProviderCopy",
                "AMD MIGraphX could not load this model. Processing "
                "continues on the CPU.",
            )
        ),
        "GPU over DirectML could not load this model. Processing "
        "continues on the CPU.": (
            QCoreApplication.translate(
                "ProviderCopy",
                "GPU over DirectML could not load this model. Processing "
                "continues on the CPU.",
            )
        ),
    }.get(value, value)


def progress_detail(value: str) -> str:
    """Translate known render progress details while preserving their counts."""
    exact = {
        "Promoting cut frames": QCoreApplication.translate(
            "ProgressCopy", "Promoting cut frames"
        ),
        "Validating cut set": QCoreApplication.translate(
            "ProgressCopy", "Validating cut set"
        ),
        "Validating cut snapshot": QCoreApplication.translate(
            "ProgressCopy", "Validating cut snapshot"
        ),
        "Validating encoded output": QCoreApplication.translate(
            "ProgressCopy", "Validating encoded output"
        ),
        "Starting segmentation session": QCoreApplication.translate(
            "ProgressCopy", "Starting segmentation session"
        ),
        "Using cached model weights": QCoreApplication.translate(
            "ProgressCopy", "Using cached model weights"
        ),
        "Reusing prepared session": QCoreApplication.translate(
            "ProgressCopy", "Reusing prepared session"
        ),
    }
    if value in exact:
        return exact[value]
    if value.startswith("Frame ") and " of " in value:
        first, second = value.removeprefix("Frame ").split(" of ", 1)
        return QCoreApplication.translate("ProgressCopy", "Frame %1 of %2").replace(
            "%1", first
        ).replace("%2", second)
    if value.startswith("Cut frame ") and " of " in value:
        first, second = value.removeprefix("Cut frame ").split(" of ", 1)
        return QCoreApplication.translate(
            "ProgressCopy", "Cut frame %1 of %2"
        ).replace("%1", first).replace("%2", second)
    return value


def presented_copy(value: str) -> str:
    """Translate a presenter value at the widget render boundary."""
    exact = {
        "Preview this frame to inspect the cutout": QCoreApplication.translate(
            "Presenter", "Preview this frame to inspect the cutout"
        ),
        "Reading video…": QCoreApplication.translate("Presenter", "Reading video…"),
        "This video could not be read. Open another video.": QCoreApplication.translate(
            "Presenter", "This video could not be read. Open another video."
        ),
        "Preview failed": QCoreApplication.translate("Presenter", "Preview failed"),
        "retry Preview Frame": QCoreApplication.translate(
            "Presenter", "retry Preview Frame"
        ),
        "Current preview — previewing selected frame": QCoreApplication.translate(
            "Presenter", "Current preview — previewing selected frame"
        ),
        "Previewing selected frame": QCoreApplication.translate(
            "Presenter", "Previewing selected frame"
        ),
        "Current preview": QCoreApplication.translate("Presenter", "Current preview"),
        "preview again": QCoreApplication.translate("Presenter", "preview again"),
        "Settings changed — preview again": QCoreApplication.translate(
            "Presenter", "Settings changed — preview again"
        ),
        "Download required": QCoreApplication.translate(
            "Presenter", "Download required"
        ),
        "Model preview — rebuild uses edited cut frames": QCoreApplication.translate(
            "Presenter", "Model preview — rebuild uses edited cut frames"
        ),
        "Edited cuts changed": QCoreApplication.translate(
            "Presenter", "Edited cuts changed"
        ),
        "Prepare & Preview": QCoreApplication.translate(
            "Presenter", "Prepare & Preview"
        ),
        "Preview Frame": QCoreApplication.translate("Presenter", "Preview Frame"),
        "Open another video": QCoreApplication.translate(
            "Presenter", "Open another video"
        ),
        "Drop a video here": QCoreApplication.translate(
            "Presenter", "Drop a video here"
        ),
        "Background-removed result": QCoreApplication.translate(
            "Presenter", "Background-removed result"
        ),
        "Retry Rebuild": QCoreApplication.translate("Presenter", "Retry Rebuild"),
        "Rebuild from edited cuts": QCoreApplication.translate(
            "Presenter", "Rebuild from edited cuts"
        ),
        "Render complete": QCoreApplication.translate("Presenter", "Render complete"),
    }
    if value in exact:
        return exact[value]
    return _presented_dynamic_copy(value)


def _presented_dynamic_copy(value: str) -> str:
    """Translate presenter messages whose stable prefix carries data."""
    if value.startswith("Preview failed: "):
        detail = value.removeprefix("Preview failed: ")
        return QCoreApplication.translate("Presenter", "Preview failed: %s") % detail
    if value.startswith("Preview failed — "):
        retry = value.removeprefix("Preview failed — ")
        return QCoreApplication.translate("Presenter", "Preview failed — %s") % (
            presented_copy(retry)
        )
    if ": " in value:
        category, message = value.split(": ", 1)
        translated_category = {
            "Segmentation": QCoreApplication.translate("Presenter", "Segmentation"),
            "Compute acceleration": QCoreApplication.translate(
                "Presenter", "Compute acceleration"
            ),
            "Sampling": QCoreApplication.translate("Presenter", "Sampling"),
            "Crop & cleanup": QCoreApplication.translate("Presenter", "Crop & cleanup"),
            "Crop": QCoreApplication.translate("Presenter", "Crop"),
            "Framing": QCoreApplication.translate("Presenter", "Framing"),
            "Playhead": QCoreApplication.translate("Presenter", "Playhead"),
            "Export range": QCoreApplication.translate("Presenter", "Export range"),
            "Preview failed": QCoreApplication.translate("Presenter", "Preview failed"),
            "Edited cuts": QCoreApplication.translate("Presenter", "Edited cuts"),
        }.get(category)
        if translated_category is not None:
            return f"{translated_category}: {presented_copy(message)}"
    return value


def main_window_copy(value: str) -> str:
    """Translate fixed copy owned by the main-window render boundary."""
    exact = {
        "MatteLoop": QCoreApplication.translate("MainWindow", "MatteLoop"),
        "Couldn’t read this video": QCoreApplication.translate(
            "MainWindow", "Couldn’t read this video"
        ),
        "Video load error": QCoreApplication.translate(
            "MainWindow", "Video load error"
        ),
        "Render complete": QCoreApplication.translate("MainWindow", "Render complete"),
        "Open output": QCoreApplication.translate("MainWindow", "Open output"),
        "Open folder": QCoreApplication.translate("MainWindow", "Open folder"),
    }
    return exact.get(value, value)


def render_copy(value: str) -> str:
    """Translate fixed copy owned by render confirmations."""
    translated = _render_copy_dialog(value)
    return translated if translated != value else _render_copy_output(value)


def _render_copy_dialog(value: str) -> str:
    """Translate preview and existing-output confirmation copy."""
    exact = {
        "Preview recommended": QCoreApplication.translate(
            "RenderController", "Preview recommended"
        ),
        "Preview this frame before rendering?": QCoreApplication.translate(
            "RenderController", "Preview this frame before rendering?"
        ),
        "A preview lets you verify the cutout before processing the whole video.": (
            QCoreApplication.translate(
                "RenderController",
                "A preview lets you verify the cutout before processing the whole "
                "video.",
            )
        ),
        "Preview first": QCoreApplication.translate(
            "RenderController", "Preview first"
        ),
        "Render anyway": QCoreApplication.translate(
            "RenderController", "Render anyway"
        ),
        "Cancel": QCoreApplication.translate("RenderController", "Cancel"),
        "Matching cut set found": QCoreApplication.translate(
            "RenderController", "Matching cut set found"
        ),
        "A validated cut set matches the current source and settings.": (
            QCoreApplication.translate(
                "RenderController",
                "A validated cut set matches the current source and settings.",
            )
        ),
        "Rebuild reuses the cuts and only reruns framing and encoding.": (
            QCoreApplication.translate(
                "RenderController",
                "Rebuild reuses the cuts and only reruns framing and encoding.",
            )
        ),
        "Rebuild": QCoreApplication.translate("RenderController", "Rebuild"),
        "Regenerate": QCoreApplication.translate("RenderController", "Regenerate"),
    }
    translated = exact.get(value, value)
    return (
        translated
        if translated != value
        else _render_copy_output_confirmation(value)
    )


def _render_copy_output_confirmation(value: str) -> str:
    """Translate existing-output choice labels."""
    return {
        "Output already exists": QCoreApplication.translate(
            "RenderController", "Output already exists"
        ),
        "%s already exists.": QCoreApplication.translate(
            "RenderController", "%s already exists."
        ),
        "Choose how to handle the existing output.": QCoreApplication.translate(
            "RenderController", "Choose how to handle the existing output."
        ),
        "Replace": QCoreApplication.translate("RenderController", "Replace"),
        "Choose another name": QCoreApplication.translate(
            "RenderController", "Choose another name"
        ),
        "Choose output name": QCoreApplication.translate(
            "RenderController", "Choose output name"
        ),
        "WebP files (*.webp)": QCoreApplication.translate(
            "RenderController", "WebP files (*.webp)"
        ),
    }.get(value, value)


def _render_copy_output(value: str) -> str:
    """Translate the two render-progress labels shared with the dialog."""
    return {
        "Rebuilding from edited cuts": QCoreApplication.translate(
            "PreviewJobDialog", "Rebuilding from edited cuts"
        ),
        "Rendering video": QCoreApplication.translate(
            "PreviewJobDialog", "Rendering video"
        ),
    }.get(value, value)


def source_error_message(value: str) -> str:
    """Translate the plain-language source recovery instruction."""
    messages = {
        "Open a video stored on this Mac.": QCoreApplication.translate(
            "SourceErrors", "Open a video stored on this Mac."
        ),
        "Open a video file that can be opened and read.": QCoreApplication.translate(
            "SourceErrors", "Open a video file that can be opened and read."
        ),
        "Open a file that contains a video track.": QCoreApplication.translate(
            "SourceErrors", "Open a file that contains a video track."
        ),
        "Open another video file; this one appears damaged.": (
            QCoreApplication.translate(
                "SourceErrors", "Open another video file; this one appears damaged."
            )
        ),
        "Open a video with a positive duration.": QCoreApplication.translate(
            "SourceErrors", "Open a video with a positive duration."
        ),
        "Convert to 8-bit SDR and try again.": QCoreApplication.translate(
            "SourceErrors", "Convert to 8-bit SDR and try again."
        ),
        "Resize to between 8×8 and 3840×2160.": QCoreApplication.translate(
            "SourceErrors", "Resize to between 8×8 and 3840×2160."
        ),
        "Convert the video to 60 fps or less.": QCoreApplication.translate(
            "SourceErrors", "Convert the video to 60 fps or less."
        ),
        "Open a video under 10 minutes.": QCoreApplication.translate(
            "SourceErrors", "Open a video under 10 minutes."
        ),
        "Open an MP4, MOV, WebM, or MKV video.": QCoreApplication.translate(
            "SourceErrors", "Open an MP4, MOV, WebM, or MKV video."
        ),
        "This video could not be read. Open another video.": QCoreApplication.translate(
            "SourceErrors", "This video could not be read. Open another video."
        ),
    }
    return messages.get(value, value)


def section_title(key: str) -> str:
    """Translate one inspector section identified by its stable key."""
    return {
        "segmentation": QCoreApplication.translate("Inspector", "Segmentation"),
        "time_sampling": QCoreApplication.translate("Inspector", "Time & Sampling"),
        "crop_cleanup": QCoreApplication.translate("Inspector", "Crop & Cleanup"),
        "transform": QCoreApplication.translate("Inspector", "Transform"),
        "output": QCoreApplication.translate("Inspector", "Output"),
        "workspace": QCoreApplication.translate("Inspector", "Workspace"),
    }.get(key, key)


def crop_field_label(name: str) -> str:
    """Translate a crop field label without translating its stable field key."""
    return {
        "x": QCoreApplication.translate("Inspector", "X"),
        "y": QCoreApplication.translate("Inspector", "Y"),
        "width": QCoreApplication.translate("Inspector", "Width"),
        "height": QCoreApplication.translate("Inspector", "Height"),
    }.get(name, name)


def model_status(status: str) -> tuple[str, str]:
    """Return translated status marker and label for the model picker."""
    return {
        "ready": ("●", QCoreApplication.translate("Inspector", "Ready")),
        "downloading": ("◌", QCoreApplication.translate("Inspector", "Downloading")),
        "not_cached": ("○", QCoreApplication.translate("Inspector", "Not cached")),
    }.get(status, ("○", QCoreApplication.translate("Inspector", "Not cached")))


def inspector_label(value: str) -> str:
    """Translate a fixed inspector form label."""
    return {
        "Model": QCoreApplication.translate("Inspector", "Model"),
        "Compute acceleration": QCoreApplication.translate(
            "Inspector", "Compute acceleration"
        ),
        "Edge treatment": QCoreApplication.translate("Inspector", "Edge treatment"),
        "Output FPS": QCoreApplication.translate("Inspector", "Output FPS"),
        "Start": QCoreApplication.translate("Inspector", "Start"),
        "End": QCoreApplication.translate("Inspector", "End"),
        "Duration": QCoreApplication.translate("Inspector", "Duration"),
        "Alpha threshold": QCoreApplication.translate("Inspector", "Alpha threshold"),
        "Padding": QCoreApplication.translate("Inspector", "Padding"),
        "Horizontal stretch": QCoreApplication.translate(
            "Inspector", "Horizontal stretch"
        ),
        "Directory": QCoreApplication.translate("Inspector", "Directory"),
        "Filename": QCoreApplication.translate("Inspector", "Filename"),
        "Maximum size": QCoreApplication.translate("Inspector", "Maximum size"),
        "X": QCoreApplication.translate("Inspector", "X"),
        "Y": QCoreApplication.translate("Inspector", "Y"),
        "Width": QCoreApplication.translate("Inspector", "Width"),
        "Height": QCoreApplication.translate("Inspector", "Height"),
    }.get(value, value)


def accessible_field_name(name: str) -> str:
    """Translate a field's spoken name from its stable object key."""
    return {
        "start": QCoreApplication.translate("Inspector", "Start"),
        "end": QCoreApplication.translate("Inspector", "End"),
        "duration": QCoreApplication.translate("Inspector", "Duration"),
        "width": QCoreApplication.translate("Inspector", "Width"),
        "height": QCoreApplication.translate("Inspector", "Height"),
        "x": QCoreApplication.translate("Inspector", "X"),
        "y": QCoreApplication.translate("Inspector", "Y"),
    }.get(name, name)

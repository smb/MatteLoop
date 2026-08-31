"""Background preview orchestration package.

The package initializer preserves the import surface of the former
preview_controller.py module.
"""

from matteloop.ui.download_transport import (
    QtNetworkDownloadTransport as _QtNetworkDownloadTransport,
)
from matteloop.ui.preview_controller.controller import PreviewController
from matteloop.ui.preview_controller.dialog import PreviewJobDialog
from matteloop.ui.preview_controller.request_assembly import (
    _preview_inputs,
    _PreviewInputs,
    _render_request,
)
from matteloop.ui.preview_controller.runtime import (
    PreviewRuntime,
    ProductionPreviewRuntime,
)
from matteloop.ui.preview_controller.worker import (
    _cancel_runtime,
    _notification_job_id,
    _preview_error,
    _PreviewWorker,
    _qimage_from_rgba,
)

__all__ = [
    "PreviewController",
    "PreviewJobDialog",
    "PreviewRuntime",
    "ProductionPreviewRuntime",
    "_PreviewInputs",
    "_PreviewWorker",
    "_QtNetworkDownloadTransport",
    "_cancel_runtime",
    "_notification_job_id",
    "_preview_error",
    "_preview_inputs",
    "_qimage_from_rgba",
    "_render_request",
]

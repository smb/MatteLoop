"""Plain-language copy for source failures in the presentation layer."""

from matteloop.core.errors import AppError, ErrorCode

_SOURCE_ERROR_MESSAGES = {
    ErrorCode.SOURCE_NOT_LOCAL: "Open a video stored on this Mac.",
    ErrorCode.SOURCE_UNREADABLE: "Open a video file that can be opened and read.",
    ErrorCode.SOURCE_NO_VIDEO: "Open a file that contains a video track.",
    ErrorCode.SOURCE_CORRUPT: "Open another video file; this one appears damaged.",
    ErrorCode.SOURCE_ZERO_DURATION: "Open a video with a positive duration.",
    ErrorCode.SOURCE_HDR_UNSUPPORTED: "Convert to 8-bit SDR and try again.",
    ErrorCode.SOURCE_DIMENSIONS_UNSUPPORTED: "Resize to between 8×8 and 3840×2160.",
    ErrorCode.SOURCE_FPS_UNSUPPORTED: "Convert the video to 60 fps or less.",
    ErrorCode.SOURCE_DURATION_UNSUPPORTED: "Open a video under 10 minutes.",
    ErrorCode.SOURCE_FORMAT_UNSUPPORTED: "Open an MP4, MOV, WebM, or MKV video.",
}
_SOURCE_ERROR_FALLBACK = "This video could not be read. Open another video."


def source_error_copy(error: object) -> str:
    if isinstance(error, AppError):
        return _SOURCE_ERROR_MESSAGES.get(error.code, _SOURCE_ERROR_FALLBACK)
    return _SOURCE_ERROR_FALLBACK

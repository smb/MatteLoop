"""Plain-language copy for source failures in the presentation layer."""

from rembggui.core.errors import AppError, ErrorCode

_SOURCE_ERROR_MESSAGES = {
    ErrorCode.SOURCE_NOT_LOCAL: "Choose a video stored on this Mac.",
    ErrorCode.SOURCE_UNREADABLE: "Choose a video file that can be opened and read.",
    ErrorCode.SOURCE_NO_VIDEO: "Choose a file that contains a video track.",
    ErrorCode.SOURCE_CORRUPT: "Choose another video file; this one appears damaged.",
    ErrorCode.SOURCE_ZERO_DURATION: "Choose a video with a positive duration.",
    ErrorCode.SOURCE_HDR_UNSUPPORTED: "Convert to 8-bit SDR and try again.",
    ErrorCode.SOURCE_DIMENSIONS_UNSUPPORTED: "Resize to 3840×2160 or smaller.",
    ErrorCode.SOURCE_FPS_UNSUPPORTED: "Convert the video to 60 fps or less.",
    ErrorCode.SOURCE_DURATION_UNSUPPORTED: "Choose a video under 10 minutes.",
    ErrorCode.SOURCE_FORMAT_UNSUPPORTED: "Choose an MP4, MOV, WebM, or MKV video.",
}
_SOURCE_ERROR_FALLBACK = "This video could not be read. Choose another video."


def source_error_copy(error: object) -> str:
    if isinstance(error, AppError):
        return _SOURCE_ERROR_MESSAGES.get(error.code, _SOURCE_ERROR_FALLBACK)
    return _SOURCE_ERROR_FALLBACK

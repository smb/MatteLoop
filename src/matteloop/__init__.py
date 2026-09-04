"""MatteLoop package."""

from PIL import Image

__version__ = "0.2.1"

# Pillow warns above this value and errors above twice this value. Align the
# warning boundary with the largest legal MatteLoop canvas without disabling the
# decompression-bomb protection globally.
PILLOW_MAX_IMAGE_PIXELS = 16_383**2
Image.MAX_IMAGE_PIXELS = PILLOW_MAX_IMAGE_PIXELS

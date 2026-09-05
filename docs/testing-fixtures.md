# Test fixtures

## Synthetic fixture rotation

`tests.fixtures.media_factory.make_video(..., rotation=...)` writes a sorted,
versioned adjacent sidecar named `<video>.matteloop.json`. It records the
counter-clockwise presentation rotation as `rotation_ccw`. The locked PyAV 16
wheel cannot author portable MP4 display-matrix metadata, so future synthetic
source-decoder tests must consume this explicit fixture contract instead of
expecting a display matrix in the video container.

# Building native artifacts

MatteLoop uses `pyside6-deploy` with Nuitka in standalone mode. Builds are
platform-specific: build the macOS artifact on an arm64 Mac and the Windows
artifact on an x64 Windows machine. Linux artifacts remain deferred.

The release artifacts are deliberately unsigned. macOS Gatekeeper and Windows
SmartScreen will warn on first launch; the steps below explain how to continue
after checking that the artifact came from the expected build.

The app icon is shipped as committed derived assets:
`assets/branding/matteloop/derived/matteloop.icns` for macOS and
`assets/branding/matteloop/derived/matteloop.ico` for Windows. The build helper
verifies both formats and selects the platform-appropriate asset; the corrected
1024 px masters remain alongside the original design masters.

## Prerequisites

You need:

- CPython 3.13.x. The project accepts `>=3.13,<3.14`.
- `uv` 0.11.32. The workflow pins this version and `uv.lock` pins the Python
  dependencies.
- macOS 13 or later on arm64, plus the Xcode Command Line Tools. Install the
  tools with `xcode-select --install` if `xcode-select -p` reports that they
  are missing.
- Windows x64 with the Visual Studio 2022 Build Tools, including the Desktop
  development with C++ workload, MSVC v143, and a Windows SDK. The hosted
  `windows-2022` runner already provides these tools.

The locked build environment includes PySide6 6.10.3 (the project constraint
is `~=6.10.1`), Nuitka 2.8.10, PyAV 16.1.0, Pillow 12.3.0, NumPy 2.5.2,
`rembg` 2.0.72, and CPU `onnxruntime` 1.29.0. Do not install a second Python
environment for the build; run the commands below from the repository root.

## macOS

Confirm the machine and install the locked environment:

```sh
uname -m
xcode-select -p
uv --version
uv sync --frozen --all-groups
```

`uname -m` must print `arm64`. Then build:

```sh
uv run --frozen --no-sync python scripts/build.py
```

The unsigned app bundle is written to `dist/MatteLoop.app`. The helper checks
the pinned build tools and every data file named by the spec, invokes
`pyside6-deploy`, and fails if the command fails or the bundle is absent or
empty. It also runs the existing offline native smoke test before succeeding.
A clean build takes roughly 5–10 minutes and produces an uncompressed
standalone bundle of roughly 600–700 MiB, depending on the exact dependency
wheels and compiler cache state. The local macOS compile produced a 628 MiB
bundle; these figures are planning estimates, not a promise about a particular
machine. Its generated app reached the frozen smoke test but this sandbox
denied the POSIX shared-memory step, so it is not claimed as fully verified.

## Windows

Open PowerShell in the repository root. Confirm the interpreter and toolchain,
then install the locked environment:

```powershell
py -3.13 --version
uv --version
uv sync --frozen --all-groups
```

Build the x64 bundle:

```powershell
uv run --frozen --no-sync python scripts/build.py
```

The standalone bundle is written to `dist\MatteLoop.dist`, with the executable
inside that directory. A Windows build is expected to take roughly 5–10
minutes and to be in the same rough 600–700 MiB uncompressed size range, but
those figures are estimates until a Windows host runs the build. The Windows
build and the GitHub Actions workflow have not been executed in this local
workspace.

## Models and first launch

Model weights are not bundled into either artifact. On the first Preview or
Render using a model, MatteLoop downloads that model from the upstream rembg
release location recorded in `resources/model-manifest.json` and caches it
locally. Later runs reuse the cache; the first use therefore needs network
access and enough disk space.

The default model cache is `~/Library/Caches/matteloop/models` on macOS.
Existing `~/Library/Caches/rembggui/` subdirectories — weights, compiled
provider caches and thumbnails — are still used when the new location does not
exist; if both exist, the MatteLoop location is preferred.

The 13 V1 models range from about 4.4 MiB (`u2netp`) to 927.6 MiB for the
largest BiRefNet models. The default `birefnet-portrait` is about 927.6 MiB;
downloading the complete V1 catalog would be about 6.35 GiB. These weights
are separate from the native bundle size.

## Running unsigned artifacts

On macOS, double-click `MatteLoop.app`. If Gatekeeper says the developer cannot
be verified, Control-click the app in Finder, choose **Open**, then confirm
**Open**. If that action is not offered, open **System Settings → Privacy &
Security**, find the blocked-app notice, choose **Open Anyway**, authenticate,
and confirm. Only do this after verifying the artifact and its build source.

On Windows, open `dist\MatteLoop.dist` and double-click the executable. When
SmartScreen displays **Windows protected your PC**, choose **More info**, then
**Run anyway**. If Windows still blocks it, open the file’s **Properties**,
select **Unblock**, apply the change, and run it again after verifying the
artifact.

## Verification status

The local checks cover the spec’s dry-run parsing, the source resource list,
the build helper’s prerequisite/output checks, and the temporary PyAV wheel
handling. The release workflow is intentionally a manually dispatched draft
and has not been run by this checkout. GitHub Actions execution, Windows
compilation, artifact upload, and SmartScreen behavior are unexercised here.
The local macOS compile completed and produced a 628 MiB bundle. Its executable
reached the offline frozen smoke test, then failed because this sandbox denies
POSIX shared-memory creation; no fully runnable macOS artifact is claimed as
verified. The frozen shared-memory boundary, Windows compilation, GitHub
Actions execution, artifact upload, and SmartScreen behavior remain
unexercised. The full local test run had 1,269 passes and 39 failures caused by
the same sandbox restrictions on POSIX shared memory or localhost sockets; an
unrelated memory-threshold test passed when rerun alone.

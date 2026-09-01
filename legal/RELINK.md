# Rebuilding and Replacing Qt/PySide 6.10.3

These instructions are installation information for the dynamically linked
Qt/PySide parts of the unsigned MatteLoop native application. They are not a
project signing, notarization, release, or support promise.

Replacement libraries and bindings must remain ABI-compatible with Qt/PySide
6.10.3 and with the bundle's platform and architecture. The complete app
requires macOS 15 or later; an ABI-compatible replacement Qt/PySide runtime
must preserve the app's macOS 15 support floor. The separately built custom
media FFmpeg/libwebp dylibs retain a 13.0 minimum and are not rebuilt from this
Qt source companion. This app has not been launched on an actual macOS 15 host.
Keep an untouched copy of the application before replacing files.

## Verify and unpack the source companion

On macOS:

```sh
shasum -a 256 -c MatteLoop-qt-sources-6.10.3-<identity>.tar.gz.sha256
mkdir qt-source-companion
tar -xzf MatteLoop-qt-sources-6.10.3-<identity>.tar.gz \
  -C qt-source-companion
cd qt-source-companion
shasum -a 256 \
  sources/qtbase-everywhere-src-6.10.3.tar.xz \
  sources/qtimageformats-everywhere-src-6.10.3.tar.xz \
  sources/pyside-setup-everywhere-src-6.10.3.tar.xz
```

On Windows PowerShell:

```powershell
$pair = Get-Content .\MatteLoop-qt-sources-6.10.3-<identity>.tar.gz.sha256
$expected = ($pair -split '\s+')[0]
$actual = (Get-FileHash .\MatteLoop-qt-sources-6.10.3-<identity>.tar.gz -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "Qt source companion checksum mismatch" }
New-Item -ItemType Directory -Force qt-source-companion | Out-Null
tar -xzf .\MatteLoop-qt-sources-6.10.3-<identity>.tar.gz -C .\qt-source-companion
Set-Location .\qt-source-companion
Get-FileHash .\sources\*.tar.xz -Algorithm SHA256
```

Compare the three inner hashes with `source-checksums.json`. Inspect
`package-inventory.json`, `component-inventory.json`, and `patches/README.md`
before rebuilding.

## Build the replacement components on macOS

Use an arm64 macOS toolchain with CMake and Ninja, Xcode command-line tools,
and CPython 3.13. Configure the replacement Qt/PySide runtime for the complete
app's macOS 15 support floor:

```sh
mkdir -p work/src work/build work/install
tar -xf sources/qtbase-everywhere-src-6.10.3.tar.xz -C work/src
tar -xf sources/qtimageformats-everywhere-src-6.10.3.tar.xz -C work/src
tar -xf sources/pyside-setup-everywhere-src-6.10.3.tar.xz -C work/src

cmake -S work/src/qtbase-everywhere-src-6.10.3 -B work/build/qtbase -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
  -DCMAKE_INSTALL_PREFIX="$PWD/work/install" \
  -DQT_BUILD_EXAMPLES=OFF -DQT_BUILD_TESTS=OFF
cmake --build work/build/qtbase --parallel
cmake --install work/build/qtbase

cmake -S work/src/qtimageformats-everywhere-src-6.10.3 \
  -B work/build/qtimageformats -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
  -DCMAKE_PREFIX_PATH="$PWD/work/install" \
  -DCMAKE_INSTALL_PREFIX="$PWD/work/install" \
  -DQT_BUILD_EXAMPLES=OFF -DQT_BUILD_TESTS=OFF
cmake --build work/build/qtimageformats --parallel
cmake --install work/build/qtimageformats
```

Build Shiboken and PySide from
`work/src/pyside-setup-everywhere-src-6.10.3` with the same CPython 3.13 and
the newly installed `work/install/bin/qtpaths`. The authoritative command
options and prerequisites for this exact source are in that tree's
`README.pyside6.md` and `coin_build_instructions.py`. A typical local build is:

```sh
cd work/src/pyside-setup-everywhere-src-6.10.3
MACOSX_DEPLOYMENT_TARGET=15.0 python3.13 setup.py build \
  --qtpaths="$OLDPWD/work/install/bin/qtpaths" \
  --ignore-git --parallel=4
```

Use the build output, not a differently versioned stock wheel, when replacing
bindings that must correspond to the rebuilt Qt libraries.

In a working copy of `MatteLoop.app`, replace the corresponding shared Qt
libraries (`Contents/MacOS/QtCore`, `QtDBus`, `QtGui`, `QtNetwork`, and
`QtWidgets`), PySide/shiboken extension modules and dylibs under
`Contents/MacOS/PySide6` and `Contents/MacOS/shiboken6`, and the platform and
image-format plugins under `Contents/MacOS/PySide6/qt-plugins`. Preserve the
existing filenames, install names, and relative loader paths; compare them
before and after with `otool -L`.

Replacing Mach-O files invalidates an existing signature. For local testing of
this unsigned build only, macOS may require an ad-hoc signature:

```sh
codesign --force --deep --sign - MatteLoop.app
QT_QPA_PLATFORM=offscreen ./MatteLoop.app/Contents/MacOS/matteloop --smoke-test
```

That ad-hoc command is only a local unsigned-use qualification step. It is not
a distributable signature and is not a claim of MatteLoop signing or
notarization support.

## Build and replace on Windows x64

Use a Visual Studio 2022 x64 developer prompt, CMake, Ninja, and CPython 3.13.
Unpack the same three archives, configure Qt Base as shared libraries, install
it, then configure Qt Image Formats against that install:

```powershell
cmake -S work\src\qtbase-everywhere-src-6.10.3 -B work\build\qtbase -G Ninja `
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON `
  -DCMAKE_INSTALL_PREFIX="$PWD\work\install" `
  -DQT_BUILD_EXAMPLES=OFF -DQT_BUILD_TESTS=OFF
cmake --build work\build\qtbase --parallel
cmake --install work\build\qtbase

cmake -S work\src\qtimageformats-everywhere-src-6.10.3 `
  -B work\build\qtimageformats -G Ninja `
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON `
  -DCMAKE_PREFIX_PATH="$PWD\work\install" `
  -DCMAKE_INSTALL_PREFIX="$PWD\work\install" `
  -DQT_BUILD_EXAMPLES=OFF -DQT_BUILD_TESTS=OFF
cmake --build work\build\qtimageformats --parallel
cmake --install work\build\qtimageformats
```

Build Shiboken/PySide from the included PySide Setup source with the same
CPython 3.13 and `work\install\bin\qtpaths.exe`, following that source tree's
exact Windows prerequisites and build options.

In a working copy of `MatteLoop.dist`, replace the matching Qt 6 DLLs, PySide6
and shiboken6 `.pyd`/DLL files, and `platforms` and `imageformats` plugin DLLs.
The concrete delivered paths are recorded in `package-inventory.json`; locate
the standalone copies with `Get-ChildItem -Recurse`. Preserve names and search
paths, then run:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\MatteLoop.dist\matteloop.exe --smoke-test
```

Windows native qualification has not been performed by this project. These
steps document the replacement boundary; they do not claim a successful
Windows build, launch, signature, or installer.

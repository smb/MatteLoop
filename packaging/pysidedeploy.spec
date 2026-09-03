[app]
title = MatteLoop
project_dir = .
input_file = packaging/entrypoint.py
exec_directory = dist
project_file =
icon = assets/branding/matteloop/derived/matteloop.icns

[python]
python_path =
packages = Nuitka==2.8.10
android_packages =

[qt]
qml_files =
excluded_qml_plugins =
modules = Core,DBus,Gui,Network,Widgets
plugins = platforms,imageformats

[android]
wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]
macos.permissions =
mode = standalone
extra_args =
	--quiet
	--jobs=2
	--no-deployment-flag=self-execution
	--assume-yes-for-downloads
	--windows-console-mode=attach
	--noinclude-qt-translations
	--output-filename=matteloop
	--include-qt-plugins=platforms,imageformats
	--include-package=matteloop
	--include-module=matteloop.smoke_child
	--include-module=multiprocessing.resource_tracker
	--include-module=multiprocessing.spawn
	--include-module=rembg.sessions.base
	--include-module=rembg.sessions.birefnet_general
	--include-module=rembg.sessions.birefnet_general_lite
	--include-module=rembg.sessions.birefnet_portrait
	--include-module=rembg.sessions.birefnet_dis
	--include-module=rembg.sessions.birefnet_hrsod
	--include-module=rembg.sessions.birefnet_cod
	--include-module=rembg.sessions.birefnet_massive
	--include-module=rembg.sessions.dis_anime
	--include-module=rembg.sessions.dis_general_use
	--include-module=rembg.sessions.silueta
	--include-module=rembg.sessions.u2net
	--include-module=rembg.sessions.u2netp
	--include-module=rembg.sessions.u2net_human_seg
	--include-package=onnxruntime
	--include-package-data=onnxruntime
	--nofollow-import-to=pymatting
	--nofollow-import-to=numba
	--nofollow-import-to=llvmlite
	--nofollow-import-to=rembg.bg
	--nofollow-import-to=scipy
	--nofollow-import-to=skimage
	--nofollow-import-to=av
	--nofollow-import-to=onnxruntime.backend
	--noinclude-dlls=*libqpdf*
	--noinclude-dlls=*QtPdf*
	--noinclude-dlls=*libqsvg*
	--noinclude-dlls=*QtSvg*
	--include-module=PIL._imaging
	--include-module=PIL._webp
	--include-module=PIL.PngImagePlugin
	--include-module=PIL.WebPImagePlugin
	--include-data-files=resources/model-manifest.json=resources/model-manifest.json
	--include-data-files=resources/model-provenance.json=resources/model-provenance.json
	--include-data-files=LICENSE=LICENSE
	--include-data-files=THIRD_PARTY_NOTICES.md=THIRD_PARTY_NOTICES.md
	--include-data-files=legal/GPL-3.0.txt=GPL-3.0.txt
	--include-data-files=legal/LGPL-3.0.txt=LGPL-3.0.txt
	--include-data-files=legal/DIRECTML-LICENSE.txt=DIRECTML-LICENSE.txt
	--include-data-files=legal/QT-PYSIDE-LGPL-NOTICE.md=QT-PYSIDE-LGPL-NOTICE.md
	--include-data-files=legal/RELINK.md=RELINK.md
	--include-data-files=resources/fonts/IBMPlexSans-Regular.ttf=resources/fonts/IBMPlexSans-Regular.ttf
	--include-data-files=resources/fonts/IBMPlexSans-SemiBold.ttf=resources/fonts/IBMPlexSans-SemiBold.ttf
	--include-data-files=resources/fonts/IBMPlexMono-Regular.ttf=resources/fonts/IBMPlexMono-Regular.ttf
	--include-data-files=resources/fonts/OFL.txt=resources/fonts/OFL.txt
	--include-data-files=resources/icons/error-24.png=resources/icons/error-24.png
	--include-data-files=resources/icons/error-32.png=resources/icons/error-32.png
	--include-data-files=resources/icons/error-48.png=resources/icons/error-48.png
	--include-data-files=resources/icons/error-64.png=resources/icons/error-64.png
	--include-data-files=resources/icons/preview-24.png=resources/icons/preview-24.png
	--include-data-files=resources/icons/preview-32.png=resources/icons/preview-32.png
	--include-data-files=resources/icons/preview-48.png=resources/icons/preview-48.png
	--include-data-files=resources/icons/preview-64.png=resources/icons/preview-64.png
	--include-data-files=resources/icons/stale-24.png=resources/icons/stale-24.png
	--include-data-files=resources/icons/stale-32.png=resources/icons/stale-32.png
	--include-data-files=resources/icons/stale-48.png=resources/icons/stale-48.png
	--include-data-files=resources/icons/stale-64.png=resources/icons/stale-64.png
	--nofollow-import-to=tests
	--nofollow-import-to=pytest
	--noinclude-data-files=**/tests/**
	--noinclude-data-files=**/*token*
	--noinclude-data-files=**/*.onnx
	--noinclude-data-files=**/models/**
	--noinclude-data-files=**/*.mp4
	--noinclude-data-files=**/*.mov
	--noinclude-data-files=**/*.webm
	--noinclude-data-files=**/*.mkv
	--noinclude-data-files=**/.venv/**

[buildozer]
mode = debug
recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =

[app]
title = rembgGUI
project_dir = .
input_file = packaging/entrypoint.py
exec_directory = dist
project_file =
icon =

[python]
python_path =
packages = Nuitka==2.8.10
android_packages =

[qt]
qml_files =
excluded_qml_plugins =
modules = Core,DBus,Gui,Widgets
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
	--noinclude-qt-translations
	--output-filename=rembggui
	--include-qt-plugins=platforms,imageformats
	--include-package=rembggui
	--include-module=rembggui.smoke_child
	--include-module=multiprocessing.resource_tracker
	--include-module=multiprocessing.spawn
	--include-package=rembg.sessions
	--include-package=onnxruntime
	--include-package-data=onnxruntime
	--include-package=av
	--include-package-data=av
	--include-module=PIL.PngImagePlugin
	--include-module=PIL.WebPImagePlugin
	--include-data-files=resources/model-manifest.json=resources/model-manifest.json
	--include-data-files=resources/model-provenance.json=resources/model-provenance.json
	--include-data-files=resources/fonts/IBMPlexSans-Regular.ttf=resources/fonts/IBMPlexSans-Regular.ttf
	--include-data-files=resources/fonts/IBMPlexSans-SemiBold.ttf=resources/fonts/IBMPlexSans-SemiBold.ttf
	--include-data-files=resources/fonts/IBMPlexMono-Regular.ttf=resources/fonts/IBMPlexMono-Regular.ttf
	--include-data-files=resources/fonts/OFL.txt=resources/fonts/OFL.txt
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

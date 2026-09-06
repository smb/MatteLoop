from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER = re.compile(r"%(?:n|[0-9]+|s)")


def _placeholders(text: str) -> Counter[str]:
    return Counter(_PLACEHOLDER.findall(text))


def _message_keys(path: Path) -> set[tuple[str, str, bool]]:
    root = ET.parse(path).getroot()
    return {
        (
            context.findtext("name") or "",
            message.findtext("source") or "",
            message.attrib.get("numerus") == "yes",
        )
        for context in root.findall("context")
        for message in context.findall("message")
    }


def _assert_translations_are_complete(path: Path) -> None:
    root = ET.parse(path).getroot()
    messages = list(root.iter("message"))
    assert messages
    for message in messages:
        source = message.findtext("source") or ""
        translation = message.find("translation")
        assert translation is not None
        if message.attrib.get("numerus") == "yes":
            forms = translation.findall("numerusform")
            assert forms and all(form.text for form in forms), source
            translated = [form.text or "" for form in forms]
        else:
            assert (translation.text or "").strip(), source
            translated = [translation.text or ""]
        for form in translated:
            assert _placeholders(source) == _placeholders(form), source


def test_catalogues_match_lupdate_extraction(tmp_path: Path) -> None:
    lupdate = shutil.which("pyside6-lupdate")
    if lupdate is None:
        pytest.skip("pyside6-lupdate is not on PATH")
    generated = tuple(
        tmp_path / name for name in ("matteloop_en.ts", "matteloop_de.ts")
    )
    subprocess.run(
        [
            lupdate,
            "-extensions",
            "py",
            "-no-obsolete",
            "src/matteloop",
            "-ts",
            *(str(path) for path in generated),
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
    )
    for filename, extracted in zip(
        ("matteloop_en.ts", "matteloop_de.ts"), generated
    ):
        checked = REPOSITORY_ROOT / "resources" / filename
        assert _message_keys(extracted) == _message_keys(checked)
        _assert_translations_are_complete(checked)

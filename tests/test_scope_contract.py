from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CURRENT_CONTRACT_FILES = (
    REPO_ROOT / "docs/designs/rembggui-desktop-app.md",
    REPO_ROOT / "docs/superpowers/plans/2026-08-28-rembggui-implementation.md",
    REPO_ROOT / ".superpowers/sdd/2026-08-28-rembggui-implementation/task-9-report.md",
    REPO_ROOT
    / ".superpowers/sdd/2026-08-28-rembggui-implementation/scope-removal-report.md",
)
FUTURE_ENHANCEMENTS = REPO_ROOT / "docs/future-enhancements.md"


def test_current_spec_plan_and_agent_context_have_no_retired_remote_scope() -> None:
    retired_id = "without" + "bg"
    forbidden = (
        retired_id,
        "cloud adapter",
        "cloud token",
        "cloud model",
        "cloud flow",
        "session-only token",
        "session-only key",
        "per-job consent",
        "frames leave",
        "upload limit",
        "max_upload_bytes",
        "cloud_" + retired_id,
    )

    findings = []
    for path in CURRENT_CONTRACT_FILES:
        text = path.read_text(encoding="utf-8").casefold()
        for term in forbidden:
            if term.casefold() in text:
                findings.append(f"{path.relative_to(REPO_ROOT)}: {term}")

    assert findings == []


def test_binding_docs_do_not_require_a_public_remote() -> None:
    forbidden = (
        "public github repository",
        "public github remote",
        "public repo hygiene",
    )

    findings = []
    for path in CURRENT_CONTRACT_FILES[:2]:
        text = path.read_text(encoding="utf-8").casefold()
        for term in forbidden:
            if term in text:
                findings.append(f"{path.relative_to(REPO_ROOT)}: {term}")

    assert findings == []


def test_v1_has_exactly_15_local_models_without_active_sam_capability() -> None:
    active_v1_files = CURRENT_CONTRACT_FILES[:2]
    forbidden = ("sam preview", "sam_prompt", "sam_preview", "16-model")

    findings = []
    for path in active_v1_files:
        text = path.read_text(encoding="utf-8").casefold()
        for term in forbidden:
            if term in text:
                findings.append(f"{path.relative_to(REPO_ROOT)}: {term}")
        for line in text.splitlines():
            if (
                re.search(r"\bsam\b", line)
                and "deferred" not in line
                and "historical" not in line
            ):
                findings.append(f"{path.relative_to(REPO_ROOT)}: active SAM reference")

    assert findings == []
    assert FUTURE_ENHANCEMENTS.is_file()
    future = FUTURE_ENHANCEMENTS.read_text(encoding="utf-8").casefold()
    assert "exploration only" in future
    assert "outside v1" in future

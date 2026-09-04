"""RenderWorker's ordering of artifact publication against cancellation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from matteloop.core.state import CancelAcknowledged, JobKind, RenderSucceeded
from matteloop.jobs.context import JobContext
from matteloop.ui.render_worker import RenderWorker
from tests.jobs.render_support import job, request


@dataclass
class _FakeArtifact:
    output_path: Path


class _CancelsBeforeReturningRuntime:
    """A render runtime that cancels the job after encoding, before commit --
    the window ``RenderFailed``/``CancelAcknowledged`` exists for.
    """

    def __init__(self, context: JobContext, output_path: Path) -> None:
        self._context = context
        self._output_path = output_path

    def render(self, request: object, context: JobContext) -> _FakeArtifact:
        context.request_cancel()
        return _FakeArtifact(self._output_path)


class _SucceedsRuntime:
    """An uncancelled render runtime, for the happy path this must not break."""

    def __init__(self, output_path: Path) -> None:
        self._output_path = output_path

    def render(self, request: object, context: JobContext) -> _FakeArtifact:
        return _FakeArtifact(self._output_path)


def test_an_uncancelled_render_still_publishes_its_artifact(tmp_path) -> None:
    context = job(tmp_path, "job-1", JobKind.RENDER)
    runtime = _SucceedsRuntime(tmp_path / "output.webp")
    worker = RenderWorker(
        "job-1", "source-1", "request-1", request(tmp_path), runtime, context
    )
    artifacts_published: list[object] = []
    notifications: list[object] = []
    worker.artifact_ready.connect(artifacts_published.append)
    worker.notification.connect(notifications.append)

    worker.run()

    assert len(artifacts_published) == 1
    assert any(isinstance(event, RenderSucceeded) for event in notifications)


def test_a_render_cancelled_after_encode_never_publishes_an_artifact(
    tmp_path,
) -> None:
    """``artifact_ready`` opens a cut session in the Transform stage (task
    C-adjacent wiring). Emitting it before the cancellation checkpoint lets a
    render cancelled between encode and commit open a session for an
    artifact whose ``RenderSucceeded`` never arrives -- the UI loops an
    unpublished, uncommitted result.
    """
    context = job(tmp_path, "job-1", JobKind.RENDER)
    runtime = _CancelsBeforeReturningRuntime(context, tmp_path / "output.webp")
    worker = RenderWorker(
        "job-1", "source-1", "request-1", request(tmp_path), runtime, context
    )
    artifacts_published: list[object] = []
    notifications: list[object] = []
    worker.artifact_ready.connect(artifacts_published.append)
    worker.notification.connect(notifications.append)

    worker.run()

    assert artifacts_published == [], (
        "a cancelled render must not publish its artifact to the Transform "
        "stage"
    )
    assert any(isinstance(event, CancelAcknowledged) for event in notifications)
    assert not any(isinstance(event, RenderSucceeded) for event in notifications)

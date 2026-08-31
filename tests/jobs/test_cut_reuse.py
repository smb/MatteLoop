from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from matteloop.core.state import JobKind
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.workspace import CutWorkspace, list_workspaces
from matteloop.jobs.workspace_names import readable_workspace_name
from tests.jobs.render_support import (
    FakeEncoder,
    FakeSegmenter,
    FakeSource,
    job,
    render_service,
    request,
)


def test_exact_key_render_reuses_promoted_cuts_without_segmentation(tmp_path) -> None:
    render_request = request(tmp_path)
    workspace = FilesystemWorkspacePort()
    render_service(workspace=workspace).render(
        render_request, job(tmp_path, "seed-exact-reuse", JobKind.RENDER)
    )

    class HashOnlySource(FakeSource):
        def probe(self, path: Path, context):
            del path, context
            raise AssertionError("exact-key reuse probed the source")

        def decode(self, *args, **kwargs):
            raise AssertionError("exact-key reuse decoded the source")

    class ExplodingSegmenter(FakeSegmenter):
        def segment(self, frame, request):
            del frame, request
            raise AssertionError("exact-key reuse segmented a frame")

    source = HashOnlySource()
    artifact = render_service(
        source=source,
        segmenter=ExplodingSegmenter(),
        workspace=workspace,
        encoder=FakeEncoder(),
    ).render(render_request, job(tmp_path, "exact-reuse", JobKind.RENDER))

    assert artifact.output_path == render_request.output.path
    assert source.hash_calls == 1


def test_new_cut_sets_use_readable_name_with_key_suffix(tmp_path) -> None:
    render_request = request(tmp_path)
    artifact = render_service().render(
        render_request, job(tmp_path, "readable-name", JobKind.RENDER)
    )

    key = artifact.cut_workspace.cache_key
    expected = readable_workspace_name(render_request.source, key)
    assert artifact.cut_workspace.path.name == expected
    assert expected.endswith(f"-{key[:8]}")


def test_sources_with_same_stem_get_distinct_cut_set_names() -> None:
    source_a = Path("/videos/clip.mp4")
    source_b = Path("/archive/clip.mp4")
    name_a = readable_workspace_name(source_a, "a" * 64)
    name_b = readable_workspace_name(source_b, "b" * 64)

    assert name_a == "clip-aaaaaaaa"
    assert name_b == "clip-bbbbbbbb"
    assert name_a != name_b


def test_readable_name_collision_keeps_both_full_keys_distinct(tmp_path) -> None:
    workspace = FilesystemWorkspacePort()
    first = workspace.create_staging(
        tmp_path, "a" * 64, "first", "clip-aaaaaaaa"
    )
    first.path.rename(first.cuts_root / "clip-aaaaaaaa")
    second = workspace.create_staging(
        tmp_path, "a" * 63 + "b", "second", "clip-aaaaaaaa"
    )

    assert second.directory_name == f"clip-aaaaaaaa-{'a' * 63}b"


def test_old_bare_key_directories_are_found_and_reused(tmp_path) -> None:
    render_request = request(tmp_path)
    original = render_service().render(
        render_request, job(tmp_path, "seed-legacy-name", JobKind.RENDER)
    )
    key = original.cut_workspace.cache_key
    legacy_path = original.cut_workspace.cuts_root / key
    original.cut_workspace.path.rename(legacy_path)

    opened = CutWorkspace.open(tmp_path, key)
    listing = list_workspaces(tmp_path)

    assert opened.path == legacy_path
    assert len(listing) == 1
    assert listing[0].workspace.path == legacy_path


def test_regenerate_keeps_previous_promoted_set_until_replacement_promotes(
    tmp_path,
) -> None:
    render_request = request(tmp_path)
    original = render_service().render(
        render_request, job(tmp_path, "seed-regeneration", JobKind.RENDER)
    )

    class ObservingWorkspace(FilesystemWorkspacePort):
        previous_frame: bytes | None = None

        def promote_render(self, workspace, manifest, scratch_directory, context):
            previous = self.open_promoted(
                workspace.output_directory, workspace.cache_key
            )
            self.previous_frame = previous.read_promoted_cut(0).tobytes()
            assert previous.path.is_dir()
            assert self.validate(previous).cache_key == workspace.cache_key
            return super().promote_render(
                workspace, manifest, scratch_directory, context
            )

    observing = ObservingWorkspace()
    result = render_service(workspace=observing).render(
        replace(render_request, regenerate=True),
        job(tmp_path, "regenerate", JobKind.RENDER),
    )

    assert observing.previous_frame == original.cut_workspace.read_promoted_cut(
        0
    ).tobytes()
    assert result.cut_workspace.path == original.cut_workspace.path
    assert len(list_workspaces(tmp_path)) == 1

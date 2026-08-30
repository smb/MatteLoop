from __future__ import annotations

from typing import TYPE_CHECKING

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory

__all__ = (
    "_ATTACHED_BOUND_DIRECTORY_CLOSES",
    "_acquire_local_advisory_lock",
    "_advisory_locks",
    "_advisory_locks_guard",
    "_attach_close_owner",
    "_deferred_bound_directory_closes",
    "_deferred_bound_directory_closes_guard",
    "_drain_attached_bound_directory_closes",
    "_drain_deferred_bound_directory_closes",
    "_forget_deferred_bound_directory_close",
    "_not_cancelled",
    "_pending_deferred_bound_directory_closes",
    "_promotion_lock",
    "_promotion_locks",
    "_promotion_locks_guard",
    "_raise_if_cancelled",
    "_release_local_advisory_lock",
    "_retain_deferred_bound_directory_close",
    "transfer_deferred_bound_directory_closes",
)

_promotion_locks_guard = Lock()
_promotion_locks: dict[str, RLock] = {}
_advisory_locks_guard = Lock()
_advisory_locks: dict[str, Lock] = {}
_deferred_bound_directory_closes_guard = RLock()
_deferred_bound_directory_closes: list[_BoundDirectory] = []
_ATTACHED_BOUND_DIRECTORY_CLOSES = "_rembggui_bound_directory_close_owners"

def _acquire_local_advisory_lock(key: str) -> Lock | None:
    with _advisory_locks_guard:
        lock = _advisory_locks.setdefault(key, Lock())
        if not lock.acquire(blocking=False):
            return None
        return lock


def _release_local_advisory_lock(key: str, lock: Lock) -> None:
    with _advisory_locks_guard:
        lock.release()
        if _advisory_locks.get(key) is lock:
            del _advisory_locks[key]


def _attach_close_owner(primary: BaseException, owner: Any) -> None:
    attached = list(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
    if owner not in attached:
        attached.append(owner)
        setattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(attached))


def _promotion_lock(key: str) -> RLock:
    with _promotion_locks_guard:
        return _promotion_locks.setdefault(key, RLock())


def _retain_deferred_bound_directory_close(
    owner: _BoundDirectory, primary: BaseException
) -> None:
    with _deferred_bound_directory_closes_guard:
        if owner in _deferred_bound_directory_closes:
            return
        if len(_deferred_bound_directory_closes) < max(
            0, MAX_DEFERRED_BOUND_DIRECTORY_CLOSES
        ):
            _deferred_bound_directory_closes.append(owner)
            return
        attached = list(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        if owner not in attached:
            attached.append(owner)
            setattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(attached))
        primary.add_note(
            "deferred-close registry capacity was exhausted; "
            "the retry owner remains attached to this primary error"
        )


def _forget_deferred_bound_directory_close(owner: _BoundDirectory) -> None:
    with _deferred_bound_directory_closes_guard:
        try:
            _deferred_bound_directory_closes.remove(owner)
        except ValueError:
            pass


def _pending_deferred_bound_directory_closes() -> int:
    with _deferred_bound_directory_closes_guard:
        return len(_deferred_bound_directory_closes)


def _drain_deferred_bound_directory_closes() -> int:
    closed = 0
    failures: list[AppError] = []
    with _deferred_bound_directory_closes_guard:
        for owner in tuple(_deferred_bound_directory_closes):
            try:
                owner.close()
            except AppError as error:
                failures.append(error)
            else:
                closed += 1
    if failures:
        failure = _unsafe_error(
            f"{len(failures)} deferred bound-directory close owner(s) remain"
        )
        for close_failure in failures:
            failure.add_note(f"additional deferred close failure: {close_failure}")
        raise failure
    return closed


def _drain_attached_bound_directory_closes(primary: BaseException) -> int:
    closed = 0
    failures: list[tuple[_BoundDirectory, AppError]] = []
    with _deferred_bound_directory_closes_guard:
        owners = tuple(getattr(primary, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        for owner in owners:
            try:
                owner.close()
            except AppError as error:
                failures.append((owner, error))
            else:
                closed += 1
        setattr(
            primary,
            _ATTACHED_BOUND_DIRECTORY_CLOSES,
            tuple(owner for owner, _error in failures),
        )
    if failures:
        failure = _unsafe_error(
            f"{len(failures)} primary-attached close owner(s) remain"
        )
        for _owner, close_failure in failures:
            failure.add_note(f"additional attached close failure: {close_failure}")
        raise failure
    return closed


def transfer_deferred_bound_directory_closes(
    source: BaseException, target: BaseException
) -> None:
    """Transfer retry owners when an internal boundary error is translated."""
    with _deferred_bound_directory_closes_guard:
        owners = tuple(getattr(source, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        if not owners:
            return
        existing = list(getattr(target, _ATTACHED_BOUND_DIRECTORY_CLOSES, ()))
        for owner in owners:
            if owner not in existing:
                existing.append(owner)
        setattr(target, _ATTACHED_BOUND_DIRECTORY_CLOSES, tuple(existing))
        setattr(source, _ATTACHED_BOUND_DIRECTORY_CLOSES, ())


def _raise_if_cancelled(cancelled: CancellationCheck) -> None:
    if cancelled():
        raise AppError(
            ErrorCode.JOB_CANCELLED,
            "cut-snapshot",
            "error.job.cancelled",
            "rebuild snapshot was cancelled",
            "retry-job",
        )


def _not_cancelled() -> bool:
    return False

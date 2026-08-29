"""Weak lifetime accounting for app-owned full-resolution RGBA objects."""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image


@dataclass(slots=True, weakref_slot=True)
class RgbaOwnershipHandle[OwnerT]:
    """Weak-referenceable lifetime handle for owners that do not support weakrefs."""

    value: OwnerT


class RgbaOwnershipTracker:
    """Measure distinct live app-side RGBA owners without retaining them.

    Codec-native buffers behind Python objects are opaque and intentionally not
    included. Non-weak-referenceable Python owners must stay inside the returned
    :class:`RgbaOwnershipHandle` for their complete app-side lifetime.
    """

    __slots__ = ("_owners", "_peak", "_size", "__weakref__")

    def __init__(self, size: tuple[int, int]) -> None:
        width, height = size
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
            or width <= 0
            or height <= 0
        ):
            raise ValueError("tracked RGBA size must contain positive integers")
        self._size = size
        self._owners: dict[int, weakref.ReferenceType[Any]] = {}
        self._peak = 0

    @property
    def current(self) -> int:
        self._discard_dead()
        return len(self._owners)

    @property
    def peak(self) -> int:
        return self._peak

    def register[OwnerT](
        self,
        owner: OwnerT,
        *,
        known_full_resolution_rgba: bool = False,
    ) -> OwnerT:
        """Register one owner by identity and return it unchanged."""
        if not known_full_resolution_rgba and not self._is_full_resolution_rgba(owner):
            return owner

        identity = id(owner)
        existing = self._owners.get(identity)
        if existing is not None:
            target = existing()
            if target is owner:
                return owner
            if target is None:
                self._owners.pop(identity, None)
            else:  # pragma: no cover - live Python objects cannot share an id
                raise RuntimeError("RGBA ownership identity collision")

        tracker_ref = weakref.ref(self)

        def discard(
            reference: weakref.ReferenceType[Any],
            *,
            owner_id: int = identity,
            tracker_reference: weakref.ReferenceType[RgbaOwnershipTracker] = (
                tracker_ref
            ),
        ) -> None:
            tracker = tracker_reference()
            if tracker is not None and tracker._owners.get(owner_id) is reference:
                tracker._owners.pop(owner_id, None)

        try:
            reference = weakref.ref(owner, discard)
        except TypeError as error:
            raise TypeError(
                "RGBA owner does not support weak references; use track_nonweak()"
            ) from error
        self._owners[identity] = reference
        self._peak = max(self._peak, len(self._owners))
        return owner

    def track_nonweak[OwnerT](self, owner: OwnerT) -> RgbaOwnershipHandle[OwnerT]:
        """Wrap and register a non-weak-referenceable RGBA owner."""
        handle = RgbaOwnershipHandle(owner)
        self.register(handle, known_full_resolution_rgba=True)
        return handle

    def _discard_dead(self) -> None:
        for identity, reference in tuple(self._owners.items()):
            if reference() is None:
                self._owners.pop(identity, None)

    def _is_full_resolution_rgba(self, owner: object) -> bool:
        width, height = self._size
        if isinstance(owner, Image.Image):
            return owner.mode == "RGBA" and owner.size == self._size
        if isinstance(owner, np.ndarray):
            return (
                owner.dtype == np.uint8
                and owner.shape == (height, width, 4)
                and owner.nbytes == width * height * 4
            )
        return False

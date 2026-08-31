"""Editable cut-workspace package with the historical module public surface.

The implementation is split by responsibility so each source module remains
reviewable: manifest models, cut operations, snapshots, scans, bound
filesystem access, publication, locking, recovery, platform probes, manifest
I/O, and low-level helpers each have a separate seam.  Symbols are re-exported
here, and the small compatibility bridge keeps existing callers that patch the
former monolithic module working.
"""

from __future__ import annotations

import sys
from types import ModuleType

from . import (
    _common,
    _cut_ops,
    _errors,
    _filesystem,
    _fs_helpers,
    _locking,
    _manifest,
    _manifest_io,
    _manifest_validation,
    _models,
    _platform,
    _publication,
    _recovery,
    _runtime_helpers,
    _scan,
    _snapshot_ops,
    _tree_helpers,
)

_IMPLEMENTATION_MODULES = (
    _common,
    _manifest,
    _models,
    _cut_ops,
    _snapshot_ops,
    _scan,
    _filesystem,
    _publication,
    _locking,
    _recovery,
    _platform,
    _manifest_io,
    _tree_helpers,
    _manifest_validation,
    _fs_helpers,
    _runtime_helpers,
    _errors,
)

_COMPAT_SYMBOLS: dict[str, object] = {}
for _implementation_module in _IMPLEMENTATION_MODULES:
    for _name, _value in vars(_implementation_module).items():
        if not _name.startswith("__"):
            _COMPAT_SYMBOLS.setdefault(_name, _value)

# Functions moved out of workspace.py retain their own module globals.  Keep
# legacy monkeypatching of the package namespace behaviorally compatible by
# reflecting replacements into every implementation module that owns a name.
for _implementation_module in _IMPLEMENTATION_MODULES:
    for _name, _value in _COMPAT_SYMBOLS.items():
        _implementation_module.__dict__.setdefault(_name, _value)

globals().update(_COMPAT_SYMBOLS)


class _WorkspacePackage(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name in _COMPAT_SYMBOLS:
            for implementation_module in _IMPLEMENTATION_MODULES:
                if name in implementation_module.__dict__:
                    implementation_module.__dict__[name] = value


_package = sys.modules[__name__]
_package.__class__ = _WorkspacePackage
__all__ = tuple(sorted(_COMPAT_SYMBOLS))

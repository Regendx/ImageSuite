from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

from imagesuite.diagnostics import diagnostics_report


def test_lightweight_diagnostics_does_not_import_torch(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "torch", "lightweight diagnostics imported PyTorch"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    report = diagnostics_report(include_ai=False)

    assert "PyTorch/AI: not checked" in report


def test_full_diagnostics_uses_loaded_torch_without_reimporting(monkeypatch):
    fake_torch = SimpleNamespace(
        __version__="test-version",
        cuda=SimpleNamespace(is_available=lambda: False),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    report = diagnostics_report(include_ai=True)

    assert "PyTorch: test-version" in report
    assert "CUDA available: False" in report

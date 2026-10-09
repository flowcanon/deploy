"""Tests for flow_deploy/__init__.py — version resolution."""

import importlib
import importlib.metadata

import flow_deploy


def test_version_falls_back_when_package_metadata_is_missing(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert importlib.reload(flow_deploy).__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(flow_deploy)

"""Tests for leafhub_sdk.probe — detection layer."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from leafhub_sdk.probe import ProbeResult, detect


# ── ProbeResult unit tests ───────────────────────────────────────────────────


class TestProbeResult:
    def test_defaults_not_ready(self):
        r = ProbeResult()
        assert not r.ready
        assert not r.cli_available
        assert not r.can_link
        assert r.manage_url == "http://127.0.0.1:8765"
        assert r.project_name is None

    def test_ready_with_valid_dotfile(self):
        r = ProbeResult(dotfile_data={"token": "lh-proj-abc123", "project": "myapp"})
        assert r.ready
        assert r.project_name == "myapp"

    def test_not_ready_without_token(self):
        r = ProbeResult(dotfile_data={"project": "myapp"})
        assert not r.ready

    def test_not_ready_with_empty_token(self):
        r = ProbeResult(dotfile_data={"token": "", "project": "myapp"})
        assert not r.ready

    def test_cli_available(self):
        r = ProbeResult(cli_path="/usr/local/bin/leafhub")
        assert r.cli_available
        assert r.can_link

    def test_server_running_provides_url(self):
        r = ProbeResult(server_running=True, server_url="http://127.0.0.1:9999")
        assert r.can_link
        assert r.manage_url == "http://127.0.0.1:9999"

    def test_sdk_importable_can_link(self):
        r = ProbeResult(sdk_importable=True)
        assert r.can_link
        assert not r.ready

    def test_open_sdk_raises_when_not_ready(self):
        r = ProbeResult()
        with pytest.raises(RuntimeError, match="not linked"):
            r.open_sdk()

    def test_open_sdk_raises_when_sdk_not_importable(self):
        r = ProbeResult(
            dotfile_data={"token": "lh-proj-test123"},
            sdk_importable=False,
        )
        with pytest.raises(ImportError, match="not installed"):
            r.open_sdk()


# ── detect() integration tests ───────────────────────────────────────────────


class TestDetect:
    def test_detect_no_dotfile(self, tmp_path: Path):
        result = detect(project_dir=tmp_path, timeout=0.01)
        assert not result.ready
        assert result.dotfile_path is None
        assert result.dotfile_data is None

    def test_detect_finds_dotfile(self, tmp_path: Path):
        dotfile = tmp_path / ".leafhub"
        data = {"version": 1, "project": "test-proj", "token": "lh-proj-abc123"}
        dotfile.write_text(json.dumps(data), encoding="utf-8")

        result = detect(project_dir=tmp_path, timeout=0.01)
        assert result.ready
        assert result.dotfile_path == dotfile
        assert result.dotfile_data == data
        assert result.project_name == "test-proj"

    def test_detect_walks_up_directory_tree(self, tmp_path: Path):
        # Put .leafhub in parent, detect from child
        dotfile = tmp_path / ".leafhub"
        data = {"version": 1, "project": "parent", "token": "lh-proj-xyz789"}
        dotfile.write_text(json.dumps(data), encoding="utf-8")

        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)

        result = detect(project_dir=child, timeout=0.01)
        assert result.ready
        assert result.dotfile_path == dotfile
        assert result.project_name == "parent"

    def test_detect_stops_at_first_dotfile(self, tmp_path: Path):
        # Parent has valid .leafhub, child has invalid .leafhub
        parent_dotfile = tmp_path / ".leafhub"
        parent_dotfile.write_text(
            json.dumps({"token": "lh-proj-parent123"}), encoding="utf-8"
        )

        child = tmp_path / "child"
        child.mkdir()
        child_dotfile = child / ".leafhub"
        child_dotfile.write_text("not json", encoding="utf-8")

        result = detect(project_dir=child, timeout=0.01)
        # Should stop at child's invalid .leafhub, NOT walk up to parent
        assert not result.ready
        assert result.dotfile_path is None

    def test_detect_invalid_json(self, tmp_path: Path):
        dotfile = tmp_path / ".leafhub"
        dotfile.write_text("{bad json", encoding="utf-8")

        result = detect(project_dir=tmp_path, timeout=0.01)
        assert not result.ready
        assert result.dotfile_data is None

    def test_detect_non_dict_json(self, tmp_path: Path):
        dotfile = tmp_path / ".leafhub"
        dotfile.write_text('"just a string"', encoding="utf-8")

        result = detect(project_dir=tmp_path, timeout=0.01)
        assert not result.ready
        assert result.dotfile_data is None

    def test_detect_server_not_running(self, tmp_path: Path):
        # Use a port that is almost certainly not listening
        result = detect(project_dir=tmp_path, port=19999, timeout=0.01)
        assert not result.server_running
        assert result.server_url is None

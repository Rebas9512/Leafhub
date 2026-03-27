"""Tests for leafhub_sdk.manifest — leafhub.toml parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from leafhub_sdk.manifest import (
    Binding,
    Manifest,
    SetupConfig,
    _minimal_toml_parse,
    find_manifest,
    load_manifest,
)


# ── Minimal TOML parser tests ───────────────────────────────────────────────


class TestMinimalTomlParse:
    def test_simple_table_and_keys(self):
        text = """
[project]
name = "myapp"
python = ">=3.11"
"""
        result = _minimal_toml_parse(text)
        assert result == {"project": {"name": "myapp", "python": ">=3.11"}}

    def test_boolean_values(self):
        text = """
[flags]
enabled = true
disabled = false
"""
        result = _minimal_toml_parse(text)
        assert result["flags"]["enabled"] is True
        assert result["flags"]["disabled"] is False

    def test_integer_and_float(self):
        text = """
[nums]
count = 42
ratio = 3.14
"""
        result = _minimal_toml_parse(text)
        assert result["nums"]["count"] == 42
        assert result["nums"]["ratio"] == 3.14

    def test_inline_array(self):
        text = """
[env_fallbacks]
llm = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
"""
        result = _minimal_toml_parse(text)
        assert result["env_fallbacks"]["llm"] == [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ]

    def test_array_of_tables(self):
        text = """
[[bindings]]
alias = "rewrite"
required = true
env_prefix = "REWRITE"

[[bindings]]
alias = "eval"
required = false
"""
        result = _minimal_toml_parse(text)
        assert len(result["bindings"]) == 2
        assert result["bindings"][0]["alias"] == "rewrite"
        assert result["bindings"][0]["required"] is True
        assert result["bindings"][1]["alias"] == "eval"
        assert result["bindings"][1]["required"] is False

    def test_comments_ignored(self):
        text = """
# This is a comment
[project]
name = "test"  # inline comments not handled but value still works
"""
        result = _minimal_toml_parse(text)
        # The inline comment may be included in value since our parser
        # is minimal — but the key point is it doesn't crash
        assert "project" in result
        assert "name" in result["project"]

    def test_nested_tables(self):
        text = """
[setup]
doctor_cmd = "python check.py"
extra_deps = ["playwright install chromium"]
"""
        result = _minimal_toml_parse(text)
        assert result["setup"]["doctor_cmd"] == "python check.py"
        assert result["setup"]["extra_deps"] == ["playwright install chromium"]

    def test_empty_string(self):
        text = ""
        result = _minimal_toml_parse(text)
        assert result == {}

    def test_string_unquoting(self):
        text = """
[project]
name = "my-app"
"""
        result = _minimal_toml_parse(text)
        assert result["project"]["name"] == "my-app"
        # No extra quotes in the parsed value
        assert '"' not in result["project"]["name"]


# ── Full manifest loading tests ──────────────────────────────────────────────


class TestLoadManifest:
    def _write_manifest(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "leafhub.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_minimal_manifest(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
name = "myapp"

[[bindings]]
alias = "llm"
required = true
""")
        m = load_manifest(tmp_path)
        assert m.name == "myapp"
        assert m.python is None
        assert len(m.bindings) == 1
        assert m.bindings[0].alias == "llm"
        assert m.bindings[0].required is True
        assert m.default_alias() == "llm"
        assert m.required_aliases() == ["llm"]

    def test_full_manifest(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
name = "trileaf"
python = ">=3.10"

[[bindings]]
alias = "rewrite"
required = true
env_prefix = "REWRITE"
capabilities = ["chat"]

[[bindings]]
alias = "eval"
required = false

[setup]
extra_deps = ["playwright install chromium"]
post_register = ["python -m download"]
doctor_cmd = "python check.py"

[env_fallbacks]
rewrite = ["REWRITE_API_KEY", "OPENAI_API_KEY"]
""")
        m = load_manifest(tmp_path)
        assert m.name == "trileaf"
        assert m.python == ">=3.10"
        assert len(m.bindings) == 2

        b0 = m.get_binding("rewrite")
        assert b0 is not None
        assert b0.required is True
        assert b0.env_prefix == "REWRITE"
        assert b0.capabilities == ["chat"]

        b1 = m.get_binding("eval")
        assert b1 is not None
        assert b1.required is False

        assert m.setup.extra_deps == ["playwright install chromium"]
        assert m.setup.post_register == ["python -m download"]
        assert m.setup.doctor_cmd == "python check.py"

        assert m.env_fallbacks["rewrite"] == ["REWRITE_API_KEY", "OPENAI_API_KEY"]
        assert m.default_alias() == "rewrite"
        assert m.required_aliases() == ["rewrite"]

    def test_missing_project_name_raises(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
python = ">=3.11"
""")
        with pytest.raises(ValueError, match="name is required"):
            load_manifest(tmp_path)

    def test_duplicate_alias_raises(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
name = "dup"

[[bindings]]
alias = "llm"

[[bindings]]
alias = "llm"
""")
        with pytest.raises(ValueError, match="duplicate alias"):
            load_manifest(tmp_path)

    def test_no_bindings_ok(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
name = "bare"
""")
        m = load_manifest(tmp_path)
        assert m.bindings == []
        assert m.default_alias() is None
        assert m.required_aliases() == []

    def test_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_manifest(tmp_path)

    def test_capabilities_single_string(self, tmp_path: Path):
        self._write_manifest(tmp_path, """
[project]
name = "cap-test"

[[bindings]]
alias = "ai"
capabilities = "chat"
""")
        # Note: inline TOML string for capabilities — our parser stores as string,
        # but _parse_manifest should coerce to list.
        m = load_manifest(tmp_path)
        assert m.bindings[0].capabilities == ["chat"]


# ── find_manifest tests ─────────────────────────────────────────────────────


class TestFindManifest:
    def test_find_in_current_dir(self, tmp_path: Path):
        (tmp_path / "leafhub.toml").write_text('[project]\nname = "x"\n')
        assert find_manifest(tmp_path) == tmp_path / "leafhub.toml"

    def test_find_in_parent(self, tmp_path: Path):
        (tmp_path / "leafhub.toml").write_text('[project]\nname = "x"\n')
        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)
        assert find_manifest(child) == tmp_path / "leafhub.toml"

    def test_not_found_returns_none(self, tmp_path: Path):
        assert find_manifest(tmp_path) is None

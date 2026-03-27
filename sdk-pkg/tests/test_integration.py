"""
Integration tests — validate the full resolve() flow that consumer
projects (Trileaf, LeafScan) would use after migration.

These tests simulate the EXACT usage patterns from both consumers
without requiring a real LeafHub vault or API keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leafhub_sdk import resolve, CredentialError, load_manifest


class TestTrileafPattern:
    """
    Simulates Trileaf's usage: resolve("rewrite", as_env=True)
    then os.environ.update(result).
    """

    def _setup_trileaf(self, tmp_path: Path) -> None:
        (tmp_path / "leafhub.toml").write_text("""
[project]
name = "trileaf"
python = ">=3.10"

[[bindings]]
alias = "rewrite"
required = true
env_prefix = "REWRITE"
capabilities = ["chat"]

[setup]
post_register = ["trileaf setup --models-only"]
doctor_cmd = "python scripts/check_env.py"

[env_fallbacks]
rewrite = ["REWRITE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
""", encoding="utf-8")

    def test_env_injection_from_openai(self, tmp_path: Path, monkeypatch):
        self._setup_trileaf(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test-key")

        env = resolve("rewrite", project_dir=tmp_path, as_env=True)
        assert isinstance(env, dict)
        assert env["REWRITE_API_KEY"] == "sk-oai-test-key"
        assert env["REWRITE_API_KIND"] == "openai-completions"
        assert "REWRITE_CREDENTIAL_SOURCE" in env

        # Simulate Trileaf's os.environ.update(env)
        monkeypatch.setattr(os, "environ", os.environ.copy())
        os.environ.update(env)
        assert os.environ["REWRITE_API_KEY"] == "sk-oai-test-key"

    def test_env_injection_from_anthropic(self, tmp_path: Path, monkeypatch):
        self._setup_trileaf(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

        env = resolve("rewrite", project_dir=tmp_path, as_env=True)
        assert isinstance(env, dict)
        assert env["REWRITE_API_KEY"] == "sk-ant-test-key"
        assert env["REWRITE_API_KIND"] == "anthropic-messages"

    def test_direct_rewrite_api_key(self, tmp_path: Path, monkeypatch):
        self._setup_trileaf(tmp_path)
        monkeypatch.setenv("REWRITE_API_KEY", "sk-direct-key")

        env = resolve("rewrite", project_dir=tmp_path, as_env=True)
        assert isinstance(env, dict)
        # REWRITE_API_KEY is in the fallback list, so it should be picked up
        assert env["REWRITE_API_KEY"] == "sk-direct-key"

    def test_fallback_priority(self, tmp_path: Path, monkeypatch):
        """REWRITE_API_KEY should be preferred over OPENAI_API_KEY."""
        self._setup_trileaf(tmp_path)
        monkeypatch.setenv("REWRITE_API_KEY", "sk-rewrite-direct")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-fallback")

        env = resolve("rewrite", project_dir=tmp_path, as_env=True)
        assert isinstance(env, dict)
        # First match in env_fallbacks wins
        assert env["REWRITE_API_KEY"] == "sk-rewrite-direct"

    def test_no_credentials_raises(self, tmp_path: Path, monkeypatch):
        self._setup_trileaf(tmp_path)
        for var in [
            "REWRITE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GROQ_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY",
            "TOGETHER_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(CredentialError):
            resolve("rewrite", project_dir=tmp_path, as_env=True)


class TestLeafScanPattern:
    """
    Simulates LeafScan's usage: resolve("llm") → cred.api_key, cred.model
    """

    def _setup_leafscan(self, tmp_path: Path) -> None:
        (tmp_path / "leafhub.toml").write_text("""
[project]
name = "leafscan"
python = ">=3.11"

[[bindings]]
alias = "llm"
required = true
capabilities = ["chat", "vision"]

[setup]
extra_deps = ["playwright install chromium"]

[env_fallbacks]
llm = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
""", encoding="utf-8")

    def test_credential_object_from_anthropic(self, tmp_path: Path, monkeypatch):
        self._setup_leafscan(tmp_path)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-scan")

        cred = resolve("llm", project_dir=tmp_path)
        assert cred.api_key == "sk-ant-scan"
        assert cred.api_format == "anthropic-messages"
        assert cred.model  # has a default model

    def test_default_alias_from_manifest(self, tmp_path: Path, monkeypatch):
        """resolve() without alias should use first binding from manifest."""
        self._setup_leafscan(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-default")

        cred = resolve(project_dir=tmp_path)
        assert cred.api_key == "sk-oai-default"

    def test_alias_override(self, tmp_path: Path, monkeypatch):
        """--alias CLI flag simulated by passing explicit alias."""
        self._setup_leafscan(tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-override")

        # Even though manifest declares "llm", user can pass any alias
        # and it will fall through to common env vars
        cred = resolve("custom-alias", project_dir=tmp_path)
        assert cred.api_key == "sk-oai-override"


class TestManifestValidation:
    """Test manifest loading edge cases relevant to consumer projects."""

    def test_trileaf_manifest_structure(self, tmp_path: Path):
        (tmp_path / "leafhub.toml").write_text("""
[project]
name = "trileaf"
python = ">=3.10"

[[bindings]]
alias = "rewrite"
required = true
env_prefix = "REWRITE"
capabilities = ["chat"]

[setup]
post_register = ["trileaf setup --models-only"]
doctor_cmd = "python scripts/check_env.py"

[env_fallbacks]
rewrite = ["REWRITE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
""", encoding="utf-8")

        m = load_manifest(tmp_path)
        assert m.name == "trileaf"
        assert m.required_aliases() == ["rewrite"]
        b = m.get_binding("rewrite")
        assert b is not None
        assert b.env_prefix == "REWRITE"
        assert m.setup.post_register == ["trileaf setup --models-only"]
        assert m.setup.doctor_cmd == "python scripts/check_env.py"

    def test_leafscan_manifest_structure(self, tmp_path: Path):
        (tmp_path / "leafhub.toml").write_text("""
[project]
name = "leafscan"
python = ">=3.11"

[[bindings]]
alias = "llm"
required = true
capabilities = ["chat", "vision"]

[setup]
extra_deps = ["playwright install chromium"]

[env_fallbacks]
llm = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
""", encoding="utf-8")

        m = load_manifest(tmp_path)
        assert m.name == "leafscan"
        assert m.required_aliases() == ["llm"]
        b = m.get_binding("llm")
        assert b is not None
        assert b.capabilities == ["chat", "vision"]
        assert m.setup.extra_deps == ["playwright install chromium"]


class TestNewProjectPattern:
    """
    Simulate what a brand-new project would do to integrate with LeafHub.
    This validates the "write a leafhub.toml and call resolve()" workflow.
    """

    def test_minimal_new_project(self, tmp_path: Path, monkeypatch):
        (tmp_path / "leafhub.toml").write_text("""
[project]
name = "my-new-project"

[[bindings]]
alias = "ai"
required = true
env_prefix = "AI"

[env_fallbacks]
ai = ["AI_API_KEY", "OPENAI_API_KEY"]
""", encoding="utf-8")

        monkeypatch.setenv("AI_API_KEY", "sk-new-project")

        # This is ALL a new project needs to do:
        env = resolve("ai", project_dir=tmp_path, as_env=True)
        assert isinstance(env, dict)
        assert env["AI_API_KEY"] == "sk-new-project"
        assert env["AI_CREDENTIAL_SOURCE"] == "env-fallback:AI_API_KEY"

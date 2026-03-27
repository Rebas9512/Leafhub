"""Tests for leafhub_sdk.resolve — unified credential resolution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from leafhub_sdk.resolve import (
    CredentialError,
    ResolvedCredential,
    _build_client,
    _to_env_dict,
    _try_common_env_vars,
    _try_env_fallbacks,
    _try_leafhub,
    resolve,
)
from leafhub_sdk.manifest import Binding, Manifest, SetupConfig


# ── ResolvedCredential unit tests ────────────────────────────────────────────


class TestResolvedCredential:
    def test_build_headers_bearer(self):
        cred = ResolvedCredential(
            api_key="sk-test123",
            auth_mode="bearer",
        )
        headers = cred.build_headers()
        assert headers == {"Authorization": "Bearer sk-test123"}

    def test_build_headers_x_api_key(self):
        cred = ResolvedCredential(
            api_key="sk-ant-test",
            auth_mode="x-api-key",
        )
        headers = cred.build_headers()
        assert headers == {"x-api-key": "sk-ant-test"}

    def test_build_headers_none_auth(self):
        cred = ResolvedCredential(api_key="ignored", auth_mode="none")
        headers = cred.build_headers()
        assert headers == {}

    def test_build_headers_custom_auth_header(self):
        cred = ResolvedCredential(
            api_key="my-key",
            auth_mode="x-api-key",
            auth_header="X-Custom-Auth",
        )
        headers = cred.build_headers()
        assert headers == {"X-Custom-Auth": "my-key"}

    def test_build_headers_extra_headers(self):
        cred = ResolvedCredential(
            api_key="sk-test",
            auth_mode="bearer",
            extra_headers={"anthropic-version": "2023-06-01"},
        )
        headers = cred.build_headers()
        assert headers["Authorization"] == "Bearer sk-test"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_build_headers_empty_key_no_auth(self):
        cred = ResolvedCredential(api_key="", auth_mode="bearer")
        headers = cred.build_headers()
        assert headers == {}


# ── _to_env_dict tests ───────────────────────────────────────────────────────


class TestToEnvDict:
    def test_with_prefix(self):
        cred = ResolvedCredential(
            api_key="sk-123",
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            api_format="openai-completions",
            auth_mode="bearer",
            source="leafhub",
        )
        binding = Binding(alias="rewrite", env_prefix="REWRITE")
        result = _to_env_dict(cred, binding)
        assert result["REWRITE_API_KEY"] == "sk-123"
        assert result["REWRITE_BASE_URL"] == "https://api.openai.com/v1"
        assert result["REWRITE_MODEL"] == "gpt-4o"
        assert result["REWRITE_API_KIND"] == "openai-completions"
        assert result["REWRITE_AUTH_MODE"] == "bearer"
        assert result["REWRITE_CREDENTIAL_SOURCE"] == "leafhub"

    def test_without_prefix(self):
        cred = ResolvedCredential(
            api_key="sk-test",
            model="gpt-4o",
            api_format="openai-completions",
            source="env:OPENAI_API_KEY",
        )
        result = _to_env_dict(cred, None)
        assert result["API_KEY"] == "sk-test"
        assert result["MODEL"] == "gpt-4o"
        assert result["CREDENTIAL_SOURCE"] == "env:OPENAI_API_KEY"

    def test_empty_fields_excluded(self):
        cred = ResolvedCredential(
            api_key="sk-test",
            base_url="",
            model="",
            source="leafhub",
        )
        binding = Binding(alias="x", env_prefix="X")
        result = _to_env_dict(cred, binding)
        assert "X_API_KEY" in result
        assert "X_BASE_URL" not in result
        assert "X_MODEL" not in result

    def test_prefix_trailing_underscore_stripped(self):
        cred = ResolvedCredential(api_key="sk", source="test")
        binding = Binding(alias="x", env_prefix="MY_PREFIX_")
        result = _to_env_dict(cred, binding)
        assert "MY_PREFIX_API_KEY" in result
        # Not MY_PREFIX__API_KEY


# ── _try_common_env_vars tests ───────────────────────────────────────────────


class TestTryCommonEnvVars:
    def test_anthropic_key_found(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        cred = _try_common_env_vars()
        assert cred is not None
        assert cred.api_key == "sk-ant-test"
        assert cred.api_format == "anthropic-messages"
        assert cred.source == "env:ANTHROPIC_API_KEY"

    def test_openai_key_found(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        cred = _try_common_env_vars()
        assert cred is not None
        assert cred.api_key == "sk-openai-test"
        assert cred.api_format == "openai-completions"

    def test_no_keys_returns_none(self, monkeypatch):
        # Clear all relevant env vars
        for var in [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
            "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
            "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        assert _try_common_env_vars() is None

    def test_priority_order(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
        monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
        cred = _try_common_env_vars()
        # Anthropic comes first in the list
        assert cred is not None
        assert cred.api_key == "ant-key"

    def test_groq_has_base_url(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        cred = _try_common_env_vars()
        assert cred is not None
        assert cred.base_url == "https://api.groq.com/openai/v1"


# ── _try_env_fallbacks tests ─────────────────────────────────────────────────


class TestTryEnvFallbacks:
    def _make_manifest(self, fallbacks: dict) -> Manifest:
        return Manifest(
            path=Path("/fake/leafhub.toml"),
            name="test",
            env_fallbacks=fallbacks,
        )

    def test_matches_first_available(self, monkeypatch):
        monkeypatch.setenv("MY_CUSTOM_KEY", "custom-val")
        m = self._make_manifest({"llm": ["MY_CUSTOM_KEY", "OPENAI_API_KEY"]})
        cred = _try_env_fallbacks("llm", m)
        assert cred is not None
        assert cred.api_key == "custom-val"
        assert cred.source == "env-fallback:MY_CUSTOM_KEY"

    def test_skips_empty_vars(self, monkeypatch):
        monkeypatch.setenv("FIRST", "")
        monkeypatch.setenv("SECOND", "good-key")
        m = self._make_manifest({"llm": ["FIRST", "SECOND"]})
        cred = _try_env_fallbacks("llm", m)
        assert cred is not None
        assert cred.api_key == "good-key"

    def test_no_matching_alias(self):
        m = self._make_manifest({"other": ["SOME_KEY"]})
        assert _try_env_fallbacks("llm", m) is None

    def test_no_manifest(self):
        assert _try_env_fallbacks("llm", None) is None

    def test_known_env_var_infers_format(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        m = self._make_manifest({"llm": ["ANTHROPIC_API_KEY"]})
        cred = _try_env_fallbacks("llm", m)
        assert cred is not None
        assert cred.api_format == "anthropic-messages"


# ── resolve() integration tests ──────────────────────────────────────────────


class TestResolve:
    def _setup_project(self, tmp_path: Path, manifest_content: str, dotfile: dict | None = None):
        """Write leafhub.toml and optionally .leafhub to tmp_path."""
        (tmp_path / "leafhub.toml").write_text(manifest_content, encoding="utf-8")
        if dotfile:
            (tmp_path / ".leafhub").write_text(
                json.dumps(dotfile), encoding="utf-8"
            )

    def test_resolve_from_env_fallback(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "test"

[[bindings]]
alias = "llm"
required = true
env_prefix = "LLM"

[env_fallbacks]
llm = ["MY_LLM_KEY"]
""")
        monkeypatch.setenv("MY_LLM_KEY", "test-key-123")
        cred = resolve("llm", project_dir=tmp_path)
        assert isinstance(cred, ResolvedCredential)
        assert cred.api_key == "test-key-123"
        assert "env-fallback" in cred.source

    def test_resolve_as_env(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "test"

[[bindings]]
alias = "rewrite"
env_prefix = "REWRITE"

[env_fallbacks]
rewrite = ["OPENAI_API_KEY"]
""")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-env")
        result = resolve("rewrite", project_dir=tmp_path, as_env=True)
        assert isinstance(result, dict)
        assert result["REWRITE_API_KEY"] == "sk-test-env"
        assert "REWRITE_CREDENTIAL_SOURCE" in result

    def test_resolve_default_alias_from_manifest(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "default-test"

[[bindings]]
alias = "primary"
required = true

[env_fallbacks]
primary = ["TEST_KEY"]
""")
        monkeypatch.setenv("TEST_KEY", "pk-123")
        cred = resolve(project_dir=tmp_path)
        assert isinstance(cred, ResolvedCredential)
        assert cred.api_key == "pk-123"

    def test_resolve_no_alias_no_manifest_raises(self, tmp_path: Path, monkeypatch):
        # Clean env to prevent common env var fallback
        for var in [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
            "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
            "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(CredentialError, match="No alias specified"):
            resolve(project_dir=tmp_path)

    def test_resolve_falls_through_to_common_env(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "fallthrough"

[[bindings]]
alias = "llm"
""")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-common")
        cred = resolve("llm", project_dir=tmp_path)
        assert isinstance(cred, ResolvedCredential)
        assert cred.api_key == "sk-from-common"
        assert cred.source == "env:OPENAI_API_KEY"

    def test_resolve_all_paths_fail_raises(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "fail-test"

[[bindings]]
alias = "llm"
env_prefix = "LLM"
""")
        for var in [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
            "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
            "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(CredentialError, match="Could not resolve"):
            resolve("llm", project_dir=tmp_path)

    def test_error_message_includes_env_prefix(self, tmp_path: Path, monkeypatch):
        self._setup_project(tmp_path, """
[project]
name = "msg-test"

[[bindings]]
alias = "rewrite"
env_prefix = "REWRITE"
""")
        for var in [
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
            "MISTRAL_API_KEY", "XAI_API_KEY", "TOGETHER_API_KEY",
            "OPENROUTER_API_KEY", "GEMINI_API_KEY",
        ]:
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(CredentialError, match="REWRITE_API_KEY"):
            resolve("rewrite", project_dir=tmp_path)


# ── _build_client tests (mock imports) ───────────────────────────────────────


class TestBuildClient:
    def test_unknown_format_returns_none(self):
        cred = ResolvedCredential(api_key="k", api_format="unknown-format")
        assert _build_client(cred) is None

    def test_anthropic_import_error(self):
        cred = ResolvedCredential(api_key="k", api_format="anthropic-messages")
        with patch.dict("sys.modules", {"anthropic": None}):
            with pytest.raises(ImportError, match="anthropic"):
                _build_client(cred)

    def test_openai_import_error(self):
        cred = ResolvedCredential(api_key="k", api_format="openai-completions")
        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(ImportError, match="openai"):
                _build_client(cred)


# ── Backward compatibility: resolve without manifest ─────────────────────────


class TestResolveWithoutManifest:
    def test_resolve_with_explicit_alias_and_env(self, tmp_path: Path, monkeypatch):
        """resolve() works even without leafhub.toml when alias is explicit."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-compat")
        cred = resolve("llm", project_dir=tmp_path)
        assert isinstance(cred, ResolvedCredential)
        assert cred.api_key == "sk-ant-compat"
        assert cred.api_format == "anthropic-messages"

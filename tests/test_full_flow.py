"""
LeafHub — Comprehensive integration test suite.

Covers:
  - crypto:   encrypt/decrypt round-trip, wrong key, missing file, version check
  - store:    provider CRUD, project CRUD, binding CRUD, token auth, edge cases
  - sdk:      LeafHub constructor, get_key, get_config, error hierarchy
  - manage:   FastAPI endpoints (health, providers CRUD, projects CRUD, auth)

Isolation:
  - Every test uses a fresh temp directory (never touches ~/.leafhub/)
  - Master key injected via LEAFHUB_MASTER_KEY env var (no keychain)
  - Rate-limiter state reset in auth teardown

Run:
    pytest test/test_full_flow.py -v
    pytest test/test_full_flow.py -v -k "crypto"   # filter by group
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

# Allow running from project root without pip install (src layout)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def hub_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh temp directory acting as ~/.leafhub/, with a pinned master key."""
    hub = tmp_path / "leafhub"
    hub.mkdir(mode=0o700)
    master_key = secrets.token_bytes(32)
    monkeypatch.setenv("LEAFHUB_MASTER_KEY", base64.b64encode(master_key).decode())
    return hub


@pytest.fixture()
def store(hub_dir: Path):
    """Open SyncStore over a fresh DB in hub_dir."""
    from leafhub.core.db import open_db
    from leafhub.core.store import SyncStore

    conn = open_db(hub_dir)
    s = SyncStore(conn)
    yield s
    conn.close()


@pytest.fixture()
def provider(store):
    """A pre-created OpenAI provider."""
    from leafhub.core.crypto import decrypt_providers, encrypt_providers, load_master_key
    from leafhub.core.db import default_hub_dir

    p = store.create_provider(
        label="openai-test",
        provider_type="openai",
        api_format="openai-completions",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        auth_mode="bearer",
    )
    # Write a fake API key to providers.enc
    hub = Path(os.environ.get("LEAFHUB_MASTER_KEY") and _hub_dir_from_store(store))
    return p


def _hub_dir_from_store(store) -> Path:
    """Extract hub_dir from the store's connection path."""
    path = Path(store._conn.execute("PRAGMA database_list").fetchone()[2])
    return path.parent


@pytest.fixture()
def app(hub_dir: Path):
    """FastAPI test app with manage server."""
    from leafhub.manage.server import create_app

    master_key = base64.b64decode(os.environ["LEAFHUB_MASTER_KEY"])
    return create_app(hub_dir=hub_dir, master_key=master_key)


@pytest.fixture()
def client(app):
    """FastAPI TestClient."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Prevent rate-limit state leaking between tests."""
    yield
    try:
        from leafhub.manage.auth import _reset_limiter_for_tests
        _reset_limiter_for_tests()
    except ImportError:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_api_key(hub_dir: Path, provider_id: str, api_key: str) -> None:
    from leafhub.core.crypto import decrypt_providers, encrypt_providers, load_master_key
    master_key = load_master_key(hub_dir)
    ks = decrypt_providers(master_key, hub_dir)
    ks[provider_id] = {"api_key": api_key}
    encrypt_providers(ks, master_key, hub_dir)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Crypto
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrypto:
    def test_round_trip(self, hub_dir):
        """Encrypt then decrypt returns the original dict."""
        from leafhub.core.crypto import decrypt_providers, encrypt_providers, load_master_key
        master_key = load_master_key(hub_dir)
        data = {"prov-1": {"api_key": "sk-abc123"}, "prov-2": {"api_key": ""}}
        encrypt_providers(data, master_key, hub_dir)
        result = decrypt_providers(master_key, hub_dir)
        assert result == data

    def test_missing_file_returns_empty(self, hub_dir):
        """decrypt_providers returns {} when providers.enc does not exist."""
        from leafhub.core.crypto import decrypt_providers, load_master_key
        master_key = load_master_key(hub_dir)
        assert decrypt_providers(master_key, hub_dir) == {}

    def test_wrong_key_raises(self, hub_dir):
        """Decrypting with a wrong master key raises RuntimeError."""
        from leafhub.core.crypto import decrypt_providers, encrypt_providers, load_master_key
        master_key = load_master_key(hub_dir)
        encrypt_providers({"x": {"api_key": "secret"}}, master_key, hub_dir)
        wrong_key = secrets.token_bytes(32)
        with pytest.raises(RuntimeError, match="decrypt"):
            decrypt_providers(wrong_key, hub_dir)

    def test_corrupt_file_raises(self, hub_dir):
        """Corrupt providers.enc raises RuntimeError."""
        from leafhub.core.crypto import decrypt_providers, load_master_key
        enc_path = hub_dir / "providers.enc"
        enc_path.write_text("not valid json")
        master_key = load_master_key(hub_dir)
        with pytest.raises(RuntimeError, match="corrupt"):
            decrypt_providers(master_key, hub_dir)

    def test_unsupported_version_raises(self, hub_dir):
        """providers.enc with version != 1 raises RuntimeError."""
        from leafhub.core.crypto import decrypt_providers, load_master_key
        enc_path = hub_dir / "providers.enc"
        enc_path.write_text(json.dumps({"version": 99}))
        master_key = load_master_key(hub_dir)
        with pytest.raises(RuntimeError, match="unsupported version"):
            decrypt_providers(master_key, hub_dir)

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="chmod 600 not enforced on Windows")
    def test_file_chmod_600(self, hub_dir):
        """providers.enc is written with mode 600."""
        from leafhub.core.crypto import encrypt_providers, load_master_key
        master_key = load_master_key(hub_dir)
        encrypt_providers({"x": {"api_key": "k"}}, master_key, hub_dir)
        mode = (hub_dir / "providers.enc").stat().st_mode & 0o777
        assert mode == 0o600

    def test_different_nonce_each_write(self, hub_dir):
        """Each encrypt call uses a fresh nonce (ciphertexts differ)."""
        from leafhub.core.crypto import encrypt_providers, load_master_key
        master_key = load_master_key(hub_dir)
        data = {"x": {"api_key": "same"}}
        encrypt_providers(data, master_key, hub_dir)
        ct1 = (hub_dir / "providers.enc").read_text()
        encrypt_providers(data, master_key, hub_dir)
        ct2 = (hub_dir / "providers.enc").read_text()
        assert json.loads(ct1)["nonce"] != json.loads(ct2)["nonce"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: SyncStore
# ═══════════════════════════════════════════════════════════════════════════════

class TestProviderCRUD:
    def test_create_and_get(self, store):
        p = store.create_provider(
            label="gpt", provider_type="openai",
            api_format="openai-completions", base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
        )
        assert p.label == "gpt"
        assert p.auth_mode == "bearer"  # inferred from api_format

        fetched = store.get_provider(p.id)
        assert fetched.id == p.id

    def test_list(self, store):
        store.create_provider("p1", "openai", "openai-completions", "https://a.com", "m1")
        store.create_provider("p2", "anthropic", "anthropic-messages", "https://b.com", "m2")
        assert len(store.list_providers()) == 2

    def test_update(self, store):
        p = store.create_provider("gpt", "openai", "openai-completions", "https://a.com", "m")
        updated = store.update_provider(p.id, label="gpt-updated", default_model="gpt-4o-mini")
        assert updated.label == "gpt-updated"
        assert updated.default_model == "gpt-4o-mini"
        assert updated.base_url == "https://a.com"  # unchanged

    def test_update_extra_headers(self, store):
        p = store.create_provider("ant", "anthropic", "anthropic-messages",
                                   "https://api.anthropic.com", "claude-3")
        updated = store.update_provider(p.id, extra_headers={"anthropic-version": "2023-06-01"})
        assert updated.extra_headers == {"anthropic-version": "2023-06-01"}
        # Clear headers
        cleared = store.update_provider(p.id, extra_headers={})
        assert cleared.extra_headers == {}

    def test_delete(self, store):
        p = store.create_provider("tmp", "openai", "openai-completions", "https://a.com", "m")
        store.delete_provider(p.id)
        with pytest.raises(KeyError):
            store.get_provider(p.id)

    def test_duplicate_label_raises(self, store):
        store.create_provider("same", "openai", "openai-completions", "https://a.com", "m")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            store.create_provider("same", "openai", "openai-completions", "https://a.com", "m")

    def test_invalid_api_format_raises(self, store):
        with pytest.raises(ValueError, match="api_format"):
            store.create_provider("x", "openai", "unknown-format", "https://a.com", "m")

    def test_auth_mode_inferred_from_format(self, store):
        ant = store.create_provider("ant", "anthropic", "anthropic-messages",
                                     "https://api.anthropic.com", "claude")
        assert ant.auth_mode == "x-api-key"

        olm = store.create_provider("olm", "ollama", "ollama",
                                     "http://localhost:11434/v1", "llama3")
        assert olm.auth_mode == "none"

    def test_delete_with_binding_raises_fk(self, store):
        """Deleting a provider that has bindings raises FK constraint error."""
        import sqlite3
        p = store.create_provider("prov", "openai", "openai-completions", "https://a.com", "m")
        proj, _ = store.create_project("myapp")
        store.add_binding(proj.id, "chat", p.id)
        with pytest.raises(sqlite3.IntegrityError):
            store.delete_provider(p.id)


class TestProjectCRUD:
    def test_create_returns_raw_token(self, store):
        proj, token = store.create_project("myapp")
        assert token.startswith("lh-proj-")
        assert len(token) == 40  # "lh-proj-" (8) + 32 hex chars
        assert proj.name == "myapp"
        assert proj.is_active

    def test_token_prefix_stored(self, store):
        proj, token = store.create_project("myapp")
        assert proj.token_prefix == token[:12]

    def test_token_not_stored_in_plaintext(self, store):
        """The raw token must not appear in the projects table."""
        proj, token = store.create_project("myapp")
        row = store._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (proj.id,)
        ).fetchone()
        assert token not in dict(row).values()

    def test_authenticate_valid(self, store):
        proj, token = store.create_project("myapp")
        result = store.authenticate_project(token)
        assert result is not None
        assert result.id == proj.id

    def test_authenticate_wrong_token(self, store):
        store.create_project("myapp")
        assert store.authenticate_project("lh-proj-" + "0" * 32) is None

    def test_authenticate_inactive(self, store):
        proj, token = store.create_project("myapp")
        store.deactivate_project(proj.id)
        assert store.authenticate_project(token) is None

    def test_authenticate_after_reactivate(self, store):
        proj, token = store.create_project("myapp")
        store.deactivate_project(proj.id)
        store.activate_project(proj.id)
        assert store.authenticate_project(token) is not None

    def test_rotate_token_invalidates_old(self, store):
        proj, old_token = store.create_project("myapp")
        new_token = store.rotate_token(proj.id)
        assert new_token != old_token
        assert store.authenticate_project(old_token) is None
        assert store.authenticate_project(new_token) is not None

    def test_delete_cascades_bindings(self, store):
        prov = store.create_provider("p", "openai", "openai-completions", "https://a.com", "m")
        proj, _ = store.create_project("myapp")
        store.add_binding(proj.id, "chat", prov.id)
        assert len(store.list_bindings(proj.id)) == 1
        store.delete_project(proj.id)
        # Bindings should be gone (FK cascade)
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM model_bindings WHERE project_id = ?", (proj.id,)
        ).fetchone()[0]
        assert rows == 0

    def test_rename_project(self, store):
        proj, _ = store.create_project("old-name")
        renamed = store.rename_project(proj.id, "new-name")
        assert renamed.name == "new-name"


class TestBindings:
    @pytest.fixture()
    def setup(self, store):
        prov = store.create_provider("p", "openai", "openai-completions", "https://a.com", "m")
        proj, token = store.create_project("myapp")
        return store, prov, proj, token

    def test_add_and_resolve(self, setup):
        store, prov, proj, _ = setup
        store.add_binding(proj.id, "chat", prov.id, model_override="gpt-4o")
        b = store.resolve_binding(proj.id, "chat")
        assert b is not None
        assert b.provider_id == prov.id
        assert b.model_override == "gpt-4o"

    def test_resolve_missing(self, setup):
        store, _, proj, _ = setup
        assert store.resolve_binding(proj.id, "nonexistent") is None

    def test_set_bindings_atomic(self, setup):
        store, prov, proj, _ = setup
        prov2 = store.create_provider("p2", "openai", "openai-completions", "https://b.com", "m")
        store.set_bindings(proj.id, [
            {"alias": "chat", "provider_id": prov.id, "model_override": None},
            {"alias": "embed", "provider_id": prov2.id, "model_override": "text-embed-3-small"},
        ])
        bindings = store.list_bindings(proj.id)
        assert len(bindings) == 2
        aliases = {b.alias for b in bindings}
        assert aliases == {"chat", "embed"}

    def test_set_bindings_replaces_all(self, setup):
        store, prov, proj, _ = setup
        store.add_binding(proj.id, "old", prov.id)
        store.set_bindings(proj.id, [
            {"alias": "new", "provider_id": prov.id, "model_override": None},
        ])
        assert store.resolve_binding(proj.id, "old") is None
        assert store.resolve_binding(proj.id, "new") is not None

    def test_remove_binding(self, setup):
        store, prov, proj, _ = setup
        store.add_binding(proj.id, "chat", prov.id)
        store.remove_binding(proj.id, "chat")
        assert store.resolve_binding(proj.id, "chat") is None

    def test_list_bindings_sorted_by_alias(self, setup):
        store, prov, proj, _ = setup
        store.add_binding(proj.id, "z-alias", prov.id)
        store.add_binding(proj.id, "a-alias", prov.id)
        bindings = store.list_bindings(proj.id)
        assert [b.alias for b in bindings] == ["a-alias", "z-alias"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: SDK (LeafHub)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSDK:
    def _make_hub(self, hub_dir, tmp_path):
        """Create a fully wired LeafHub instance with one provider and project."""
        from leafhub.core.db import open_db
        from leafhub.core.store import SyncStore
        from leafhub.sdk import LeafHub

        conn = open_db(hub_dir)
        store = SyncStore(conn)
        prov = store.create_provider(
            "openai", "openai", "openai-completions",
            "https://api.openai.com/v1", "gpt-4o-mini",
        )
        proj, token = store.create_project("myapp")
        store.add_binding(proj.id, "chat", prov.id)
        conn.close()

        _write_api_key(hub_dir, prov.id, "sk-test-key-123")
        return LeafHub(token=token, hub_dir=hub_dir), token, prov

    def test_get_key(self, hub_dir, tmp_path):
        hub, _, _ = self._make_hub(hub_dir, tmp_path)
        assert hub.get_key("chat") == "sk-test-key-123"

    def test_get_config(self, hub_dir, tmp_path):
        hub, _, prov = self._make_hub(hub_dir, tmp_path)
        cfg = hub.get_config("chat")
        assert cfg.api_key == "sk-test-key-123"
        assert cfg.base_url == "https://api.openai.com/v1"
        assert cfg.model == "gpt-4o-mini"
        assert cfg.api_format == "openai-completions"

    def test_build_headers_bearer(self, hub_dir, tmp_path):
        hub, _, _ = self._make_hub(hub_dir, tmp_path)
        cfg = hub.get_config("chat")
        headers = cfg.build_headers()
        assert headers["Authorization"] == "Bearer sk-test-key-123"

    def test_build_headers_x_api_key(self, hub_dir):
        from leafhub.sdk import ProviderConfig
        cfg = ProviderConfig(
            api_key="ant-key", base_url="https://api.anthropic.com",
            model="claude-3", api_format="anthropic-messages",
            auth_mode="x-api-key", extra_headers={"anthropic-version": "2023-06-01"},
        )
        headers = cfg.build_headers()
        assert headers["x-api-key"] == "ant-key"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers

    def test_build_headers_none_auth(self, hub_dir):
        from leafhub.sdk import ProviderConfig
        cfg = ProviderConfig(
            api_key="", base_url="http://localhost:11434/v1",
            model="llama3", api_format="ollama", auth_mode="none",
        )
        assert cfg.build_headers() == {}

    def test_list_aliases(self, hub_dir, tmp_path):
        hub, _, _ = self._make_hub(hub_dir, tmp_path)
        assert hub.list_aliases() == ["chat"]

    def test_invalid_token_raises(self, hub_dir, tmp_path):
        from leafhub.errors import InvalidTokenError
        from leafhub.sdk import LeafHub
        # Create DB so storage exists
        from leafhub.core.db import open_db
        open_db(hub_dir).close()
        with pytest.raises(InvalidTokenError):
            LeafHub(token="lh-proj-" + "0" * 32, hub_dir=hub_dir)

    def test_storage_not_found_raises(self, tmp_path):
        from leafhub.errors import StorageNotFoundError
        from leafhub.sdk import LeafHub
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(StorageNotFoundError):
            LeafHub(token="lh-proj-" + "0" * 32, hub_dir=empty)

    def test_alias_not_bound_raises(self, hub_dir, tmp_path):
        from leafhub.errors import AliasNotBoundError
        hub, _, _ = self._make_hub(hub_dir, tmp_path)
        with pytest.raises(AliasNotBoundError, match="nope"):
            hub.get_key("nope")

    def test_context_manager(self, hub_dir, tmp_path):
        """LeafHub can be used as a context manager; connection closes on exit."""
        from leafhub.core.db import open_db
        from leafhub.core.store import SyncStore
        from leafhub.sdk import LeafHub

        conn = open_db(hub_dir)
        _, token = SyncStore(conn).create_project("app")
        conn.close()
        _write_api_key(hub_dir, "dummy", "key")  # ensure providers.enc exists

        with LeafHub(token=token, hub_dir=hub_dir) as hub:
            assert hub.list_aliases() == []

    def test_no_api_key_for_auth_required_provider_raises(self, hub_dir):
        """DecryptionError when api_key is absent but auth_mode != none."""
        from leafhub.core.db import open_db
        from leafhub.core.store import SyncStore
        from leafhub.errors import DecryptionError
        from leafhub.sdk import LeafHub

        conn = open_db(hub_dir)
        store = SyncStore(conn)
        prov = store.create_provider("gpt", "openai", "openai-completions",
                                      "https://api.openai.com/v1", "gpt-4o")
        proj, token = store.create_project("app")
        store.add_binding(proj.id, "chat", prov.id)
        conn.close()
        # Don't write any key to providers.enc

        hub = LeafHub(token=token, hub_dir=hub_dir)
        with pytest.raises(DecryptionError):
            hub.get_key("chat")

    def test_model_override_respected(self, hub_dir):
        from leafhub.core.db import open_db
        from leafhub.core.store import SyncStore
        from leafhub.sdk import LeafHub

        conn = open_db(hub_dir)
        store = SyncStore(conn)
        prov = store.create_provider("gpt", "openai", "openai-completions",
                                      "https://api.openai.com/v1", "gpt-4o-mini")
        proj, token = store.create_project("app")
        store.add_binding(proj.id, "fast", prov.id, model_override="gpt-3.5-turbo")
        conn.close()
        _write_api_key(hub_dir, prov.id, "sk-x")

        hub = LeafHub(token=token, hub_dir=hub_dir)
        cfg = hub.get_config("fast")
        assert cfg.model == "gpt-3.5-turbo"  # override wins


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Manage server (FastAPI)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealth:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_admin_status(self, client):
        r = client.get("/admin/status")
        assert r.status_code == 200
        data = r.json()
        assert "providers" in data
        assert "projects" in data


class TestProviderAPI:
    @pytest.fixture(autouse=True)
    def mock_probe(self, monkeypatch):
        """Prevent real HTTP connectivity probes during provider API tests."""
        monkeypatch.setattr(
            "leafhub.manage.providers._probe_provider",
            lambda *a, **kw: (True, "mocked — always connected"),
        )

    def _create(self, client, label="openai", api_key="sk-test") -> dict:
        r = client.post("/admin/providers", json={
            "label": label,
            "provider_type": "openai",
            "api_format": "openai-completions",
            "base_url": "https://api.openai.com/v1",
            "default_model": "gpt-4o-mini",
            "api_key": api_key,
        })
        assert r.status_code == 201, r.text
        return r.json()

    def test_create_and_list(self, client):
        self._create(client)
        r = client.get("/admin/providers")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 1

    def test_create_no_api_key_for_ollama(self, client):
        """Ollama provider with auth_mode=none and no api_key should succeed."""
        r = client.post("/admin/providers", json={
            "label": "ollama-local",
            "provider_type": "ollama",
            "api_format": "ollama",
            "base_url": "http://localhost:11434/v1",
            "default_model": "llama3.2",
            "auth_mode": "none",
        })
        assert r.status_code == 201, r.text

    def test_duplicate_label_returns_409(self, client):
        self._create(client, "same")
        r = client.post("/admin/providers", json={
            "label": "same", "provider_type": "openai",
            "api_format": "openai-completions", "base_url": "https://a.com",
            "default_model": "m", "api_key": "k",
        })
        assert r.status_code == 409

    def test_invalid_api_format_returns_422(self, client):
        r = client.post("/admin/providers", json={
            "label": "bad", "provider_type": "openai",
            "api_format": "made-up-format", "base_url": "https://a.com",
            "default_model": "m", "api_key": "k",
        })
        assert r.status_code == 422

    def test_update(self, client):
        p = self._create(client)
        r = client.put(f"/admin/providers/{p['id']}", json={
            "default_model": "gpt-4o",
            "base_url": "https://api.openai.com/v2",
        })
        assert r.status_code == 200
        assert r.json()["default_model"] == "gpt-4o"

    def test_update_nonexistent_returns_404(self, client):
        r = client.put("/admin/providers/no-such-id", json={"label": "x"})
        assert r.status_code == 404

    def test_delete(self, client):
        p = self._create(client)
        r = client.delete(f"/admin/providers/{p['id']}")
        assert r.status_code == 204
        r2 = client.get("/admin/providers")
        assert len(r2.json()["data"]) == 0

    def test_delete_nonexistent_returns_404(self, client):
        r = client.delete("/admin/providers/no-such-id")
        assert r.status_code == 404

    def test_delete_with_binding_returns_409(self, client):
        """Cannot delete a provider that has active bindings."""
        prov = self._create(client)
        # Create project with binding
        client.post("/admin/projects", json={
            "name": "myapp",
            "bindings": [{"alias": "chat", "provider_id": prov["id"]}],
        })
        r = client.delete(f"/admin/providers/{prov['id']}")
        assert r.status_code == 409


class TestProjectAPI:
    @pytest.fixture(autouse=True)
    def mock_probe(self, monkeypatch):
        """Prevent real HTTP connectivity probes during project API tests."""
        monkeypatch.setattr(
            "leafhub.manage.providers._probe_provider",
            lambda *a, **kw: (True, "mocked — always connected"),
        )

    def _create_provider(self, client) -> dict:
        r = client.post("/admin/providers", json={
            "label": "prov", "provider_type": "openai",
            "api_format": "openai-completions", "base_url": "https://a.com",
            "default_model": "m", "api_key": "sk-x",
        })
        return r.json()

    def test_create_returns_token_once(self, client):
        r = client.post("/admin/projects", json={"name": "myapp"})
        assert r.status_code == 201
        data = r.json()
        assert "token" in data
        assert data["token"].startswith("lh-proj-")

    def test_token_absent_on_list(self, client):
        client.post("/admin/projects", json={"name": "myapp"})
        r = client.get("/admin/projects")
        for proj in r.json()["data"]:
            assert "token" not in proj

    def test_create_with_bindings(self, client):
        prov = self._create_provider(client)
        r = client.post("/admin/projects", json={
            "name": "myapp",
            "bindings": [{"alias": "chat", "provider_id": prov["id"]}],
        })
        assert r.status_code == 201
        assert len(r.json()["bindings"]) == 1

    def test_same_name_allowed(self, client):
        # Projects are identified by token hash, not name — two projects may
        # share a name (e.g. different environments / agents for the same app).
        r1 = client.post("/admin/projects", json={"name": "dup"})
        r2 = client.post("/admin/projects", json={"name": "dup"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] != r2.json()["id"]

    def test_update_name_and_bindings(self, client):
        prov = self._create_provider(client)
        proj = client.post("/admin/projects", json={"name": "old"}).json()
        r = client.put(f"/admin/projects/{proj['id']}", json={
            "name": "new",
            "bindings": [{"alias": "chat", "provider_id": prov["id"]}],
        })
        assert r.status_code == 200
        assert r.json()["name"] == "new"
        assert len(r.json()["bindings"]) == 1

    def test_rotate_token(self, client):
        proj = client.post("/admin/projects", json={"name": "myapp"}).json()
        old_token = proj["token"]
        r = client.post(f"/admin/projects/{proj['id']}/rotate-token")
        assert r.status_code == 200
        new_token = r.json()["token"]
        assert new_token != old_token
        assert new_token.startswith("lh-proj-")

    def test_deactivate_and_activate(self, client):
        proj = client.post("/admin/projects", json={"name": "myapp"}).json()

        r = client.post(f"/admin/projects/{proj['id']}/deactivate")
        assert r.status_code == 204
        listing = client.get("/admin/projects").json()["data"]
        assert not listing[0]["is_active"]

        r = client.post(f"/admin/projects/{proj['id']}/activate")
        assert r.status_code == 204
        listing = client.get("/admin/projects").json()["data"]
        assert listing[0]["is_active"]

    def test_delete_project(self, client):
        proj = client.post("/admin/projects", json={"name": "myapp"}).json()
        r = client.delete(f"/admin/projects/{proj['id']}")
        assert r.status_code == 204
        assert client.get("/admin/projects").json()["data"] == []

    def test_delete_nonexistent_returns_404(self, client):
        r = client.delete("/admin/projects/no-such-id")
        assert r.status_code == 404

    def test_rotate_nonexistent_returns_404(self, client):
        r = client.post("/admin/projects/no-such-id/rotate-token")
        assert r.status_code == 404


class TestAuth:
    @pytest.fixture()
    def authed_client(self, app, monkeypatch):
        """Client with admin token set in environment."""
        from fastapi.testclient import TestClient
        monkeypatch.setenv("LEAFHUB_ADMIN_TOKEN", "super-secret")
        with TestClient(app) as c:
            yield c

    def test_no_token_in_dev_mode(self, client):
        """Without LEAFHUB_ADMIN_TOKEN set, all admin routes are open."""
        r = client.get("/admin/providers")
        assert r.status_code == 200

    def test_missing_token_returns_401(self, authed_client):
        r = authed_client.get("/admin/providers")
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, authed_client):
        r = authed_client.get(
            "/admin/providers",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401

    def test_correct_token_succeeds(self, authed_client):
        r = authed_client.get(
            "/admin/providers",
            headers={"Authorization": "Bearer super-secret"},
        )
        assert r.status_code == 200

    def test_rate_limit_after_failures(self, authed_client):
        """After 5 failed attempts the IP is locked out (429)."""
        for _ in range(5):
            authed_client.get("/admin/providers",
                               headers={"Authorization": "Bearer bad"})
        r = authed_client.get("/admin/providers",
                               headers={"Authorization": "Bearer bad"})
        assert r.status_code == 429

    def test_health_unprotected(self, authed_client):
        """/health is always accessible regardless of admin token."""
        r = authed_client.get("/health")
        assert r.status_code == 200

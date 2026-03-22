"""
tests/test_new_features.py

Tests for two features added in the current sprint:

  Feature 1 — Provider connectivity probe
    _probe_provider() unit tests (all HTTP response branches + network errors)
    create_provider API endpoint: probe failure → 422, probe success → 201

  Feature 2 — Same-name projects
    DB migration removes UNIQUE constraint from projects.name on existing DBs
    Store layer allows multiple projects with identical names
    API layer no longer returns 409 on duplicate name
    Same-name project tokens are independent
    Renaming to an already-used name is allowed

  Feature 3 — leafhub_dist/ module distributed on project link (v2, 2026-03-21)
    POST /admin/projects/{id}/link creates leafhub_dist/ in project root
    POST /admin/projects (with path=) creates leafhub_dist/ in project root
    Distribution is idempotent (re-linking does not overwrite existing leafhub_dist/)
    leafhub_dist/ not written when no path is given

Run:
    pytest tests/test_new_features.py -v
or standalone:
    python tests/test_new_features.py
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from leafhub.core.db import open_db
from leafhub.core.store import SyncStore
from leafhub.manage.providers import _probe_provider

try:
    from starlette.testclient import TestClient
    from leafhub.manage.server import create_app
    _FASTAPI = True
except ImportError:
    _FASTAPI = False


# ── shared helpers ────────────────────────────────────────────────────────────

def _make_hub(tmp_path: Path) -> tuple[Path, bytes, SyncStore]:
    hub = tmp_path / ".leafhub"
    hub.mkdir(parents=True)
    key = secrets.token_bytes(32)
    store = SyncStore(open_db(hub))
    return hub, key, store


def _key_env(key: bytes):
    return patch.dict(os.environ, {"LEAFHUB_MASTER_KEY": base64.b64encode(key).decode()})


def _mock_200():
    """Return a mock context-manager that simulates a 200 OK response."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.status = 200
    return cm


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="http://x", code=code, msg="", hdrs=None, fp=None)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _probe_provider — unit tests (no network)
# ═══════════════════════════════════════════════════════════════════════════════

class TestProbeProviderUnit(unittest.TestCase):
    """All tests mock urllib.request.urlopen so no network is needed."""

    _BASE  = "https://api.example.com/v1"
    _KEY   = "sk-testkey"

    def _probe(self, *, api_format="openai-completions", api_key=_KEY,
               auth_mode="bearer", auth_header=None, extra_headers=None):
        return _probe_provider(
            self._BASE, api_format, api_key, auth_mode,
            auth_header, extra_headers or {},
        )

    # ── Auth guard (no HTTP call needed) ─────────────────────────────────────

    def test_missing_key_with_bearer_returns_fail(self):
        ok, msg = self._probe(api_key="", auth_mode="bearer")
        self.assertFalse(ok)
        self.assertIn("API key is required", msg)

    def test_missing_key_with_x_api_key_returns_fail(self):
        ok, msg = self._probe(api_key="", auth_mode="x-api-key")
        self.assertFalse(ok)

    def test_missing_key_with_none_auth_is_ok_no_call(self):
        """auth_mode=none skips the key guard — probe proceeds."""
        with patch("urllib.request.urlopen", return_value=_mock_200()):
            ok, msg = self._probe(api_key="", auth_mode="none")
        self.assertTrue(ok)

    # ── HTTP response branches ────────────────────────────────────────────────

    def test_200_returns_true(self):
        with patch("urllib.request.urlopen", return_value=_mock_200()):
            ok, msg = self._probe()
        self.assertTrue(ok)
        self.assertIn("200", msg)

    def test_401_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(401)):
            ok, msg = self._probe()
        self.assertFalse(ok)
        self.assertIn("401", msg)
        self.assertIn("API key", msg)

    def test_403_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(403)):
            ok, msg = self._probe()
        self.assertFalse(ok)
        self.assertIn("403", msg)

    def test_404_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            ok, msg = self._probe()
        self.assertFalse(ok)
        self.assertIn("404", msg)
        self.assertIn("Base URL", msg)

    def test_429_rate_limited_returns_true(self):
        """429 means the key is valid and the endpoint is reachable."""
        with patch("urllib.request.urlopen", side_effect=_http_error(429)):
            ok, msg = self._probe()
        self.assertTrue(ok)
        self.assertIn("429", msg)

    def test_422_other_4xx_returns_true(self):
        """Other 4xx: endpoint reachable, likely format/version mismatch."""
        with patch("urllib.request.urlopen", side_effect=_http_error(422)):
            ok, msg = self._probe()
        self.assertTrue(ok)

    def test_500_server_error_returns_true(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(500)):
            ok, msg = self._probe()
        self.assertTrue(ok)
        self.assertIn("500", msg)

    def test_url_error_returns_false(self):
        err = urllib.error.URLError("Name or service not known")
        with patch("urllib.request.urlopen", side_effect=err):
            ok, msg = self._probe()
        self.assertFalse(ok)
        self.assertIn("Connection failed", msg)

    def test_timeout_returns_false(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            ok, msg = self._probe()
        self.assertFalse(ok)
        self.assertIn("timed out", msg)

    # ── Probe URL / headers ───────────────────────────────────────────────────

    def test_probe_uses_models_for_ollama(self):
        # Ollama preset base URL ends in /v1 (OpenAI-compat layer), so the
        # probe appends /models → .../v1/models — same as openai-completions.
        # The native /api/tags endpoint lives at the root and is not probed.
        captured = {}
        def _urlopen(req, *, timeout):
            captured["url"] = req.full_url
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(api_format="ollama", api_key="", auth_mode="none")
        self.assertTrue(captured["url"].endswith("/models"))

    def test_probe_uses_models_for_openai(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["url"] = req.full_url
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(api_format="openai-completions")
        self.assertTrue(captured["url"].endswith("/models"))

    def test_bearer_auth_header_set(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(auth_mode="bearer", api_key="sk-mykey")
        auth = captured["headers"].get("Authorization") or captured["headers"].get("authorization")
        self.assertIsNotNone(auth)
        self.assertIn("sk-mykey", auth)

    def test_x_api_key_auth_header_set(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(auth_mode="x-api-key", api_key="ak-mykey")
        # header may be normalised to title-case by urllib
        all_keys = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertIn("ak-mykey", all_keys.get("x-api-key", ""))

    def test_bearer_custom_auth_header_name(self):
        """auth_header override changes the header name."""
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(auth_mode="bearer", api_key="sk-x", auth_header="Api-Key")
        all_keys = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertIn("api-key", all_keys)
        self.assertIn("sk-x", all_keys["api-key"])

    def test_none_auth_mode_sends_no_auth_header(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(auth_mode="none", api_key="")
        all_keys = {k.lower() for k in captured["headers"]}
        self.assertNotIn("authorization", all_keys)
        self.assertNotIn("x-api-key", all_keys)

    def test_anthropic_version_header_auto_injected(self):
        """anthropic-messages format auto-adds anthropic-version when absent."""
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(api_format="anthropic-messages", auth_mode="x-api-key")
        all_keys = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertIn("anthropic-version", all_keys)
        self.assertEqual(all_keys["anthropic-version"], "2023-06-01")

    def test_anthropic_version_not_overridden_when_caller_provides_it(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(
                api_format="anthropic-messages", auth_mode="x-api-key",
                extra_headers={"anthropic-version": "2024-01-01"},
            )
        all_keys = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(all_keys["anthropic-version"], "2024-01-01")

    def test_extra_headers_forwarded(self):
        captured = {}
        def _urlopen(req, *, timeout):
            captured["headers"] = dict(req.headers)
            return _mock_200()
        with patch("urllib.request.urlopen", side_effect=_urlopen):
            self._probe(extra_headers={"X-Custom-Header": "hello"})
        all_keys = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(all_keys.get("x-custom-header"), "hello")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. create_provider API endpoint — probe gate
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_FASTAPI, "FastAPI / starlette not installed")
class TestCreateProviderWithProbe(unittest.TestCase):

    def setUp(self):
        self._tmp   = tempfile.TemporaryDirectory()
        self.hub    = Path(self._tmp.name) / ".leafhub"
        self.hub.mkdir()
        self.key    = secrets.token_bytes(32)
        app         = create_app(hub_dir=self.hub, master_key=self.key)
        self._tc    = TestClient(app, raise_server_exceptions=True)
        self.client = self._tc.__enter__()

    def tearDown(self):
        self._tc.__exit__(None, None, None)
        self._tmp.cleanup()

    _BODY = {
        "label":         "TestProv",
        "api_format":    "openai-completions",
        "base_url":      "https://api.example.com/v1",
        "default_model": "gpt-test",
        "api_key":       "sk-testkey",
    }

    def test_probe_failure_returns_422_and_nothing_saved(self):
        """If the probe fails, the provider must NOT be persisted."""
        with patch(
            "leafhub.manage.providers._probe_provider",
            return_value=(False, "Authentication failed — check your API key (HTTP 401)"),
        ):
            r = self.client.post("/admin/providers", json=self._BODY)

        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("connectivity check failed", r.json()["detail"])

        # Nothing should be in the DB
        list_r = self.client.get("/admin/providers")
        self.assertEqual(list_r.json()["data"], [])

    def test_probe_success_creates_provider(self):
        with patch(
            "leafhub.manage.providers._probe_provider",
            return_value=(True, "Connected successfully (HTTP 200)"),
        ):
            r = self.client.post("/admin/providers", json=self._BODY)

        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["label"], "TestProv")
        self.assertEqual(body["api_format"], "openai-completions")

    def test_probe_called_with_resolved_auth_mode(self):
        """For anthropic-messages with no explicit auth_mode, probe gets x-api-key."""
        calls = []
        def _fake_probe(base_url, api_format, api_key, auth_mode, *args, **kwargs):
            calls.append({"auth_mode": auth_mode, "api_format": api_format})
            return True, "ok"

        body = {**self._BODY, "api_format": "anthropic-messages", "label": "Anthropic"}
        with patch("leafhub.manage.providers._probe_provider", side_effect=_fake_probe):
            self.client.post("/admin/providers", json=body)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["auth_mode"], "x-api-key")

    def test_probe_called_with_none_auth_for_ollama(self):
        body = {**self._BODY, "api_format": "ollama",
                "label": "Ollama", "api_key": ""}
        calls = []
        def _fake_probe(base_url, api_format, api_key, auth_mode, *args, **kwargs):
            calls.append(auth_mode)
            return True, "ok"
        with patch("leafhub.manage.providers._probe_provider", side_effect=_fake_probe):
            self.client.post("/admin/providers", json=body)
        self.assertEqual(calls[0], "none")

    def test_explicit_auth_mode_overrides_default(self):
        """Caller can override the inferred auth_mode."""
        calls = []
        def _fake_probe(base_url, api_format, api_key, auth_mode, *args, **kwargs):
            calls.append(auth_mode)
            return True, "ok"
        body = {**self._BODY, "auth_mode": "x-api-key", "label": "Override"}
        with patch("leafhub.manage.providers._probe_provider", side_effect=_fake_probe):
            self.client.post("/admin/providers", json=body)
        self.assertEqual(calls[0], "x-api-key")

    def test_probe_not_called_on_update(self):
        """PUT /admin/providers/{id} must NOT re-probe — user updates at their own risk."""
        with patch(
            "leafhub.manage.providers._probe_provider",
            return_value=(True, "ok"),
        ) as mock_probe:
            r = self.client.post("/admin/providers", json=self._BODY)
            prov_id = r.json()["id"]
            mock_probe.reset_mock()

            self.client.put(f"/admin/providers/{prov_id}", json={"label": "Updated"})
            mock_probe.assert_not_called()

    def test_probe_error_message_surfaced_in_422_detail(self):
        with patch(
            "leafhub.manage.providers._probe_provider",
            return_value=(False, "Connection failed: [Errno -2] Name or service not known"),
        ):
            r = self.client.post("/admin/providers", json=self._BODY)
        self.assertIn("Name or service not known", r.json()["detail"])


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Same-name projects — DB migration
# ═══════════════════════════════════════════════════════════════════════════════

class TestSameNameProjectsMigration(unittest.TestCase):

    def test_unique_constraint_removed_by_migration(self):
        """
        An existing DB where projects.name is UNIQUE must be migrated so that
        two rows with the same name can be inserted.
        """
        import re
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "projects.db"
            # Build old-style DB with UNIQUE on name
            conn = sqlite3.connect(str(db_file))
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE providers (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                    provider_type TEXT NOT NULL, api_format TEXT NOT NULL,
                    base_url TEXT NOT NULL, default_model TEXT NOT NULL,
                    available_models TEXT,
                    auth_mode TEXT NOT NULL DEFAULT 'bearer',
                    auth_header TEXT, extra_headers TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_prefix TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE model_bindings (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL,
                    provider_id TEXT NOT NULL REFERENCES providers(id),
                    model_override TEXT,
                    UNIQUE(project_id, alias)
                );
            """)
            conn.close()

            # open_db must migrate — removing the UNIQUE on name
            migrated = open_db(hub_dir=Path(tmp))

            # Verify: schema must NOT have UNIQUE on name
            row = migrated.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(
                re.search(r"name\s+TEXT\s+NOT\s+NULL\s+UNIQUE", row[0]),
                "UNIQUE constraint on projects.name was not removed by migration",
            )

            # Practical check: two rows with the same name must be insertable
            import uuid, hashlib
            def _insert(name):
                pid = str(uuid.uuid4())
                tok = "lh-proj-" + secrets.token_hex(16)
                h   = hashlib.sha256(tok.encode()).hexdigest()
                migrated.execute(
                    "INSERT INTO projects (id, name, token_hash, token_prefix, is_active) "
                    "VALUES (?,?,?,?,1)",
                    (pid, name, h, "lh-proj-"),
                )
                migrated.commit()

            _insert("duplicate-name")
            _insert("duplicate-name")   # must not raise

            count = migrated.execute(
                "SELECT COUNT(*) FROM projects WHERE name = 'duplicate-name'"
            ).fetchone()[0]
            self.assertEqual(count, 2)

            migrated.close()

    def test_fresh_db_allows_duplicate_names_immediately(self):
        """Fresh DBs are created without the UNIQUE constraint."""
        with tempfile.TemporaryDirectory() as tmp:
            import uuid, hashlib
            conn = open_db(hub_dir=Path(tmp))
            def _insert(name):
                pid = str(uuid.uuid4())
                tok = "lh-proj-" + secrets.token_hex(16)
                h   = hashlib.sha256(tok.encode()).hexdigest()
                conn.execute(
                    "INSERT INTO projects (id, name, token_hash, token_prefix, is_active) "
                    "VALUES (?,?,?,?,1)",
                    (pid, name, h, "lh-proj-"),
                )
                conn.commit()
            _insert("same")
            _insert("same")
            count = conn.execute(
                "SELECT COUNT(*) FROM projects WHERE name='same'"
            ).fetchone()[0]
            self.assertEqual(count, 2)
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Same-name projects — store layer
# ═══════════════════════════════════════════════════════════════════════════════

class TestSameNameProjectsStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        hub = Path(self._tmp.name) / ".leafhub"
        hub.mkdir()
        self.store = SyncStore(open_db(hub))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_two_projects_same_name_both_created(self):
        p1, t1 = self.store.create_project("agent")
        p2, t2 = self.store.create_project("agent")
        self.assertNotEqual(p1.id, p2.id)
        self.assertNotEqual(t1, t2)

    def test_three_projects_same_name(self):
        ids = [self.store.create_project("shared")[0].id for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)

    def test_same_name_projects_have_unique_tokens(self):
        _, t1 = self.store.create_project("multi")
        _, t2 = self.store.create_project("multi")
        _, t3 = self.store.create_project("multi")
        self.assertEqual(len({t1, t2, t3}), 3)

    def test_each_token_authenticates_its_own_project(self):
        p1, t1 = self.store.create_project("shared")
        p2, t2 = self.store.create_project("shared")

        auth1 = self.store.authenticate_project(t1)
        auth2 = self.store.authenticate_project(t2)
        self.assertIsNotNone(auth1)
        self.assertIsNotNone(auth2)
        self.assertEqual(auth1.id, p1.id)
        self.assertEqual(auth2.id, p2.id)

    def test_rotating_one_does_not_affect_the_other(self):
        p1, t1 = self.store.create_project("pair")
        p2, t2 = self.store.create_project("pair")

        self.store.rotate_token(p1.id)
        # p2's original token must still work
        auth = self.store.authenticate_project(t2)
        self.assertIsNotNone(auth)
        self.assertEqual(auth.id, p2.id)

    def test_deactivating_one_does_not_affect_the_other(self):
        p1, t1 = self.store.create_project("pair2")
        p2, t2 = self.store.create_project("pair2")

        self.store.deactivate_project(p1.id)
        # p1's token returns None (deactivated)
        self.assertIsNone(self.store.authenticate_project(t1))
        # p2 still works
        auth = self.store.authenticate_project(t2)
        self.assertIsNotNone(auth)
        self.assertEqual(auth.id, p2.id)

    def test_deleting_one_preserves_the_other(self):
        p1, _ = self.store.create_project("pair3")
        p2, t2 = self.store.create_project("pair3")

        self.store.delete_project(p1.id)
        # p2 must still be retrievable
        reloaded = self.store.get_project(p2.id)
        self.assertEqual(reloaded.name, "pair3")

    def test_find_project_by_name_returns_one(self):
        """find_project_by_name returns the first match — callers must not assume uniqueness."""
        self.store.create_project("findme")
        self.store.create_project("findme")
        result = self.store.find_project_by_name("findme")
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Same-name projects — API layer
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_FASTAPI, "FastAPI / starlette not installed")
class TestSameNameProjectsAPI(unittest.TestCase):

    def setUp(self):
        self._tmp   = tempfile.TemporaryDirectory()
        self.hub    = Path(self._tmp.name) / ".leafhub"
        self.hub.mkdir()
        self.key    = secrets.token_bytes(32)
        app         = create_app(hub_dir=self.hub, master_key=self.key)
        self._tc    = TestClient(app, raise_server_exceptions=True)
        self.client = self._tc.__enter__()

    def tearDown(self):
        self._tc.__exit__(None, None, None)
        self._tmp.cleanup()

    def _create_project(self, name: str) -> dict:
        r = self.client.post("/admin/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_same_name_projects_both_return_201(self):
        p1 = self._create_project("agent")
        p2 = self._create_project("agent")
        self.assertNotEqual(p1["id"], p2["id"])
        self.assertEqual(p1["name"], p2["name"])

    def test_both_projects_appear_in_list(self):
        self._create_project("listed")
        self._create_project("listed")
        r = self.client.get("/admin/projects")
        same = [p for p in r.json()["data"] if p["name"] == "listed"]
        self.assertEqual(len(same), 2)

    def test_rename_to_existing_name_returns_200(self):
        """Renaming to an already-used name is now allowed."""
        self._create_project("existing-name")
        p2 = self._create_project("to-rename")
        r = self.client.put(f"/admin/projects/{p2['id']}", json={"name": "existing-name"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "existing-name")

    def test_rename_project_to_own_name_ok(self):
        p = self._create_project("same-self")
        r = self.client.put(f"/admin/projects/{p['id']}", json={"name": "same-self"})
        self.assertEqual(r.status_code, 200)

    def test_each_token_is_independent(self):
        """The tokens returned for same-name projects must be distinct."""
        p1 = self._create_project("token-pair")
        p2 = self._create_project("token-pair")
        self.assertNotEqual(p1["token"], p2["token"])


# ═══════════════════════════════════════════════════════════════════════════════
# 6. probe.py copy — distributed on link / create-with-path
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_FASTAPI, "FastAPI / starlette not installed")
class TestProbeCopyOnLink(unittest.TestCase):
    """
    When a project is linked to a local directory, LeafHub distributes the
    leafhub_dist/ integration module (v2 standard, 2026-03-21):
      leafhub_dist/__init__.py  — Python package entrypoint
      leafhub_dist/probe.py     — stdlib-only runtime detection
      leafhub_dist/register.sh  — shell registration helper
    """

    def setUp(self):
        self._tmp   = tempfile.TemporaryDirectory()
        self.hub    = Path(self._tmp.name) / ".leafhub"
        self.hub.mkdir()
        self.key    = secrets.token_bytes(32)
        self.proj_dir = Path(tempfile.mkdtemp())
        app         = create_app(hub_dir=self.hub, master_key=self.key)
        self._tc    = TestClient(app, raise_server_exceptions=True)
        self.client = self._tc.__enter__()

    def tearDown(self):
        self._tc.__exit__(None, None, None)
        import shutil
        shutil.rmtree(self.proj_dir, ignore_errors=True)
        self._tmp.cleanup()

    def _create_project(self, name="test-proj") -> dict:
        r = self.client.post("/admin/projects", json={"name": name})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_link_endpoint_copies_probe_file(self):
        p = self._create_project()
        r = self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        self.assertEqual(r.status_code, 200, r.text)
        probe_copy = self.proj_dir / "leafhub_dist" / "probe.py"
        self.assertTrue(probe_copy.exists(), "leafhub_dist/probe.py not found in project dir")

    def test_probe_copy_is_valid_python(self):
        p = self._create_project()
        self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        import ast
        src = (self.proj_dir / "leafhub_dist" / "probe.py").read_text(encoding="utf-8")
        try:
            ast.parse(src)
        except SyntaxError as e:
            self.fail(f"leafhub_dist/probe.py is not valid Python: {e}")

    def test_probe_copy_is_executable_standalone(self):
        """The distributed probe must be importable and expose the public API."""
        p = self._create_project()
        self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        import importlib.util
        mod_name = "_leafhub_probe_standalone_test"
        spec = importlib.util.spec_from_file_location(
            mod_name, self.proj_dir / "leafhub_dist" / "probe.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules before exec so that deferred annotations
        # (from __future__ import annotations) resolve correctly.
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
            self.assertTrue(hasattr(mod, "detect"), "distributed probe missing detect()")
            self.assertTrue(hasattr(mod, "ProbeResult"), "distributed probe missing ProbeResult")
        finally:
            sys.modules.pop(mod_name, None)

    def test_create_with_path_also_copies_probe(self):
        r = self.client.post("/admin/projects", json={
            "name": "create-linked",
            "path": str(self.proj_dir),
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue((self.proj_dir / "leafhub_dist" / "probe.py").exists())

    def test_relink_already_integrated_does_not_overwrite_files(self):
        """
        After the first link, leafhub_dist/ is distributed to the project dir,
        marking it as 'already integrated'.  A subsequent link must only update
        .leafhub and must NOT overwrite integration files.

        This allows projects to customise their local copies without having
        LeafHub silently undo those changes on every re-link.
        """
        p = self._create_project()
        # First link — distributes leafhub_dist/
        self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        dist_dir = self.proj_dir / "leafhub_dist"
        self.assertTrue(dist_dir.is_dir(),
                        "first link must distribute leafhub_dist/")
        self.assertTrue((dist_dir / "probe.py").exists(),
                        "first link must distribute leafhub_dist/probe.py")

        # Overwrite probe to simulate local customisation
        (dist_dir / "probe.py").write_text("# custom probe", encoding="utf-8")

        # Second link — leafhub_dist/ already present → should NOT overwrite
        self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        probe_src = (dist_dir / "probe.py").read_text(encoding="utf-8")
        self.assertEqual(probe_src.strip(), "# custom probe",
                         "re-link must not overwrite leafhub_dist/probe.py when already integrated")

    def test_relink_without_register_sh_distributes_files(self):
        """
        A directory that was linked before the v2 standard (no leafhub_dist/,
        no root register.sh) is treated as not yet integrated and receives the
        leafhub_dist/ module on the next link.
        """
        p = self._create_project()
        # Manually write only .leafhub — simulating a pre-v2 link
        from leafhub.manage.projects import _write_dotfile
        raw_token = "lh-proj-" + "x" * 32
        _write_dotfile(self.proj_dir, p["name"], raw_token)
        self.assertFalse((self.proj_dir / "leafhub_dist").exists())

        # Link via API — no leafhub_dist/ → should distribute
        self.client.post(
            f"/admin/projects/{p['id']}/link",
            json={"path": str(self.proj_dir)},
        )
        self.assertTrue((self.proj_dir / "leafhub_dist").is_dir(),
                        "link must distribute leafhub_dist/ to non-integrated dir")
        self.assertTrue((self.proj_dir / "leafhub_dist" / "probe.py").exists(),
                        "link must distribute leafhub_dist/probe.py to non-integrated dir")

    def test_no_probe_copy_when_no_path(self):
        """Creating a project without a path must not create leafhub_dist/ anywhere."""
        p = self._create_project("no-path-proj")
        # The project dir fixture exists but should not have a dist dir
        self.assertFalse((self.proj_dir / "leafhub_dist").exists())


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)

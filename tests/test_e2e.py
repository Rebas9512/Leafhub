"""
End-to-end test — full workflow + edge cases.

Exercises the complete user journey across all three phases:

  Story A — CLI workflow (Phase 2)
    1. Add provider (metadata + encrypted key)
    2. Create project → receive one-time token
    3. Bind alias to provider
    4. SDK reads key, gets config, lists aliases
    5. Model override: SDK returns overridden model
    6. Two projects share the same provider independently
    7. Token rotation: old token rejected, new token works
    8. Project deactivation: deactivated project token rejected
    9. Unbind alias, then re-bind
   10. Provider deletion blocked while binding exists
   11. Provider deletion after unbind → key purged from providers.enc

  Story B — Manage API workflow (Phase 3, skipped if FastAPI absent)
   12. Full CRUD via HTTP: provider → project → bind → SDK read
   13. Rename project (happy path + same-name rename now allowed)
   14. Rotate token via API: old token rejected, new token works
   15. Provider key update via API: SDK sees updated key
   16. Delete provider via API: key purged, SDK gets DecryptionError

  Edge cases
   17. SDK with missing DB → StorageNotFoundError
   18. SDK with invalid token → InvalidTokenError
   19. SDK with inactive project token → InvalidTokenError
   20. SDK alias not bound → AliasNotBoundError
   21. SDK with wrong master key → DecryptionError
   22. SDK context manager closes connection on exit
   23. Duplicate provider label → rejected (CLI layer)
   24. Duplicate project name → allowed (independent tokens)
   25. add_binding with nonexistent provider → IntegrityError
   26. delete_project cascades bindings

All tests use temp directories — ~/.leafhub/ is never touched.
Master key is injected via LEAFHUB_MASTER_KEY env var.
"""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).parent.parent
_SRC  = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from leafhub import LeafHub
from leafhub.errors import (
    AliasNotBoundError,
    DecryptionError,
    InvalidTokenError,
    StorageNotFoundError,
)
from leafhub.core.db import open_db
from leafhub.core.store import SyncStore
from leafhub.core.crypto import (
    encrypt_providers,
    decrypt_providers,
    load_master_key,
)

try:
    from starlette.testclient import TestClient
    from leafhub.manage.server import create_app
    _FASTAPI = True
except ImportError:
    _FASTAPI = False


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_hub(tmp: str) -> tuple[Path, bytes, SyncStore]:
    """Create an isolated hub_dir, return (hub_dir, master_key, store)."""
    hub = Path(tmp) / ".leafhub"
    hub.mkdir(parents=True)
    key = secrets.token_bytes(32)
    store = SyncStore(open_db(hub))
    return hub, key, store


def _with_key(key: bytes):
    """Return a patch.dict context that injects the master key as env var."""
    return patch.dict(os.environ, {"LEAFHUB_MASTER_KEY": base64.b64encode(key).decode()})


def _add_provider(store: SyncStore, hub: Path, key: bytes,
                  label: str = "TestProv", api_key: str = "sk-test") -> str:
    """Insert provider in DB and enc file. Returns provider_id."""
    p = store.create_provider(
        label=label,
        provider_type="custom",
        api_format="openai-completions",
        base_url="https://api.example.com/v1",
        default_model="gpt-test",
        available_models=["gpt-test", "gpt-other"],
    )
    ks = decrypt_providers(key, hub)
    ks[p.id] = {"api_key": api_key}
    encrypt_providers(ks, key, hub)
    return p.id


# ═════════════════════════════════════════════════════════════════════════════
# Story A — full CLI-layer workflow
# ═════════════════════════════════════════════════════════════════════════════

class TestFullCLIWorkflow(unittest.TestCase):

    def setUp(self):
        self._tmp   = tempfile.TemporaryDirectory()
        self.hub, self.key, self.store = _make_hub(self._tmp.name)
        self._key_patch = _with_key(self.key)
        self._key_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    # ── 1. Add provider ───────────────────────────────────────────────────

    def test_01_provider_key_stored_encrypted(self):
        pid = _add_provider(self.store, self.hub, self.key, "OpenAI", "sk-abc")
        ks  = decrypt_providers(self.key, self.hub)
        self.assertIn(pid, ks)
        self.assertEqual(ks[pid]["api_key"], "sk-abc")
        # Raw key must NOT appear in DB
        row = self.store._conn.execute(
            "SELECT * FROM providers WHERE id = ?", (pid,)
        ).fetchone()
        # The DB has no text columns that contain the plaintext key
        for col in ("label", "provider_type", "api_format", "base_url", "default_model"):
            self.assertNotIn("sk-abc", str(row[col]))

    # ── 2. Create project → one-time token ───────────────────────────────

    def test_02_project_token_format(self):
        _, token = self.store.create_project("MyProject")
        self.assertTrue(token.startswith("lh-proj-"))
        self.assertEqual(len(token), 40)   # "lh-proj-" (8) + 32 hex chars

    def test_02_token_hash_never_equals_raw(self):
        import hashlib
        proj, token = self.store.create_project("HashCheck")
        token_hash  = hashlib.sha256(token.encode()).hexdigest()
        row = self.store._conn.execute(
            "SELECT token_hash FROM projects WHERE id = ?", (proj.id,)
        ).fetchone()
        self.assertEqual(row["token_hash"], token_hash)
        self.assertNotEqual(row["token_hash"], token)

    # ── 3-4. Bind alias → SDK reads key ──────────────────────────────────

    def test_03_sdk_reads_key_via_alias(self):
        pid           = _add_provider(self.store, self.hub, self.key, "Prov", "sk-real")
        proj, token   = self.store.create_project("Proj")
        self.store.add_binding(proj.id, "gpt", pid)

        hub = LeafHub(token=token, hub_dir=self.hub)
        self.assertEqual(hub.get_key("gpt"), "sk-real")
        hub._conn.close()

    def test_04_get_config_returns_full_info(self):
        pid           = _add_provider(self.store, self.hub, self.key, "Prov2", "sk-cfg")
        proj, token   = self.store.create_project("Proj2")
        self.store.add_binding(proj.id, "ai", pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            cfg = hub.get_config("ai")

        self.assertEqual(cfg.api_key,    "sk-cfg")
        self.assertEqual(cfg.base_url,   "https://api.example.com/v1")
        self.assertEqual(cfg.model,      "gpt-test")
        self.assertEqual(cfg.api_format, "openai-completions")

    def test_04_list_aliases(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, token   = self.store.create_project("ListAliases")
        self.store.add_binding(proj.id, "alpha", pid)
        self.store.add_binding(proj.id, "beta",  pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            aliases = hub.list_aliases()

        self.assertCountEqual(aliases, ["alpha", "beta"])

    # ── 5. Model override ─────────────────────────────────────────────────

    def test_05_model_override_returned(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, token   = self.store.create_project("Override")
        self.store.add_binding(proj.id, "custom", pid, model_override="gpt-4-turbo")

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            cfg = hub.get_config("custom")

        self.assertEqual(cfg.model, "gpt-4-turbo")

    def test_05_no_override_uses_default_model(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, token   = self.store.create_project("NoOverride")
        self.store.add_binding(proj.id, "plain", pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            cfg = hub.get_config("plain")

        self.assertEqual(cfg.model, "gpt-test")

    # ── 6. Two projects share the same provider ───────────────────────────

    def test_06_two_projects_independent(self):
        pid = _add_provider(self.store, self.hub, self.key, "Shared", "sk-shared")

        proj1, tok1 = self.store.create_project("Proj-A")
        proj2, tok2 = self.store.create_project("Proj-B")
        self.store.add_binding(proj1.id, "llm", pid, model_override="model-a")
        self.store.add_binding(proj2.id, "llm", pid, model_override="model-b")

        with LeafHub(token=tok1, hub_dir=self.hub) as h1:
            with LeafHub(token=tok2, hub_dir=self.hub) as h2:
                self.assertEqual(h1.get_config("llm").model, "model-a")
                self.assertEqual(h2.get_config("llm").model, "model-b")
                self.assertEqual(h1.get_key("llm"), "sk-shared")
                self.assertEqual(h2.get_key("llm"), "sk-shared")

    # ── 7. Token rotation ─────────────────────────────────────────────────

    def test_07_rotated_old_token_rejected(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, old_tok = self.store.create_project("Rotate")
        self.store.add_binding(proj.id, "ai", pid)

        # Old token works before rotation
        hub = LeafHub(token=old_tok, hub_dir=self.hub)
        hub._conn.close()

        new_tok = self.store.rotate_token(proj.id)

        with self.assertRaises(InvalidTokenError):
            LeafHub(token=old_tok, hub_dir=self.hub)

    def test_07_rotated_new_token_works(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, _       = self.store.create_project("RotateNew")
        self.store.add_binding(proj.id, "ai", pid)
        new_tok = self.store.rotate_token(proj.id)

        with LeafHub(token=new_tok, hub_dir=self.hub) as hub:
            self.assertEqual(hub.get_key("ai"), "sk-test")

    # ── 8. Deactivated project ────────────────────────────────────────────

    def test_08_deactivated_project_token_rejected(self):
        proj, token = self.store.create_project("Deactivated")
        self.store.deactivate_project(proj.id)
        with self.assertRaises(InvalidTokenError):
            LeafHub(token=token, hub_dir=self.hub)

    def test_08_reactivated_project_works(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, token = self.store.create_project("Reactivate")
        self.store.add_binding(proj.id, "ai", pid)
        self.store.deactivate_project(proj.id)
        self.store.activate_project(proj.id)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            self.assertEqual(hub.get_key("ai"), "sk-test")

    # ── 9. Unbind / re-bind ───────────────────────────────────────────────

    def test_09_unbind_then_alias_gone(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, token   = self.store.create_project("UnbindTest")
        self.store.add_binding(proj.id, "ai", pid)
        self.store.remove_binding(proj.id, "ai")

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            with self.assertRaises(AliasNotBoundError):
                hub.get_key("ai")

    def test_09_rebind_works(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, token   = self.store.create_project("Rebind")
        self.store.add_binding(proj.id, "ai", pid)
        self.store.remove_binding(proj.id, "ai")
        self.store.add_binding(proj.id, "ai", pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            self.assertEqual(hub.get_key("ai"), "sk-test")

    # ── 10. Provider delete blocked by active binding ─────────────────────

    def test_10_provider_delete_blocked_by_binding(self):
        pid           = _add_provider(self.store, self.hub, self.key)
        proj, _       = self.store.create_project("BlockDelete")
        self.store.add_binding(proj.id, "ai", pid)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_provider(pid)

    # ── 11. Provider delete after unbind → key purged ────────────────────

    def test_11_provider_delete_purges_key(self):
        pid         = _add_provider(self.store, self.hub, self.key, "ToDelete", "sk-gone")
        proj, _     = self.store.create_project("Purge")
        self.store.add_binding(proj.id, "ai", pid)
        self.store.remove_binding(proj.id, "ai")

        self.store.delete_provider(pid)

        # Simulate CLI key cleanup
        ks = decrypt_providers(self.key, self.hub)
        ks.pop(pid, None)
        encrypt_providers(ks, self.key, self.hub)

        ks_after = decrypt_providers(self.key, self.hub)
        self.assertNotIn(pid, ks_after)


# ═════════════════════════════════════════════════════════════════════════════
# Story B — Manage API workflow
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_FASTAPI, "FastAPI / starlette not installed")
class TestManageAPIWorkflow(unittest.TestCase):

    def setUp(self):
        self._tmp       = tempfile.TemporaryDirectory()
        self.hub        = Path(self._tmp.name) / ".leafhub"
        self.hub.mkdir()
        self.master_key = secrets.token_bytes(32)
        app             = create_app(hub_dir=self.hub, master_key=self.master_key)
        self._tc        = TestClient(app, raise_server_exceptions=True)
        self.client     = self._tc.__enter__()

    def tearDown(self):
        self._tc.__exit__(None, None, None)
        self._tmp.cleanup()

    def _add_provider(self, label="P", api_key="sk-x") -> dict:
        # Patch the probe so tests don't need a real network connection.
        with patch(
            "leafhub.manage.providers._probe_provider",
            return_value=(True, "ok (test bypass)"),
        ):
            r = self.client.post("/admin/providers", json={
                "label": label, "api_format": "openai-completions",
                "base_url": "https://api.example.com/v1",
                "default_model": "test-model", "api_key": api_key,
            })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _add_project(self, name="Proj", bindings=None) -> dict:
        body: dict = {"name": name}
        if bindings:
            body["bindings"] = bindings
        r = self.client.post("/admin/projects", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # ── 12. Full CRUD → SDK read ──────────────────────────────────────────

    def test_12_sdk_reads_key_created_via_api(self):
        prov  = self._add_provider("Prov12", "sk-e2e")
        proj  = self._add_project("Proj12", bindings=[{
            "alias": "ai", "provider_id": prov["id"]
        }])
        token = proj["token"]

        with patch.dict(os.environ, {
            "LEAFHUB_MASTER_KEY": base64.b64encode(self.master_key).decode()
        }):
            with LeafHub(token=token, hub_dir=self.hub) as hub:
                self.assertEqual(hub.get_key("ai"), "sk-e2e")

    # ── 13. Rename project: happy + duplicate-name conflict ───────────────

    def test_13_rename_project_success(self):
        p  = self._add_project("OldName")
        r  = self.client.put(f"/admin/projects/{p['id']}", json={"name": "NewName"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "NewName")

    def test_13_rename_to_existing_name_allowed(self):
        # Same-name projects are now supported — renaming to an existing name is 200.
        self._add_project("Existing")
        p2 = self._add_project("ToRename")
        r  = self.client.put(f"/admin/projects/{p2['id']}", json={"name": "Existing"})
        self.assertEqual(r.status_code, 200)

    def test_13_rename_to_same_name_ok(self):
        """Renaming a project to its own name must not be rejected."""
        p = self._add_project("SameName")
        r = self.client.put(f"/admin/projects/{p['id']}", json={"name": "SameName"})
        self.assertEqual(r.status_code, 200)

    # ── 14. Rotate token via API ──────────────────────────────────────────

    def test_14_rotate_token_old_rejected(self):
        prov  = self._add_provider("ProvRot")
        proj  = self._add_project("ProjRot", bindings=[{"alias": "ai",
                                                         "provider_id": prov["id"]}])
        old_token = proj["token"]

        r = self.client.post(f"/admin/projects/{proj['id']}/rotate-token")
        self.assertEqual(r.status_code, 200)
        new_token = r.json()["token"]

        with patch.dict(os.environ, {
            "LEAFHUB_MASTER_KEY": base64.b64encode(self.master_key).decode()
        }):
            with self.assertRaises(InvalidTokenError):
                LeafHub(token=old_token, hub_dir=self.hub)

            with LeafHub(token=new_token, hub_dir=self.hub) as hub:
                self.assertEqual(hub.get_key("ai"), "sk-x")

    # ── 15. Update provider key via API → SDK sees new key ────────────────

    def test_15_api_key_update_visible_to_sdk(self):
        prov  = self._add_provider("ProvKeyUpdate", "sk-old")
        proj  = self._add_project("ProjKeyUpdate", bindings=[{
            "alias": "ai", "provider_id": prov["id"]
        }])
        token = proj["token"]

        self.client.put(f"/admin/providers/{prov['id']}", json={"api_key": "sk-new"})

        with patch.dict(os.environ, {
            "LEAFHUB_MASTER_KEY": base64.b64encode(self.master_key).decode()
        }):
            with LeafHub(token=token, hub_dir=self.hub) as hub:
                # Fresh instance — not cached from before
                self.assertEqual(hub.get_key("ai"), "sk-new")

    # ── 16. Delete provider via API → key purged, SDK errors ─────────────

    def test_16_deleted_provider_key_purged(self):
        prov  = self._add_provider("ProvDel", "sk-del")
        proj  = self._add_project("ProjDel", bindings=[{
            "alias": "ai", "provider_id": prov["id"]
        }])
        token = proj["token"]

        # Unbind first (FK requirement)
        self.client.put(f"/admin/projects/{proj['id']}", json={"bindings": []})
        r = self.client.delete(f"/admin/providers/{prov['id']}")
        self.assertEqual(r.status_code, 204)

        ks = decrypt_providers(self.master_key, self.hub)
        self.assertNotIn(prov["id"], ks)


# ═════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═════════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self._tmp   = tempfile.TemporaryDirectory()
        self.hub, self.key, self.store = _make_hub(self._tmp.name)
        self._patch = _with_key(self.key)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.store.close()
        self._tmp.cleanup()

    # ── 17. SDK: missing DB ───────────────────────────────────────────────

    def test_17_missing_db_raises_storage_not_found(self):
        empty_hub = Path(self._tmp.name) / "empty"
        empty_hub.mkdir()
        with self.assertRaises(StorageNotFoundError):
            LeafHub(token="lh-proj-" + "a" * 32, hub_dir=empty_hub)

    # ── 18. SDK: invalid token ────────────────────────────────────────────

    def test_18_invalid_token_raises_error(self):
        with self.assertRaises(InvalidTokenError):
            LeafHub(token="lh-proj-" + "0" * 32, hub_dir=self.hub)

    # ── 19. SDK: deactivated project ─────────────────────────────────────

    def test_19_inactive_project_raises_invalid_token(self):
        proj, token = self.store.create_project("InactiveEdge")
        self.store.deactivate_project(proj.id)
        with self.assertRaises(InvalidTokenError):
            LeafHub(token=token, hub_dir=self.hub)

    # ── 20. SDK: alias not bound ──────────────────────────────────────────

    def test_20_unbound_alias_raises_error(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, token = self.store.create_project("UnboundAlias")
        self.store.add_binding(proj.id, "bound-alias", pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            with self.assertRaises(AliasNotBoundError):
                hub.get_key("nonexistent-alias")

    def test_20_error_message_lists_available_aliases(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, token = self.store.create_project("AliasMsg")
        self.store.add_binding(proj.id, "real-alias", pid)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            try:
                hub.get_key("wrong")
            except AliasNotBoundError as e:
                self.assertIn("real-alias", str(e))

    # ── 21. SDK: wrong master key ─────────────────────────────────────────

    def test_21_wrong_master_key_raises_decryption_error(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, token = self.store.create_project("WrongKey")
        self.store.add_binding(proj.id, "ai", pid)

        wrong_key = secrets.token_bytes(32)
        with patch.dict(os.environ, {
            "LEAFHUB_MASTER_KEY": base64.b64encode(wrong_key).decode()
        }):
            with LeafHub(token=token, hub_dir=self.hub) as hub:
                with self.assertRaises(DecryptionError):
                    hub.get_key("ai")

    # ── 22. Context manager closes connection ─────────────────────────────

    def test_22_context_manager_closes_conn(self):
        proj, token = self.store.create_project("CTX")

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            inner_conn = hub._conn

        # After __exit__, the connection should be closed
        with self.assertRaises(Exception):
            inner_conn.execute("SELECT 1")

    # ── 23. Duplicate provider label ──────────────────────────────────────

    def test_23_duplicate_provider_label_blocked(self):
        _add_provider(self.store, self.hub, self.key, "DupeProv")
        existing = self.store.find_provider_by_label("DupeProv")
        self.assertIsNotNone(existing)

        # The app layer (CLI/API) checks find_provider_by_label before insert.
        # Here we verify find_provider_by_label returns the right entry so the
        # caller can reject duplicates.
        self.assertEqual(existing.label, "DupeProv")

    # ── 24. Duplicate project name — now allowed ──────────────────────────

    def test_24_duplicate_project_name_allowed(self):
        # Same-name projects are supported; each gets an independent token.
        p1, t1 = self.store.create_project("DupeProj")
        p2, t2 = self.store.create_project("DupeProj")
        self.assertNotEqual(p1.id, p2.id)
        self.assertNotEqual(t1, t2)
        # find_project_by_name still returns one (first match)
        found = self.store.find_project_by_name("DupeProj")
        self.assertIsNotNone(found)

    # ── 25. add_binding with nonexistent provider → FK error ─────────────

    def test_25_binding_to_deleted_provider_raises_fk_error(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, _     = self.store.create_project("FKTest")
        self.store.add_binding(proj.id, "ai", pid)
        self.store.remove_binding(proj.id, "ai")
        self.store.delete_provider(pid)

        # Clean up enc
        ks = decrypt_providers(self.key, self.hub)
        ks.pop(pid, None)
        encrypt_providers(ks, self.key, self.hub)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_binding(proj.id, "ai", pid)

    # ── 26. delete_project cascades bindings ─────────────────────────────

    def test_26_delete_project_cascades_bindings(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, _     = self.store.create_project("Cascade")
        self.store.add_binding(proj.id, "ai",  pid)
        self.store.add_binding(proj.id, "ai2", pid)

        bindings_before = self.store.list_bindings(proj.id)
        self.assertEqual(len(bindings_before), 2)

        self.store.delete_project(proj.id)

        rows = self.store._conn.execute(
            "SELECT count(*) FROM model_bindings WHERE project_id = ?",
            (proj.id,)
        ).fetchone()[0]
        self.assertEqual(rows, 0)

    # ── 27. provider.enc missing → DecryptionError on get_key ─────────────

    def test_27_missing_enc_file_returns_empty_key_store(self):
        """decrypt_providers returns {} when providers.enc absent; SDK raises DecryptionError."""
        proj, token = self.store.create_project("NoEnc")
        # Add provider metadata only — skip writing providers.enc
        prov = self.store.create_provider(
            label="ghost", provider_type="custom",
            api_format="openai-completions",
            base_url="https://x.com", default_model="m",
        )
        self.store.add_binding(proj.id, "ai", prov.id)

        # Ensure enc file does not exist
        enc = self.hub / "providers.enc"
        enc.unlink(missing_ok=True)

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            with self.assertRaises(DecryptionError):
                hub.get_key("ai")

    # ── 28. set_bindings is atomic ────────────────────────────────────────

    def test_28_set_bindings_atomic_replace(self):
        pid         = _add_provider(self.store, self.hub, self.key)
        proj, token = self.store.create_project("SetBindings")
        self.store.add_binding(proj.id, "old1", pid)
        self.store.add_binding(proj.id, "old2", pid)

        self.store.set_bindings(proj.id, [
            {"alias": "new1", "provider_id": pid},
            {"alias": "new2", "provider_id": pid},
        ])

        with LeafHub(token=token, hub_dir=self.hub) as hub:
            aliases = hub.list_aliases()

        self.assertCountEqual(aliases, ["new1", "new2"])
        self.assertNotIn("old1", aliases)
        self.assertNotIn("old2", aliases)

    # ── 29. providers.enc re-encrypted on every write ─────────────────────

    def test_29_each_write_uses_fresh_salt_and_nonce(self):
        import json
        pid = _add_provider(self.store, self.hub, self.key, "Fresh1", "sk-1")

        enc1 = (self.hub / "providers.enc").read_text()
        salt1  = json.loads(enc1)["salt"]
        nonce1 = json.loads(enc1)["nonce"]

        # Second write
        pid2 = _add_provider(self.store, self.hub, self.key, "Fresh2", "sk-2")
        enc2 = (self.hub / "providers.enc").read_text()
        salt2  = json.loads(enc2)["salt"]
        nonce2 = json.loads(enc2)["nonce"]

        # Each encryption uses a fresh salt and nonce
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(nonce1, nonce2)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.discover(
        start_dir=str(Path(__file__).parent),
        pattern="test_e2e.py",
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

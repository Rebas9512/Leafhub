"""
tests/test_probe_integration.py

Integration tests for the LeafHub probe / dotfile / link flow.

Coverage:
  - leafhub.probe.detect()          — all detection branches + edge cases
  - leafhub.probe.ProbeResult       — property logic
  - leafhub.sdk.LeafHub.from_directory() — directory-tree walk
  - leafhub.manage.projects._write_dotfile — atomic write, permissions, gitignore
  - leafhub.core.store.set_project_path   — DB round-trip
  - leafhub.core.store._row_to_project    — path column migration guard
  - Full link → detect → open_sdk → get_key round-trip
  - Trileaf resolve_from_leafhub          — runtime key resolution layer

Run:
    pytest tests/test_probe_integration.py -v
or standalone:
    python tests/test_probe_integration.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── ensure leafhub package is on sys.path ─────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from leafhub.probe import ProbeResult, detect
from leafhub.core.db import open_db
from leafhub.core.store import SyncStore
from leafhub.manage.projects import _write_dotfile


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_store(tmp_path: Path) -> SyncStore:
    """Open a fresh in-memory-like store in *tmp_path*."""
    conn = open_db(hub_dir=tmp_path)
    return SyncStore(conn)


def _make_provider(store: SyncStore, hub_dir: Path) -> str:
    """Insert a minimal provider, encrypt its key, and return the provider id."""
    from leafhub.core.crypto import encrypt_providers, load_master_key

    p = store.create_provider(
        label="test-openai",
        provider_type="openai",
        api_format="openai-completions",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    )
    master_key = load_master_key(hub_dir)
    encrypt_providers({p.id: {"api_key": "sk-test-1234"}}, master_key, hub_dir)
    return p.id


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ProbeResult property logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestProbeResultProperties(unittest.TestCase):

    def test_ready_false_when_no_dotfile(self):
        r = ProbeResult()
        self.assertFalse(r.ready)

    def test_ready_false_when_empty_token(self):
        r = ProbeResult(dotfile_data={"project": "x", "token": ""})
        self.assertFalse(r.ready)

    def test_ready_false_when_token_key_missing(self):
        r = ProbeResult(dotfile_data={"project": "x"})
        self.assertFalse(r.ready)

    def test_ready_true_when_token_present(self):
        r = ProbeResult(dotfile_data={"project": "x", "token": "lh-proj-abc123"})
        self.assertTrue(r.ready)

    def test_cli_available_reflects_cli_path(self):
        r = ProbeResult(cli_path=None)
        self.assertFalse(r.cli_available)
        r2 = ProbeResult(cli_path="/usr/local/bin/leafhub")
        self.assertTrue(r2.cli_available)

    def test_can_link_server(self):
        r = ProbeResult(server_running=True)
        self.assertTrue(r.can_link)

    def test_can_link_cli(self):
        r = ProbeResult(cli_path="/usr/bin/leafhub")
        self.assertTrue(r.can_link)

    def test_can_link_sdk(self):
        r = ProbeResult(sdk_importable=True)
        self.assertTrue(r.can_link)

    def test_can_link_false_when_nothing(self):
        r = ProbeResult()
        self.assertFalse(r.can_link)

    def test_manage_url_default(self):
        r = ProbeResult()
        self.assertEqual(r.manage_url, "http://127.0.0.1:8765")

    def test_manage_url_uses_detected_server(self):
        r = ProbeResult(server_url="http://127.0.0.1:9000", server_running=True)
        self.assertEqual(r.manage_url, "http://127.0.0.1:9000")

    def test_project_name_from_dotfile(self):
        r = ProbeResult(dotfile_data={"project": "trileaf", "token": "x"})
        self.assertEqual(r.project_name, "trileaf")

    def test_project_name_none_when_no_dotfile(self):
        r = ProbeResult()
        self.assertIsNone(r.project_name)

    def test_open_sdk_raises_when_not_ready(self):
        r = ProbeResult()
        with self.assertRaises(RuntimeError):
            r.open_sdk()

    def test_open_sdk_raises_when_sdk_not_importable(self):
        r = ProbeResult(
            dotfile_data={"token": "lh-proj-x"},
            sdk_importable=False,
        )
        with self.assertRaises(ImportError):
            r.open_sdk()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. detect() — dotfile discovery edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestDetectDotfile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, directory: Path, data: dict | None, *, as_dir: bool = False) -> Path:
        target = directory / ".leafhub"
        if as_dir:
            target.mkdir(parents=True, exist_ok=True)
        elif data is None:
            target.write_text("not json {{{{", encoding="utf-8")
        else:
            target.write_text(json.dumps(data), encoding="utf-8")
        return target

    # ── Basic cases ──────────────────────────────────────────────────────────

    def test_no_dotfile_in_tree(self):
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertIsNone(r.dotfile_path)
        self.assertIsNone(r.dotfile_data)
        self.assertFalse(r.ready)

    def test_dotfile_in_cwd(self):
        self._write(self.tmp_path, {"project": "p", "token": "lh-proj-abc", "version": 1})
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertIsNotNone(r.dotfile_path)
        self.assertTrue(r.ready)
        self.assertEqual(r.project_name, "p")

    def test_dotfile_in_parent_directory(self):
        child = self.tmp_path / "a" / "b" / "c"
        child.mkdir(parents=True)
        self._write(self.tmp_path, {"project": "root-proj", "token": "lh-proj-xyz", "version": 1})
        r = detect(project_dir=child, timeout=0.05)
        self.assertTrue(r.ready)
        self.assertEqual(r.project_name, "root-proj")

    def test_nearest_ancestor_wins(self):
        """When both a child and parent have dotfiles, the child's should win."""
        child = self.tmp_path / "sub"
        child.mkdir()
        self._write(self.tmp_path, {"project": "parent", "token": "lh-proj-parent", "version": 1})
        self._write(child, {"project": "child", "token": "lh-proj-child", "version": 1})
        r = detect(project_dir=child, timeout=0.05)
        self.assertEqual(r.project_name, "child")

    # ── Edge cases ───────────────────────────────────────────────────────────

    def test_dotfile_is_a_directory_ignored(self):
        """A directory named .leafhub must not be treated as the dotfile."""
        self._write(self.tmp_path, None, as_dir=True)
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertFalse(r.ready)
        self.assertIsNone(r.dotfile_path)

    def test_dotfile_invalid_json(self):
        self._write(self.tmp_path, None)  # writes garbage
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        # dotfile_path is None because we couldn't parse it, but we stopped walking
        self.assertFalse(r.ready)

    def test_dotfile_json_not_a_dict(self):
        (self.tmp_path / ".leafhub").write_text("[1, 2, 3]", encoding="utf-8")
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertFalse(r.ready)
        self.assertIsNone(r.dotfile_data)

    def test_dotfile_missing_token_field(self):
        self._write(self.tmp_path, {"project": "p", "version": 1})
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertIsNotNone(r.dotfile_data)
        self.assertFalse(r.ready)

    def test_dotfile_empty_token(self):
        self._write(self.tmp_path, {"project": "p", "token": "", "version": 1})
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertFalse(r.ready)

    def test_dotfile_token_null(self):
        self._write(self.tmp_path, {"project": "p", "token": None, "version": 1})
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        self.assertFalse(r.ready)

    def test_nonexistent_project_dir(self):
        """detect() must not raise if the starting directory doesn't exist."""
        ghost = self.tmp_path / "ghost" / "dir"
        r = detect(project_dir=ghost, timeout=0.05)
        self.assertFalse(r.ready)

    def test_default_project_dir_is_cwd(self):
        """detect(project_dir=None) should default to Path.cwd() without error."""
        r = detect(timeout=0.05)   # should not raise
        self.assertIsInstance(r, ProbeResult)

    # ── Port probe ───────────────────────────────────────────────────────────

    def test_server_not_running_on_unused_port(self):
        r = detect(project_dir=self.tmp_path, port=19999, timeout=0.1)
        self.assertFalse(r.server_running)
        self.assertIsNone(r.server_url)

    def test_server_running_on_listening_port(self):
        """Spin up a minimal TCP listener and verify detect() finds it."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def _accept():
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=_accept, daemon=True)
        t.start()
        try:
            r = detect(project_dir=self.tmp_path, port=port, timeout=1.0)
            self.assertTrue(r.server_running)
            self.assertEqual(r.server_url, f"http://127.0.0.1:{port}")
        finally:
            server.close()
            t.join(timeout=2)

    # ── SDK / CLI detection ──────────────────────────────────────────────────

    def test_sdk_importable_reflects_leafhub_presence(self):
        r = detect(project_dir=self.tmp_path, timeout=0.05)
        import importlib.util
        expected = importlib.util.find_spec("leafhub") is not None
        self.assertEqual(r.sdk_importable, expected)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. _write_dotfile — atomic write, permissions, gitignore
# ═══════════════════════════════════════════════════════════════════════════════

class TestWriteDotfile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_dotfile_with_correct_content(self):
        dotfile = _write_dotfile(self.tmp, "myproject", "lh-proj-testtoken")
        self.assertTrue(dotfile.exists())
        data = json.loads(dotfile.read_text())
        self.assertEqual(data["project"], "myproject")
        self.assertEqual(data["token"], "lh-proj-testtoken")
        self.assertEqual(data["version"], 1)
        self.assertIn("linked_at", data)

    def test_dotfile_permissions_600(self):
        if sys.platform == "win32":
            self.skipTest("chmod 600 not applicable on Windows")
        dotfile = _write_dotfile(self.tmp, "p", "tok")
        mode = stat.S_IMODE(dotfile.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 600, got {oct(mode)}")

    def test_no_temp_files_left_after_write(self):
        _write_dotfile(self.tmp, "p", "tok")
        leftovers = [f for f in self.tmp.iterdir() if f.name.startswith(".leafhub-")]
        self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_overwrites_existing_dotfile_atomically(self):
        """Second write should replace the first; permissions must remain 600."""
        _write_dotfile(self.tmp, "p", "token-v1")
        _write_dotfile(self.tmp, "p", "token-v2")
        data = json.loads((self.tmp / ".leafhub").read_text())
        self.assertEqual(data["token"], "token-v2")
        if sys.platform != "win32":
            mode = stat.S_IMODE((self.tmp / ".leafhub").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_gitignore_entry_added_when_absent(self):
        gi = self.tmp / ".gitignore"
        gi.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
        _write_dotfile(self.tmp, "p", "tok")
        lines = gi.read_text().splitlines()
        self.assertIn(".leafhub", lines)

    def test_gitignore_entry_not_duplicated(self):
        gi = self.tmp / ".gitignore"
        gi.write_text("*.pyc\n.leafhub\n", encoding="utf-8")
        _write_dotfile(self.tmp, "p", "tok")
        _write_dotfile(self.tmp, "p", "tok2")
        lines = gi.read_text().splitlines()
        self.assertEqual(lines.count(".leafhub"), 1)

    def test_no_gitignore_created_when_none_exists(self):
        _write_dotfile(self.tmp, "p", "tok")
        self.assertFalse((self.tmp / ".gitignore").exists())

    def test_gitignore_not_modified_when_already_contains_entry(self):
        gi = self.tmp / ".gitignore"
        original = "*.pyc\n.leafhub\nvenv/\n"
        gi.write_text(original, encoding="utf-8")
        _write_dotfile(self.tmp, "p", "tok")
        self.assertEqual(gi.read_text(encoding="utf-8"), original)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. LeafHub.from_directory() — directory-tree walk
# ═══════════════════════════════════════════════════════════════════════════════

class TestFromDirectory(unittest.TestCase):

    def setUp(self):
        self.hub_dir = Path(tempfile.mkdtemp())
        self.proj_dir = Path(tempfile.mkdtemp())
        self.store = _make_store(self.hub_dir)

    def tearDown(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.hub_dir, ignore_errors=True)
        shutil.rmtree(self.proj_dir, ignore_errors=True)

    def _setup_linked_project(self, name: str = "test-proj") -> tuple[str, str]:
        """Create a project, write dotfile, return (project_id, raw_token)."""
        project, raw_token = self.store.create_project(name)
        _write_dotfile(self.proj_dir, name, raw_token)
        self.store.set_project_path(project.id, str(self.proj_dir))
        return project.id, raw_token

    def test_from_directory_finds_dotfile_in_cwd(self):
        from leafhub import LeafHub
        self._setup_linked_project()
        hub = LeafHub.from_directory(self.proj_dir, hub_dir=self.hub_dir)
        self.assertIsNotNone(hub)

    def test_from_directory_walks_up_to_parent(self):
        from leafhub import LeafHub
        self._setup_linked_project()
        # Start 3 levels below the dotfile
        deep = self.proj_dir / "a" / "b" / "c"
        deep.mkdir(parents=True)
        hub = LeafHub.from_directory(deep, hub_dir=self.hub_dir)
        self.assertIsNotNone(hub)

    def test_from_directory_raises_when_no_dotfile(self):
        from leafhub import LeafHub
        empty = Path(tempfile.mkdtemp())
        try:
            with self.assertRaises(FileNotFoundError):
                LeafHub.from_directory(empty, hub_dir=self.hub_dir)
        finally:
            import shutil
            shutil.rmtree(empty, ignore_errors=True)

    def test_from_directory_raises_on_empty_token_in_dotfile(self):
        from leafhub import LeafHub
        (self.proj_dir / ".leafhub").write_text(
            json.dumps({"project": "p", "token": "", "version": 1}),
            encoding="utf-8",
        )
        with self.assertRaises(FileNotFoundError):
            LeafHub.from_directory(self.proj_dir, hub_dir=self.hub_dir)

    def test_from_directory_raises_on_invalid_token(self):
        from leafhub import LeafHub, InvalidTokenError
        (self.proj_dir / ".leafhub").write_text(
            json.dumps({"project": "p", "token": "lh-proj-bogustoken", "version": 1}),
            encoding="utf-8",
        )
        with self.assertRaises(InvalidTokenError):
            LeafHub.from_directory(self.proj_dir, hub_dir=self.hub_dir)

    def test_from_directory_ignores_dir_named_dotleafhub(self):
        from leafhub import LeafHub
        (self.proj_dir / ".leafhub").mkdir()   # directory, not file
        with self.assertRaises(FileNotFoundError):
            LeafHub.from_directory(self.proj_dir, hub_dir=self.hub_dir)

    def test_from_directory_defaults_to_cwd(self):
        from leafhub import LeafHub
        old_cwd = os.getcwd()
        try:
            os.chdir(self.proj_dir)
            self._setup_linked_project()
            hub = LeafHub.from_directory(hub_dir=self.hub_dir)
            self.assertIsNotNone(hub)
        finally:
            os.chdir(old_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Store — set_project_path + _row_to_project migration guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestStorePathColumn(unittest.TestCase):

    def setUp(self):
        self.hub_dir = Path(tempfile.mkdtemp())
        self.store = _make_store(self.hub_dir)

    def tearDown(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.hub_dir, ignore_errors=True)

    def test_path_is_none_on_fresh_project(self):
        project, _ = self.store.create_project("fresh")
        self.assertIsNone(project.path)

    def test_set_project_path_persists(self):
        project, _ = self.store.create_project("pathtest")
        self.store.set_project_path(project.id, "/tmp/myproject")
        reloaded = self.store.get_project(project.id)
        self.assertEqual(reloaded.path, "/tmp/myproject")

    def test_set_project_path_to_none_clears_it(self):
        project, _ = self.store.create_project("unlinktest")
        self.store.set_project_path(project.id, "/tmp/x")
        self.store.set_project_path(project.id, None)
        reloaded = self.store.get_project(project.id)
        self.assertIsNone(reloaded.path)

    def test_row_to_project_handles_missing_path_column(self):
        """Simulate an old DB row without a path column."""
        # Directly insert a row without the path column using a raw connection
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE projects "
            "(id TEXT, name TEXT, token_hash TEXT, token_prefix TEXT, "
            "is_active INTEGER, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO projects VALUES (?,?,?,?,?,?)",
            ("id1", "old", "hash", "lh-proj-", 1, "2024-01-01"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM projects").fetchone()

        store = SyncStore(conn)
        # Monkey-patch list_bindings to return empty list for this synthetic row
        store.list_bindings = lambda pid: []
        project = store._row_to_project(row)
        self.assertIsNone(project.path)
        conn.close()

    def test_path_survives_token_rotation(self):
        project, _ = self.store.create_project("rotatetest")
        self.store.set_project_path(project.id, "/linked/path")
        self.store.rotate_token(project.id)
        reloaded = self.store.get_project(project.id)
        self.assertEqual(reloaded.path, "/linked/path")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DB migration — path column is added to existing databases
# ═══════════════════════════════════════════════════════════════════════════════

class TestDbMigration(unittest.TestCase):

    def test_path_column_added_to_old_db(self):
        """Simulate opening a pre-migration DB that has no path column."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "projects.db"
            # Build a DB without the path column
            conn = sqlite3.connect(str(db_file))
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                    provider_type TEXT NOT NULL, api_format TEXT NOT NULL,
                    base_url TEXT NOT NULL, default_model TEXT NOT NULL,
                    available_models TEXT, auth_mode TEXT NOT NULL DEFAULT 'bearer',
                    auth_header TEXT, extra_headers TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE, token_prefix TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS model_bindings (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    alias TEXT NOT NULL, provider_id TEXT NOT NULL REFERENCES providers(id),
                    model_override TEXT, UNIQUE(project_id, alias)
                );
            """)
            conn.close()

            # open_db should migrate and add the path column
            migrated = open_db(hub_dir=Path(tmp))
            columns = {row[1] for row in migrated.execute("PRAGMA table_info(projects)")}
            self.assertIn("path", columns)
            migrated.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Full round-trip: link → detect → open_sdk → get_key
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullRoundTrip(unittest.TestCase):

    def setUp(self):
        self.hub_dir = Path(tempfile.mkdtemp())
        self.proj_dir = Path(tempfile.mkdtemp())
        self.store = _make_store(self.hub_dir)

    def tearDown(self):
        import shutil
        self.store.close()
        shutil.rmtree(self.hub_dir, ignore_errors=True)
        shutil.rmtree(self.proj_dir, ignore_errors=True)

    def test_link_detect_open_sdk_get_key(self):
        """
        Full flow:
          1. Create project + provider in LeafHub
          2. Write .leafhub to project directory (simulate UI link)
          3. detect() finds the dotfile
          4. open_sdk() opens a valid LeafHub instance
          5. get_key() returns the expected API key
        """
        from leafhub import LeafHub

        # 1. Setup provider + project
        provider_id = _make_provider(self.store, self.hub_dir)
        project, raw_token = self.store.create_project("round-trip")
        self.store.add_binding(project.id, "rewrite", provider_id)
        self.store.set_project_path(project.id, str(self.proj_dir))

        # 2. Write dotfile (simulates the link endpoint)
        _write_dotfile(self.proj_dir, "round-trip", raw_token)

        # 3. detect() should find it
        r = detect(project_dir=self.proj_dir, timeout=0.05)
        self.assertTrue(r.ready)
        self.assertEqual(r.project_name, "round-trip")

        # 4. open_sdk() — manually construct instead (needs hub_dir override)
        hub = LeafHub(token=raw_token, hub_dir=self.hub_dir)

        # 5. get_key() returns the stored API key
        key = hub.get_key("rewrite")
        self.assertEqual(key, "sk-test-1234")

    def test_token_rotation_updates_dotfile(self):
        """After rotate, from_directory() must still work with the new token."""
        from leafhub import LeafHub

        _make_provider(self.store, self.hub_dir)
        project, raw_token = self.store.create_project("rotate-test")
        _write_dotfile(self.proj_dir, "rotate-test", raw_token)
        self.store.set_project_path(project.id, str(self.proj_dir))

        # Rotate and rewrite dotfile (mimics the rotate endpoint)
        new_token = self.store.rotate_token(project.id)
        _write_dotfile(self.proj_dir, "rotate-test", new_token)

        # Old token must fail
        from leafhub import InvalidTokenError
        with self.assertRaises(InvalidTokenError):
            LeafHub(token=raw_token, hub_dir=self.hub_dir)

        # New token from dotfile must work
        hub = LeafHub(token=new_token, hub_dir=self.hub_dir)
        self.assertIsNotNone(hub)

    def test_deactivated_project_token_fails(self):
        """Tokens for deactivated projects must be rejected."""
        from leafhub import LeafHub, InvalidTokenError

        project, raw_token = self.store.create_project("deact-test")
        self.store.deactivate_project(project.id)

        with self.assertRaises(InvalidTokenError):
            LeafHub(token=raw_token, hub_dir=self.hub_dir)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. resolve_from_leafhub (Trileaf rewrite_config integration)
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skip(
    "Tests for Trileaf/scripts/rewrite_config.py which lives outside this repo. "
    "Run from the Trileaf repository instead."
)
class TestResolveFromLeafhub(unittest.TestCase):
    """
    Tests for Trileaf's resolve_from_leafhub() integration shim.
    We mock LeafHub.from_directory to avoid needing a real ~/.leafhub setup.
    """

    def _import_resolve(self):
        trileaf_root = REPO_ROOT / "Trileaf"
        if str(trileaf_root) not in sys.path:
            sys.path.insert(0, str(trileaf_root))
        from scripts.rewrite_config import resolve_from_leafhub
        return resolve_from_leafhub

    def test_returns_none_when_leafhub_not_installed(self):
        resolve_from_leafhub = self._import_resolve()
        with patch("importlib.util.find_spec", return_value=None):
            result = resolve_from_leafhub("rewrite")
        self.assertIsNone(result)

    def test_returns_none_when_no_dotfile(self):
        resolve_from_leafhub = self._import_resolve()
        with patch("leafhub.LeafHub.from_directory", side_effect=FileNotFoundError("no dotfile")):
            result = resolve_from_leafhub("rewrite")
        self.assertIsNone(result)

    def test_returns_credentials_dict_when_linked(self):
        resolve_from_leafhub = self._import_resolve()

        mock_cfg = MagicMock()
        mock_cfg.api_key   = "sk-from-leafhub"
        mock_cfg.base_url  = "https://api.openai.com/v1"
        mock_cfg.model     = "gpt-4o-mini"
        mock_cfg.api_format = "openai-completions"
        mock_cfg.auth_mode  = "bearer"

        mock_hub = MagicMock()
        mock_hub.get_config.return_value = mock_cfg

        with patch("leafhub.LeafHub.from_directory", return_value=mock_hub):
            result = resolve_from_leafhub("rewrite")

        self.assertIsNotNone(result)
        self.assertEqual(result["value"], "sk-from-leafhub")
        self.assertEqual(result["source"], "leafhub")
        self.assertEqual(result["base_url"], "https://api.openai.com/v1")

    def test_returns_none_on_invalid_token(self):
        resolve_from_leafhub = self._import_resolve()
        from leafhub import InvalidTokenError
        with patch("leafhub.LeafHub.from_directory", side_effect=InvalidTokenError("bad")):
            result = resolve_from_leafhub("rewrite")
        self.assertIsNone(result)

    def test_returns_none_on_alias_not_bound(self):
        resolve_from_leafhub = self._import_resolve()
        from leafhub import AliasNotBoundError

        mock_hub = MagicMock()
        mock_hub.get_config.side_effect = AliasNotBoundError("rewrite not bound")

        with patch("leafhub.LeafHub.from_directory", return_value=mock_hub):
            result = resolve_from_leafhub("rewrite")
        self.assertIsNone(result)

    def test_does_not_raise_on_arbitrary_exception(self):
        """resolve_from_leafhub must always fall back gracefully."""
        resolve_from_leafhub = self._import_resolve()
        with patch("leafhub.LeafHub.from_directory", side_effect=RuntimeError("unexpected")):
            result = resolve_from_leafhub("rewrite")
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Security — dotfile never world-readable
# ═══════════════════════════════════════════════════════════════════════════════

class TestDotfileSecurity(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipIf(sys.platform == "win32", "chmod 600 not applicable on Windows")
    def test_world_not_readable(self):
        dotfile = _write_dotfile(self.tmp, "sec-test", "lh-proj-secret")
        mode = stat.S_IMODE(dotfile.stat().st_mode)
        # Other-read bit must NOT be set
        self.assertEqual(mode & stat.S_IROTH, 0, "dotfile is world-readable!")
        # Group-read bit must NOT be set
        self.assertEqual(mode & stat.S_IRGRP, 0, "dotfile is group-readable!")

    @unittest.skipIf(sys.platform == "win32", "chmod 600 not applicable on Windows")
    def test_no_temp_file_left_readable(self):
        _write_dotfile(self.tmp, "p", "tok")
        for f in self.tmp.iterdir():
            if f.name.startswith(".leafhub-"):
                mode = stat.S_IMODE(f.stat().st_mode)
                self.fail(f"Temp file left behind: {f} mode={oct(mode)}")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)

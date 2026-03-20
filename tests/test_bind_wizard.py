"""
Tests for the interactive provider-binding wizard.

Covers:
  CLI wizard (_interactive_bind_wizard):
    1.  Non-TTY stdin → wizard skips silently
    2.  User picks an existing provider by number → binding created
    3.  User skips ('s') → no binding, helpful message
    4.  Empty input (Enter) when providers exist → treated as skip
    5.  Invalid numeric choice (out of range) → binding skipped
    6.  Invalid non-numeric choice → binding skipped
    7.  User picks 'n' (new provider), cancels at label step → no binding
    8.  Existing provider with same label → returned without re-creating
    9.  Multiple bindings in one session (loop, then exit)
   10.  store.add_binding failure → error printed, wizard exits cleanly
   11.  cmd_project_create: store always closed even when wizard raises KeyboardInterrupt
   12.  cmd_project_link:   store always closed even when wizard raises KeyboardInterrupt
   13.  Same-name projects: wizard binds the correct project (by ID)
   14.  No providers + user says 'n' to adding one → skip message, no hang

  probe._bind_wizard_rest:
   15.  Non-TTY stdin → wizard skips silently
   16.  User picks existing provider → PUT /admin/projects/{id} called with merged bindings
   17.  Provider list fetch fails → wizard returns without error
   18.  PUT fails after user selects provider → error printed, wizard exits
   19.  _prompt_add_provider_rest: non-TTY → returns None immediately

All tests use temp directories; ~/.leafhub/ is never touched.
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_ROOT = Path(__file__).parent.parent
_SRC  = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from leafhub.core.db    import open_db
from leafhub.core.store import SyncStore
from leafhub.core.crypto import encrypt_providers, decrypt_providers


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _make_hub(tmp: str) -> tuple[Path, bytes, SyncStore]:
    hub = Path(tmp) / ".leafhub"
    hub.mkdir(parents=True)
    key   = secrets.token_bytes(32)
    store = SyncStore(open_db(hub))
    return hub, key, store


def _with_master_key(key: bytes):
    return patch.dict(os.environ, {"LEAFHUB_MASTER_KEY": base64.b64encode(key).decode()})


def _add_provider(store: SyncStore, hub: Path, key: bytes,
                  label: str = "OpenAI", api_key: str = "sk-test") -> str:
    p  = store.create_provider(
        label=label,
        provider_type="custom",
        api_format="openai-completions",
        base_url="https://api.example.com/v1",
        default_model="gpt-test",
        available_models=[],
    )
    ks = decrypt_providers(key, hub)
    ks[p.id] = {"api_key": api_key}
    encrypt_providers(ks, key, hub)
    return p.id


def _simulate_input(responses: list[str]):
    """Return a patch for builtins.input that yields canned responses."""
    it = iter(responses)
    return patch("builtins.input", side_effect=lambda _prompt="": next(it))


def _isatty_true():
    """Patch sys.stdin.isatty to return True."""
    return patch("sys.stdin.isatty", return_value=True)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI wizard tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestInteractiveBindWizard(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub, self.key, self.store = _make_hub(self._tmp.name)
        self._key_patch = _with_master_key(self.key)
        self._key_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    def _make_project(self, name: str = "myproject"):
        proj, _ = self.store.create_project(name)
        return proj

    # ── import target ──────────────────────────────────────────────────────────

    @property
    def _wizard(self):
        from leafhub.cli import _interactive_bind_wizard
        return _interactive_bind_wizard

    # ── 1. Non-TTY → skips silently ───────────────────────────────────────────

    def test_01_non_tty_skips_silently(self):
        proj = self._make_project()
        with patch("sys.stdin.isatty", return_value=False):
            # Should return immediately without calling input()
            with patch("builtins.input", side_effect=AssertionError("input() called in non-TTY")):
                self._wizard(self.store, self.hub, proj.id, "myproject")
        # No binding should have been created
        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 2. Pick existing provider by number ───────────────────────────────────

    def test_02_pick_existing_provider(self):
        pid   = _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj  = self._make_project()
        prov  = self.store.find_provider_by_label("OpenAI")

        with _isatty_true(), _simulate_input(["1", "chat", "n"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(len(p.bindings), 1)
        self.assertEqual(p.bindings[0].alias,       "chat")
        self.assertEqual(p.bindings[0].provider_id, prov.id)

    # ── 3. User skips ('s') ───────────────────────────────────────────────────

    def test_03_skip_s_creates_no_binding(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj = self._make_project()

        with _isatty_true(), _simulate_input(["s"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 4. Empty Enter when providers exist → treated as skip ─────────────────

    def test_04_empty_enter_skips(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj = self._make_project()

        with _isatty_true(), _simulate_input([""]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 5. Invalid numeric choice out of range ────────────────────────────────

    def test_05_out_of_range_choice_skips(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")  # only 1 provider → index 0
        proj = self._make_project()

        with _isatty_true(), _simulate_input(["99"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 6. Invalid non-numeric choice ─────────────────────────────────────────

    def test_06_garbage_input_skips(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj = self._make_project()

        with _isatty_true(), _simulate_input(["foobar"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 7. 'n' for new provider but cancel at label step ─────────────────────

    def test_07_new_provider_cancel_at_label(self):
        proj = self._make_project()  # no providers at all

        # No providers → asks Y/n to add one → yes → label input → empty → cancel
        with _isatty_true(), _simulate_input(["y", ""]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])

    # ── 8. Existing provider with same label reused ───────────────────────────

    def test_08_existing_label_reused_in_new_provider_flow(self):
        """_prompt_new_provider: if label already exists, returns existing provider."""
        from leafhub.cli import _prompt_new_provider

        pid  = _add_provider(self.store, self.hub, self.key, "Dup")
        prov = self.store.find_provider_by_label("Dup")

        with _isatty_true(), _simulate_input(["Dup"]):
            result = _prompt_new_provider(self.store, self.hub)

        self.assertIsNotNone(result)
        self.assertEqual(result.id, prov.id)

    # ── 9. Multiple bindings in one session ───────────────────────────────────

    def test_09_multiple_bindings_in_one_session(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI",    "sk-a")
        _add_provider(self.store, self.hub, self.key, "Anthropic", "sk-b")
        proj = self._make_project()

        # First: pick provider 1 → alias "chat" → add another? yes
        # Second: pick provider 2 → alias "embed" → add another? no
        with _isatty_true(), _simulate_input(["1", "chat", "y", "2", "embed", "n"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        aliases = {b.alias for b in p.bindings}
        self.assertEqual(aliases, {"chat", "embed"})

    # ── 10. add_binding failure → error printed, wizard exits cleanly ─────────

    def test_10_add_binding_failure_is_handled(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj = self._make_project()

        bad_store = MagicMock(wraps=self.store)
        bad_store.add_binding.side_effect = RuntimeError("DB locked")

        printed = []
        with _isatty_true(), \
             _simulate_input(["1", "chat"]), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            self._wizard(bad_store, self.hub, proj.id, "myproject")

        # Error message must mention the failure
        self.assertTrue(any("Binding failed" in line for line in printed))
        # And no more input() calls happened (wizard exited after the error)

    # ── 13. Same-name projects: correct project is bound by ID ───────────────

    def test_13_same_name_projects_bound_by_id(self):
        """With two identically-named projects, wizard must bind the right one."""
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        proj_a, _ = self.store.create_project("myapp")
        proj_b, _ = self.store.create_project("myapp")   # same name, different ID

        with _isatty_true(), _simulate_input(["1", "chat", "n"]):
            self._wizard(self.store, self.hub, proj_b.id, "myapp")

        a = self.store.get_project(proj_a.id)
        b = self.store.get_project(proj_b.id)

        self.assertEqual(a.bindings, [],     "proj_a must have no bindings")
        self.assertEqual(len(b.bindings), 1, "proj_b must have exactly 1 binding")
        self.assertEqual(b.bindings[0].alias, "chat")

    # ── 14. No providers + user declines to add ───────────────────────────────

    def test_14_no_providers_user_declines(self):
        proj = self._make_project()

        with _isatty_true(), _simulate_input(["n"]):
            self._wizard(self.store, self.hub, proj.id, "myproject")

        p = self.store.get_project(proj.id)
        self.assertEqual(p.bindings, [])


# ═══════════════════════════════════════════════════════════════════════════════
# cmd_project_create / cmd_project_link — store always closed
# ═══════════════════════════════════════════════════════════════════════════════

class TestStoreClosed(unittest.TestCase):
    """
    Verify that store.close() is always called even when the wizard raises.
    The cmd_* functions now wrap the wizard in try/finally.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub_dir = Path(self._tmp.name) / ".leafhub"
        self.hub_dir.mkdir(parents=True)
        self.key = secrets.token_bytes(32)
        self._key_patch = _with_master_key(self.key)
        self._key_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._tmp.cleanup()

    def _args(self, **kwargs):
        defaults = dict(project_name="myapp", path=None, no_probe=True)
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def test_11_create_store_closed_on_wizard_interrupt(self):
        """store.close() is called even when wizard raises KeyboardInterrupt."""
        closed = []

        real_open_store = None
        from leafhub import cli as cli_mod

        def fake_open_store(hub_dir=None):
            nonlocal real_open_store
            # open a real store so create_project works
            from leafhub.core.db import open_db
            from leafhub.core.store import SyncStore
            from leafhub.core import default_hub_dir
            resolved = hub_dir or self.hub_dir
            conn = open_db(resolved)
            store = SyncStore(conn)
            orig_close = store.close
            def close_spy():
                closed.append(True)
                orig_close()
            store.close = close_spy
            return store, resolved

        with patch.object(cli_mod, "_open_store", fake_open_store), \
             patch.object(cli_mod, "_interactive_bind_wizard",
                          side_effect=KeyboardInterrupt), \
             patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                cli_mod.cmd_project_create(self._args())

        self.assertTrue(closed, "store.close() was not called after KeyboardInterrupt")

    def test_12_link_store_closed_on_wizard_interrupt(self):
        """store.close() is called even when wizard raises during project link."""
        # First create a project to link
        from leafhub.core.db import open_db
        from leafhub.core.store import SyncStore
        setup_store = SyncStore(open_db(self.hub_dir))
        setup_store.create_project("myapp")
        setup_store.close()

        link_dir = Path(self._tmp.name) / "project_dir"
        link_dir.mkdir()

        closed = []
        from leafhub import cli as cli_mod

        def fake_open_store(hub_dir=None):
            from leafhub.core.db import open_db
            from leafhub.core.store import SyncStore
            resolved = hub_dir or self.hub_dir
            conn = open_db(resolved)
            store = SyncStore(conn)
            orig_close = store.close
            def close_spy():
                closed.append(True)
                orig_close()
            store.close = close_spy
            return store, resolved

        args = self._args(path=str(link_dir))

        with patch.object(cli_mod, "_open_store", fake_open_store), \
             patch.object(cli_mod, "_interactive_bind_wizard",
                          side_effect=KeyboardInterrupt), \
             patch("leafhub.manage.projects._write_dotfile", return_value=None), \
             patch("leafhub.manage.projects._copy_probe_to_project", return_value=None), \
             patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                cli_mod.cmd_project_link(args)

        self.assertTrue(closed, "store.close() was not called after KeyboardInterrupt in link")


# ═══════════════════════════════════════════════════════════════════════════════
# probe.py wizard helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestBindWizardRest(unittest.TestCase):

    # ── 15. Non-TTY → skips silently ─────────────────────────────────────────

    def test_15_bind_wizard_rest_non_tty_skips(self):
        from leafhub.probe import _bind_wizard_rest

        with patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen",
                   side_effect=AssertionError("HTTP called in non-TTY")):
            _bind_wizard_rest("http://127.0.0.1:8765", "proj-id", "myapp", 5.0)
        # No exception = pass

    # ── 16. Pick provider → PUT with merged bindings ──────────────────────────

    def test_16_pick_provider_sends_put(self):
        from leafhub.probe import _bind_wizard_rest

        providers_resp = {"data": [{"id": "prov-1", "label": "OpenAI", "api_format": "openai-completions"}]}
        project_resp   = {"bindings": []}   # no existing bindings
        put_body_captured = []

        def fake_urlopen(req, timeout=None):
            # req is a string URL for GET calls, a Request object for PUT
            import urllib.request as _ur
            url    = req if isinstance(req, str) else req.get_full_url()
            method = "GET" if isinstance(req, str) else req.get_method()

            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.__exit__.return_value  = False

            if method == "GET" and "/admin/providers" in url:
                resp.read.return_value = json.dumps(providers_resp).encode()
            elif method == "GET" and "/admin/projects/" in url:
                resp.read.return_value = json.dumps(project_resp).encode()
            elif method == "PUT":
                put_body_captured.append(json.loads(req.data.decode()))
                resp.read.return_value = json.dumps({"id": "proj-1"}).encode()
            return resp

        with patch("sys.stdin.isatty", return_value=True), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             _simulate_input(["1", "chat", "n"]):
            _bind_wizard_rest("http://127.0.0.1:8765", "proj-1", "myapp", 5.0)

        self.assertEqual(len(put_body_captured), 1)
        bindings = put_body_captured[0]["bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["alias"],       "chat")
        self.assertEqual(bindings[0]["provider_id"], "prov-1")

    # ── 17. Provider list fetch fails → silent return ─────────────────────────

    def test_17_provider_fetch_failure_returns_silently(self):
        from leafhub.probe import _bind_wizard_rest
        import urllib.error

        with patch("sys.stdin.isatty", return_value=True), \
             patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection refused")), \
             patch("builtins.input",
                   side_effect=AssertionError("input() called after fetch failure")):
            _bind_wizard_rest("http://127.0.0.1:8765", "proj-id", "myapp", 5.0)
        # No exception = pass

    # ── 18. PUT fails after user selects provider ─────────────────────────────

    def test_18_put_failure_prints_error(self):
        from leafhub.probe import _bind_wizard_rest
        import urllib.error

        providers_resp = {"data": [{"id": "prov-1", "label": "OpenAI", "api_format": "openai-completions"}]}
        project_resp   = {"bindings": []}

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            url    = req if isinstance(req, str) else req.get_full_url()
            method = "GET" if isinstance(req, str) else req.get_method()

            if method == "PUT":
                raise urllib.error.URLError("write failed")

            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.__exit__.return_value  = False
            if "/admin/providers" in url:
                resp.read.return_value = json.dumps(providers_resp).encode()
            else:
                resp.read.return_value = json.dumps(project_resp).encode()
            return resp

        printed = []
        with patch("sys.stdin.isatty", return_value=True), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))), \
             _simulate_input(["1", "chat"]):
            _bind_wizard_rest("http://127.0.0.1:8765", "proj-1", "myapp", 5.0)

        self.assertTrue(any("Binding failed" in line or "failed" in line.lower() for line in printed))

    # ── 19. _prompt_add_provider_rest: non-TTY → returns None ────────────────

    def test_19_prompt_add_provider_rest_non_tty(self):
        from leafhub.probe import _prompt_add_provider_rest

        with patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.input",
                   side_effect=AssertionError("input() called in non-TTY")):
            result = _prompt_add_provider_rest("http://127.0.0.1:8765", 5.0)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)

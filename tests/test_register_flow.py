"""
tests/test_register_flow.py

Tests for the Project Integration Standard update:

  1. cmd_register
     a. New project: creates, writes dotfile, copies probe, auto-binds
     b. Existing project: re-links (rotates token, updates path)
     c. Headless + no providers: skips wizard, exits cleanly (project still linked)
     d. Headless + one provider: auto-binds without prompts
     e. Multiple providers (headless): picks first automatically

  2. cmd_shell_helper
     a. Reads from package data (importlib.resources)
     b. Falls back to filesystem when package data unavailable

  3. project create --if-not-exists
     a. New project: creates normally
     b. Existing project with --path: re-links silently (no duplicate)

  4. project create --yes
     a. Skips interactive binding wizard

  5. provider list --json / status --json
     a. provider list --json returns valid JSON array
     b. status --json returns object with providers / projects / bound_projects / ready

  6. _cleanup_installer_registration
     a. Removes symlinks in ~/.local/bin/ pointing into project_dir
     b. Strips venv PATH lines from shell RC files
     c. Handles project_dir containing symlink components (resolved comparison)
     d. Does NOT remove non-symlink entries in ~/.local/bin/
     e. Silently skips when ~/.local/bin/ does not exist

All tests use temp directories; ~/.leafhub/ and ~/.local/ are never touched.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).parent.parent
_SRC  = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from leafhub.core.db     import open_db
from leafhub.core.store  import SyncStore
from leafhub.core.crypto import encrypt_providers, decrypt_providers


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _make_hub(tmp_path: Path) -> tuple[Path, bytes, SyncStore]:
    hub = tmp_path / ".leafhub"
    hub.mkdir(parents=True)
    key   = secrets.token_bytes(32)
    store = SyncStore(open_db(hub))
    return hub, key, store


def _with_master_key(key: bytes):
    return patch.dict(os.environ, {"LEAFHUB_MASTER_KEY": base64.b64encode(key).decode()})


def _with_hub_dir(hub: Path):
    # default_hub_dir() uses Path.home()/.leafhub — patch it at the source
    # so cli._open_store() writes to our temp hub, not the real ~/.leafhub/.
    return patch("leafhub.core.default_hub_dir", return_value=hub)


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


def _make_args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. cmd_register
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdRegister(unittest.TestCase):

    def setUp(self):
        self._tmp      = tempfile.TemporaryDirectory()
        self.tmp_path  = Path(self._tmp.name)
        self.hub, self.key, self.store = _make_hub(self.tmp_path)
        self.proj_dir  = self.tmp_path / "myproject"
        self.proj_dir.mkdir()
        self._key_patch = _with_master_key(self.key)
        self._hub_patch = _with_hub_dir(self.hub)
        self._key_patch.start()
        self._hub_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._hub_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────────────

    def _run_register(self, name="my-app", path=None, alias="default",
                      headless=True):
        from leafhub.cli import cmd_register
        args = _make_args(
            project_name=name,
            path=str(path or self.proj_dir),
            alias=alias,
            headless=headless,
        )
        with patch("leafhub.manage.projects._write_dotfile"), \
             patch("leafhub.manage.projects._distribute_integration_files",
                   return_value=[]):
            cmd_register(args)

    def _reload_store(self) -> SyncStore:
        """Open a fresh store to verify persisted state."""
        return SyncStore(open_db(self.hub))

    # ── 1a. New project ────────────────────────────────────────────────────────

    def test_1a_new_project_is_created(self):
        self._run_register("fresh-app")
        s = self._reload_store()
        try:
            p = s.find_project_by_name("fresh-app")
            self.assertIsNotNone(p, "project should have been created")
            self.assertEqual(p.name, "fresh-app")
        finally:
            s.close()

    def test_1a_new_project_path_stored(self):
        self._run_register("path-app")
        s = self._reload_store()
        try:
            p = s.find_project_by_name("path-app")
            self.assertEqual(str(p.path), str(self.proj_dir))
        finally:
            s.close()

    def test_1a_dotfile_and_integration_files_written(self):
        """_write_dotfile and _distribute_integration_files must both be called for new projects."""
        from leafhub.cli import cmd_register
        args = _make_args(project_name="dotfile-app",
                          path=str(self.proj_dir), alias="default", headless=True)
        with patch("leafhub.manage.projects._write_dotfile") as mock_dotfile, \
             patch("leafhub.manage.projects._distribute_integration_files",
                   return_value=["register.sh", "leafhub_probe.py"]) as mock_dist:
            cmd_register(args)
        mock_dotfile.assert_called_once()
        mock_dist.assert_called_once()

    # ── 1b. Existing project re-links ─────────────────────────────────────────

    def test_1b_relink_does_not_redistribute_files(self):
        """Re-linking an existing project must NOT call _distribute_integration_files."""
        from leafhub.cli import cmd_register
        # First: create the project
        self._run_register("no-redist-app")

        # Second call: re-link should only update .leafhub
        args = _make_args(project_name="no-redist-app",
                          path=str(self.proj_dir), alias="default", headless=True)
        with patch("leafhub.manage.projects._write_dotfile"), \
             patch("leafhub.manage.projects._distribute_integration_files") as mock_dist:
            cmd_register(args)
        mock_dist.assert_not_called()

    def test_1b_existing_project_relinked(self):
        # Create the project first
        self._run_register("relink-app")

        # Create a second project dir and re-run register
        second_dir = self.tmp_path / "second"
        second_dir.mkdir()

        from leafhub.cli import cmd_register
        args = _make_args(project_name="relink-app",
                          path=str(second_dir), alias="default", headless=True)
        with patch("leafhub.manage.projects._write_dotfile"), \
             patch("leafhub.manage.projects._copy_probe_to_project"):
            cmd_register(args)

        # Must still be exactly one project with this name (no duplicate)
        s = self._reload_store()
        try:
            projects = [p for p in s.list_projects() if p.name == "relink-app"]
            self.assertEqual(len(projects), 1, "re-link must not create a duplicate")
            self.assertEqual(str(projects[0].path), str(second_dir))
        finally:
            s.close()

    # ── 1c. Headless + no providers ───────────────────────────────────────────

    def test_1c_headless_no_providers_exits_cleanly(self):
        """With no providers and headless=True, register must not raise and
        must still create the project."""
        self._run_register("no-provider-app", headless=True)
        s = self._reload_store()
        try:
            p = s.find_project_by_name("no-provider-app")
            self.assertIsNotNone(p)
        finally:
            s.close()

    # ── 1d. Headless + one provider: auto-binds ───────────────────────────────

    def test_1d_headless_one_provider_auto_binds(self):
        _add_provider(self.store, self.hub, self.key, "OpenAI")
        self._run_register("bind-app", headless=True)

        s = self._reload_store()
        try:
            p = s.find_project_by_name("bind-app")
            self.assertIsNotNone(p)
            self.assertEqual(len(p.bindings), 1)
            self.assertEqual(p.bindings[0].alias, "default")
        finally:
            s.close()

    # ── 1e. Multiple providers (headless): picks first ────────────────────────

    def test_1e_headless_multiple_providers_picks_first(self):
        pid1 = _add_provider(self.store, self.hub, self.key, "OpenAI")
        _add_provider(self.store, self.hub, self.key, "Anthropic", "ak-test")

        self._run_register("multi-app", headless=True)

        s = self._reload_store()
        try:
            p = s.find_project_by_name("multi-app")
            self.assertEqual(len(p.bindings), 1)
            self.assertEqual(p.bindings[0].provider_id, pid1)
        finally:
            s.close()

    # ── 1f. Custom alias respected ────────────────────────────────────────────

    def test_1f_custom_alias_used(self):
        _add_provider(self.store, self.hub, self.key)
        self._run_register("alias-app", alias="chat", headless=True)

        s = self._reload_store()
        try:
            p = s.find_project_by_name("alias-app")
            self.assertEqual(p.bindings[0].alias, "chat")
        finally:
            s.close()

    # ── 1g. Hardcoded project name is gone ────────────────────────────────────

    def test_1g_no_hardcoded_trileaf_in_output(self):
        """Provider-needed message must use the project name, not 'Trileaf'."""
        from leafhub.cli import cmd_register
        args = _make_args(project_name="demo", path=str(self.proj_dir),
                          alias="default", headless=False)
        output_lines: list[str] = []
        with patch("leafhub.manage.projects._write_dotfile"), \
             patch("leafhub.manage.projects._copy_probe_to_project"), \
             patch("sys.stdin.isatty", return_value=False):
            with patch("builtins.print", side_effect=lambda *a, **k: output_lines.append(" ".join(str(x) for x in a))):
                cmd_register(args)
        full_output = "\n".join(output_lines)
        self.assertNotIn("Trileaf", full_output)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. cmd_shell_helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestCmdShellHelper(unittest.TestCase):

    def _run(self) -> str:
        from leafhub.cli import cmd_shell_helper
        captured = []
        with patch("builtins.print", side_effect=lambda *a, **k: captured.append(
            (a[0] if a else "") + k.get("end", "\n")
        )):
            cmd_shell_helper(_make_args())
        return "".join(captured)

    def test_2a_outputs_shell_content(self):
        output = self._run()
        self.assertIn("leafhub_setup_project", output,
                      "output must contain the public function name")

    def test_2a_output_starts_with_shebang_or_comment(self):
        output = self._run()
        self.assertTrue(
            output.lstrip().startswith("#") or output.lstrip().startswith("#!/"),
            "register.sh should start with a comment or shebang",
        )

    def test_2b_fallback_to_filesystem(self):
        """When importlib.resources raises FileNotFoundError, fall back to
        the repo-root register.sh file."""
        from leafhub.cli import cmd_shell_helper
        repo_register = _ROOT / "register.sh"
        if not repo_register.exists():
            self.skipTest("register.sh not found at repo root — skipping fallback test")

        captured = []
        with patch("importlib.resources.files", side_effect=FileNotFoundError), \
             patch("builtins.print", side_effect=lambda *a, **k: captured.append(
                 (a[0] if a else "") + k.get("end", "\n")
             )):
            cmd_shell_helper(_make_args())
        output = "".join(captured)
        self.assertIn("leafhub_setup_project", output)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. project create --if-not-exists
# ═══════════════════════════════════════════════════════════════════════════════

class TestIfNotExists(unittest.TestCase):

    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hub, self.key, self.store = _make_hub(self.tmp_path)
        self.proj_dir = self.tmp_path / "proj"
        self.proj_dir.mkdir()
        self._key_patch = _with_master_key(self.key)
        self._hub_patch = _with_hub_dir(self.hub)
        self._key_patch.start()
        self._hub_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._hub_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    def _run_create(self, name, path=None, if_not_exists=False):
        from leafhub.cli import cmd_project_create
        args = _make_args(
            project_name=name,
            path=str(path or self.proj_dir),
            if_not_exists=if_not_exists,
            yes=True,
        )
        with patch("leafhub.manage.projects._write_dotfile"), \
             patch("leafhub.manage.projects._distribute_integration_files",
                   return_value=[]):
            cmd_project_create(args)

    def _reload_store(self) -> SyncStore:
        return SyncStore(open_db(self.hub))

    def test_3a_new_project_created_normally(self):
        self._run_create("brand-new", if_not_exists=True)
        s = self._reload_store()
        try:
            p = s.find_project_by_name("brand-new")
            self.assertIsNotNone(p)
        finally:
            s.close()

    def test_3b_existing_project_relinked_no_duplicate(self):
        self._run_create("existing", if_not_exists=False)

        # Second call with --if-not-exists must re-link, not create a duplicate
        second_dir = self.tmp_path / "second"
        second_dir.mkdir()
        self._run_create("existing", path=second_dir, if_not_exists=True)

        s = self._reload_store()
        try:
            matches = [p for p in s.list_projects() if p.name == "existing"]
            self.assertEqual(len(matches), 1,
                             "--if-not-exists must not create a second project")
        finally:
            s.close()

    def test_3b_existing_project_path_updated(self):
        self._run_create("path-update", if_not_exists=False)
        second_dir = self.tmp_path / "second2"
        second_dir.mkdir()
        self._run_create("path-update", path=second_dir, if_not_exists=True)

        s = self._reload_store()
        try:
            p = s.find_project_by_name("path-update")
            self.assertEqual(str(p.path), str(second_dir))
        finally:
            s.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. project create --yes (skip wizard)
# ═══════════════════════════════════════════════════════════════════════════════

class TestProjectCreateYes(unittest.TestCase):

    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hub, self.key, self.store = _make_hub(self.tmp_path)
        self._key_patch = _with_master_key(self.key)
        self._hub_patch = _with_hub_dir(self.hub)
        self._key_patch.start()
        self._hub_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._hub_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    def test_4a_yes_skips_wizard(self):
        """--yes must not call _interactive_bind_wizard even when TTY is available."""
        from leafhub.cli import cmd_project_create
        args = _make_args(project_name="skip-wizard", path=None,
                          no_probe=True, if_not_exists=False, yes=True)
        with patch("leafhub.cli._interactive_bind_wizard") as mock_wizard, \
             patch("sys.stdin.isatty", return_value=True):
            cmd_project_create(args)
        mock_wizard.assert_not_called()

    def test_4b_without_yes_wizard_runs(self):
        """Without --yes, wizard is called when TTY is available."""
        from leafhub.cli import cmd_project_create
        args = _make_args(project_name="run-wizard", path=None,
                          no_probe=True, if_not_exists=False, yes=False)
        with patch("leafhub.cli._interactive_bind_wizard") as mock_wizard, \
             patch("sys.stdin.isatty", return_value=True):
            cmd_project_create(args)
        mock_wizard.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. provider list --json  /  status --json
# ═══════════════════════════════════════════════════════════════════════════════

class TestJsonOutput(unittest.TestCase):

    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hub, self.key, self.store = _make_hub(self.tmp_path)
        self._key_patch = _with_master_key(self.key)
        self._hub_patch = _with_hub_dir(self.hub)
        self._key_patch.start()
        self._hub_patch.start()

    def tearDown(self):
        self._key_patch.stop()
        self._hub_patch.stop()
        self.store.close()
        self._tmp.cleanup()

    def _capture_print(self, func, *args) -> str:
        lines: list[str] = []
        with patch("builtins.print", side_effect=lambda *a, **k: lines.append(str(a[0]) if a else "")):
            func(*args)
        return "\n".join(lines)

    # ── provider list --json ───────────────────────────────────────────────────

    def test_5a_provider_list_json_empty(self):
        from leafhub.cli import cmd_provider_list
        args = _make_args(json=True)
        output = self._capture_print(cmd_provider_list, args)
        data = json.loads(output)
        self.assertEqual(data, [])

    def test_5a_provider_list_json_with_provider(self):
        _add_provider(self.store, self.hub, self.key, "MyProv")
        from leafhub.cli import cmd_provider_list
        args = _make_args(json=True)
        output = self._capture_print(cmd_provider_list, args)
        data = json.loads(output)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["label"], "MyProv")
        # Required keys
        for key in ("label", "api_format", "default_model", "id"):
            self.assertIn(key, data[0])

    def test_5a_provider_list_json_multiple(self):
        _add_provider(self.store, self.hub, self.key, "A")
        _add_provider(self.store, self.hub, self.key, "B", api_key="sk-b")
        from leafhub.cli import cmd_provider_list
        args = _make_args(json=True)
        output = self._capture_print(cmd_provider_list, args)
        data = json.loads(output)
        self.assertEqual(len(data), 2)

    # ── status --json ──────────────────────────────────────────────────────────

    def test_5b_status_json_empty(self):
        from leafhub.cli import cmd_status
        args = _make_args(json=True)
        output = self._capture_print(cmd_status, args)
        data = json.loads(output)
        self.assertEqual(data["providers"], 0)
        self.assertEqual(data["projects"], 0)
        self.assertEqual(data["bound_projects"], 0)
        self.assertFalse(data["ready"])

    def test_5b_status_json_with_provider(self):
        _add_provider(self.store, self.hub, self.key)
        from leafhub.cli import cmd_status
        args = _make_args(json=True)
        output = self._capture_print(cmd_status, args)
        data = json.loads(output)
        self.assertEqual(data["providers"], 1)
        self.assertTrue(data["ready"])

    def test_5b_status_json_bound_projects_counted(self):
        pid = _add_provider(self.store, self.hub, self.key)
        p, _ = self.store.create_project("myapp")
        self.store.add_binding(project_id=p.id, alias="default", provider_id=pid)

        from leafhub.cli import cmd_status
        args = _make_args(json=True)
        output = self._capture_print(cmd_status, args)
        data = json.loads(output)
        self.assertEqual(data["projects"], 1)
        self.assertEqual(data["bound_projects"], 1)

    def test_5b_status_json_unbound_project_not_counted(self):
        _add_provider(self.store, self.hub, self.key)
        self.store.create_project("unbound")
        from leafhub.cli import cmd_status
        args = _make_args(json=True)
        output = self._capture_print(cmd_status, args)
        data = json.loads(output)
        self.assertEqual(data["projects"], 1)
        self.assertEqual(data["bound_projects"], 0)

    def test_5b_status_json_keys_present(self):
        from leafhub.cli import cmd_status
        args = _make_args(json=True)
        output = self._capture_print(cmd_status, args)
        data = json.loads(output)
        for key in ("providers", "projects", "bound_projects", "ready"):
            self.assertIn(key, data, f"missing key '{key}' in status --json output")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _cleanup_installer_registration
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(os.name == "posix", "symlink / RC tests are POSIX-only")
class TestCleanupInstallerRegistration(unittest.TestCase):

    def setUp(self):
        self._tmp      = tempfile.TemporaryDirectory()
        self.tmp_path  = Path(self._tmp.name)
        self.fake_home = self.tmp_path / "home"
        self.fake_home.mkdir()
        self.project_dir = self.tmp_path / "myproject"
        self.project_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _cleanup(self, project_dir=None) -> list[str]:
        from leafhub.manage.projects import _cleanup_installer_registration
        with patch.object(Path, "home", return_value=self.fake_home):
            return _cleanup_installer_registration(project_dir or self.project_dir)

    def _make_local_bin(self) -> Path:
        local_bin = self.fake_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        return local_bin

    # ── 6a. Symlink removal ────────────────────────────────────────────────────

    def test_6a_symlink_pointing_into_project_removed(self):
        local_bin = self._make_local_bin()
        # Real binary inside the project
        binary = self.project_dir / "bin" / "myapp"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\necho hi\n")
        # Symlink in ~/.local/bin/ → project binary
        link = local_bin / "myapp"
        link.symlink_to(binary)

        removed = self._cleanup()

        self.assertFalse(link.exists() or link.is_symlink(),
                         "symlink should have been removed")
        self.assertTrue(any("myapp" in r for r in removed))

    def test_6a_symlink_outside_project_not_removed(self):
        local_bin = self._make_local_bin()
        other_dir = self.tmp_path / "other"
        other_dir.mkdir()
        other_bin = other_dir / "othertool"
        other_bin.write_text("#!/bin/sh\n")
        link = local_bin / "othertool"
        link.symlink_to(other_bin)

        self._cleanup()

        self.assertTrue(link.is_symlink(), "unrelated symlink must not be removed")

    def test_6a_non_symlink_entry_not_removed(self):
        local_bin = self._make_local_bin()
        # Regular file (not symlink) that happens to share project dir name
        regular = local_bin / "myapp"
        regular.write_text("#!/bin/sh\n")

        self._cleanup()

        self.assertTrue(regular.exists(), "non-symlink must not be removed")

    # ── 6b. RC file PATH line stripping ───────────────────────────────────────

    def test_6b_venv_path_line_removed_from_rc(self):
        zshrc = self.fake_home / ".zshrc"
        venv_bin = str(self.project_dir / ".venv" / "bin")
        zshrc.write_text(
            "# existing config\n"
            f'export PATH="{venv_bin}:$PATH"\n'
            "alias ll='ls -la'\n",
            encoding="utf-8",
        )

        removed = self._cleanup()

        content = zshrc.read_text(encoding="utf-8")
        self.assertNotIn(venv_bin, content)
        self.assertIn("alias ll", content, "unrelated lines must be preserved")
        self.assertTrue(any(".zshrc" in r for r in removed))

    def test_6b_unrelated_rc_lines_preserved(self):
        bashrc = self.fake_home / ".bashrc"
        venv_bin = str(self.project_dir / ".venv" / "bin")
        bashrc.write_text(
            "source ~/.bash_aliases\n"
            f'PATH="{venv_bin}:$PATH"\n'
            "export NVM_DIR=~/.nvm\n",
            encoding="utf-8",
        )
        self._cleanup()
        content = bashrc.read_text(encoding="utf-8")
        self.assertIn("source ~/.bash_aliases", content)
        self.assertIn("NVM_DIR", content)

    def test_6b_rc_not_modified_when_no_venv_entry(self):
        zshrc = self.fake_home / ".zshrc"
        original = "export PATH=$PATH:/usr/local/bin\n"
        zshrc.write_text(original, encoding="utf-8")

        removed = self._cleanup()

        self.assertEqual(zshrc.read_text(encoding="utf-8"), original)
        self.assertFalse(any(".zshrc" in r for r in removed))

    # ── 6c. Symlink comparison works when project_dir has symlink components ──

    def test_6c_symlinked_project_dir_still_cleaned(self):
        """Symlinks in project_dir's path must not prevent cleanup.

        The bug: entry.resolve().is_relative_to(project_dir) would fail if
        project_dir itself contains symlink components.
        The fix: compare against project_dir.resolve().
        """
        local_bin = self._make_local_bin()

        # Create a real directory and a symlink alias for it
        real_dir  = self.tmp_path / "real_project"
        real_dir.mkdir()
        symlink_dir = self.tmp_path / "symlink_project"
        symlink_dir.symlink_to(real_dir)

        # Binary lives inside the real dir
        binary = real_dir / "myapp"
        binary.write_text("#!/bin/sh\n")

        # Symlink in ~/.local/bin/ → binary via the real path
        link = local_bin / "myapp"
        link.symlink_to(binary)

        # Pass the symlinked path as project_dir — fix must still find the symlink
        from leafhub.manage.projects import _cleanup_installer_registration
        with patch.object(Path, "home", return_value=self.fake_home):
            removed = _cleanup_installer_registration(symlink_dir)

        self.assertFalse(link.is_symlink(),
                         "symlink should be removed even when project_dir is a symlink path")
        self.assertTrue(any("myapp" in r for r in removed))

    # ── 6d. Missing ~/.local/bin silently skipped ─────────────────────────────

    def test_6d_missing_local_bin_does_not_raise(self):
        # fake_home has no .local/bin — must not raise
        try:
            removed = self._cleanup()
        except Exception as exc:
            self.fail(f"_cleanup raised unexpectedly: {exc}")
        self.assertEqual(removed, [])

    # ── 6e. Returns list of human-readable descriptions ───────────────────────

    def test_6e_return_value_is_strings(self):
        local_bin = self._make_local_bin()
        binary = self.project_dir / "tool"
        binary.write_text("#!/bin/sh\n")
        link = local_bin / "tool"
        link.symlink_to(binary)
        removed = self._cleanup()
        for item in removed:
            self.assertIsInstance(item, str)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)

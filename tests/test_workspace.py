from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from astock_bot.workspace import WorkspaceRegistry
from astock_bot.web import _origin_matches_request_host


class WorkspaceRegistryTests(unittest.TestCase):
    def test_default_keeps_legacy_paths_and_new_workspace_uses_own_directory(self):
        with TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            registry = WorkspaceRegistry(data_dir)
            default = registry.default()
            created = registry.create()

            self.assertTrue(default.is_default)
            self.assertFalse(created.is_default)
            self.assertNotEqual(default.id, created.id)
            self.assertEqual(
                registry.ledger_path(default, data_dir / "portfolio.db"),
                data_dir / "portfolio.db",
            )
            self.assertEqual(
                registry.ledger_path(created, data_dir / "portfolio.db"),
                data_dir / "workspaces" / created.id / "portfolio.db",
            )
            self.assertEqual(len(registry.list()), 2)

    def test_default_has_fixed_password_and_new_workspace_gets_one_time_random_password(self):
        with TemporaryDirectory() as directory:
            registry = WorkspaceRegistry(Path(directory) / "data")
            default = registry.default()
            created = registry.create()

            self.assertTrue(registry.verify_password(default, "960818"))
            self.assertFalse(registry.verify_password(default, "960819"))
            self.assertIsNotNone(created.initial_password)
            self.assertGreaterEqual(len(created.initial_password or ""), 40)
            self.assertTrue(registry.verify_password(created, created.initial_password or ""))
            self.assertNotIn("initial_password", registry.path.read_text(encoding="utf-8"))

    def test_workspace_access_token_is_scoped_to_its_workspace(self):
        with TemporaryDirectory() as directory:
            registry = WorkspaceRegistry(Path(directory) / "data")
            default = registry.default()
            created = registry.create()
            token = registry.issue_access_token(default)
            default = registry.get(default.id)

            self.assertTrue(registry.has_access(default, token))
            self.assertFalse(registry.has_access(created, token))

    def test_same_origin_and_local_aliases_are_accepted_but_remote_origin_is_rejected(self):
        self.assertTrue(_origin_matches_request_host("http://121.40.151.18:8787", "121.40.151.18:8787"))
        self.assertTrue(_origin_matches_request_host("http://localhost:8787", "127.0.0.1:8787"))
        self.assertTrue(_origin_matches_request_host("http://127.0.0.1:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("http://127.0.0.1:8787", "0.0.0.0:8787"))
        self.assertTrue(_origin_matches_request_host("http://0.0.0.0:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("http://localhost:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("null", "127.0.0.1:8787"))
        self.assertTrue(_origin_matches_request_host("null", "121.40.151.18:8787"))
        self.assertFalse(_origin_matches_request_host("https://example.com", "localhost:8787"))
        self.assertFalse(_origin_matches_request_host("http://localhost:9000", "127.0.0.1:8787"))


if __name__ == "__main__":
    unittest.main()

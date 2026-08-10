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

    def test_local_origin_aliases_are_accepted_but_remote_origin_is_rejected(self):
        self.assertTrue(_origin_matches_request_host("http://localhost:8787", "127.0.0.1:8787"))
        self.assertTrue(_origin_matches_request_host("http://127.0.0.1:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("http://127.0.0.1:8787", "0.0.0.0:8787"))
        self.assertTrue(_origin_matches_request_host("http://0.0.0.0:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("http://localhost:8787", "localhost:8787"))
        self.assertTrue(_origin_matches_request_host("null", "127.0.0.1:8787"))
        self.assertFalse(_origin_matches_request_host("null", "example.com"))
        self.assertFalse(_origin_matches_request_host("https://example.com", "localhost:8787"))
        self.assertFalse(_origin_matches_request_host("http://localhost:9000", "127.0.0.1:8787"))


if __name__ == "__main__":
    unittest.main()

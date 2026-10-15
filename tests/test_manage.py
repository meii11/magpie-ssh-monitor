import argparse
import tempfile
from pathlib import Path
import unittest

import manage


class ManagementTests(unittest.TestCase):
    def test_aliases_cannot_inject_ssh_options_or_shell_commands(self):
        for host in ["-oProxyCommand=id", "host;id", "host\nother", "user@host", "host *", "", "x" * 64]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                manage.validate_host(host)

    def test_similar_aliases_have_distinct_persistent_identities(self):
        root = Path("/tmp/private")
        values = [manage.locations(root, host) for host in ["dev-a", "dev.a", "dev_a", "Dev-a"]]
        self.assertEqual(len({str(x["state"]) for x in values}), 4)
        self.assertEqual(len({x["provider"] for x in values}), 4)
        self.assertEqual(len({x["label"] for x in values}), 4)

    def test_gateway_never_sends_local_usage_to_a_remote_site(self):
        for value in ["https://example.com", "http://localhost:3425", "http://127.0.0.1:0", "http://127.0.0.1:99999", "http://user:secret@127.0.0.1:3425", "http://127.0.0.1:3425/path"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                manage.validate_gateway(value)
        self.assertEqual(manage.validate_gateway("http://127.0.0.1:3499"), "http://127.0.0.1:3499")

    def test_background_job_uses_installed_runtime_and_explicit_state(self):
        paths = manage.locations(Path("/tmp/private"), "devbox")
        value = manage.job_definition(paths, {"interval": 120})
        self.assertEqual(value["WorkingDirectory"], str(paths["program"]))
        self.assertIn(str(paths["state"]), value["ProgramArguments"])
        self.assertEqual(value["StartInterval"], 120)
        self.assertNotIn(str(manage.SOURCE), value["ProgramArguments"])

    def test_registry_rejects_unknown_versions_and_duplicate_hosts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "registry.json").write_text('{"version":99,"servers":[]}')
            with self.assertRaises(ValueError): manage.registry(root)
            (root / "registry.json").write_text('{"version":1,"servers":["devbox","devbox"]}')
            with self.assertRaises(ValueError): manage.registry(root)

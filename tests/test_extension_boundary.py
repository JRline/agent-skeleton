"""Sample connectors, datasets and their data are extensions: the core never names one (CLAUDE.md constraints)."""
import re
import unittest

from agent import config, connectors

CORE = ["agent", "app.py", "cli.py", "scripts"]


class ExtensionBoundary(unittest.TestCase):
    def test_the_core_names_no_bundled_connector_or_dataset(self):
        names = set()
        for conn in connectors.all_connectors().values():
            names |= {conn.name, *conn.datasets}
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
        found = []
        for part in CORE:
            path = config.ROOT / part
            for f in [path] if path.is_file() else path.rglob("*"):
                if f.suffix in (".py", ".md", ".html") and "__pycache__" not in f.parts:
                    for n, line in enumerate(f.read_text().splitlines(), 1):
                        if pattern.search(line):
                            found.append(f"{f.relative_to(config.ROOT)}:{n}: {line.strip()}")
        self.assertEqual(found, [], "move these into extensions/connectors/<name>/")

    def test_a_connectors_sample_inputs_come_from_its_own_module(self):
        self.assertIn("book_mapping.csv", connectors.sample_inputs())


if __name__ == "__main__":
    unittest.main()

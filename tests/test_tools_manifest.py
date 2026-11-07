import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "tools" / "manifest.json").read_text(encoding="utf-8"))
CLASSES = {"read-only", "local-write", "remote-write", "human-only"}
ARGUMENT_FIELDS = {"name", "type", "required", "default", "description"}


def help_text(tool):
    command = [sys.executable, str(ROOT / tool["path"])]
    if tool["subcommand"]:
        command.append(tool["subcommand"])
    done = subprocess.run([*command, "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, f"{tool['name']}: {done.stderr}"
    return done.stdout


class ToolsManifestTest(unittest.TestCase):
    def test_the_safety_classes_are_the_four_defined_ones(self):
        self.assertEqual(set(MANIFEST["safety_classes"]), CLASSES)
        for tool in MANIFEST["tools"]:
            self.assertIn(tool["safety_class"], CLASSES, tool["name"])

    def test_tool_names_are_unique_and_each_entry_is_complete(self):
        names = [tool["name"] for tool in MANIFEST["tools"]]
        self.assertEqual(len(names), len(set(names)))
        for tool in MANIFEST["tools"]:
            for key in ("name", "path", "subcommand", "description", "safety_class", "writes", "arguments", "output"):
                self.assertIn(key, tool, f"{tool['name']} lacks {key}")
            self.assertTrue(tool["description"].strip())
            self.assertIn("format", tool["output"])
            self.assertIn("shape", tool["output"])
            for argument in tool["arguments"]:
                self.assertEqual(set(argument), ARGUMENT_FIELDS, f"{tool['name']} {argument.get('name')}")

    def test_a_read_only_tool_writes_nothing(self):
        for tool in MANIFEST["tools"]:
            if tool["safety_class"] == "read-only":
                self.assertEqual(tool["writes"], [], tool["name"])

    def test_no_human_only_tool_is_listed(self):
        # The manifest is what an MCP server reads. A human-only tool stays out of it.
        for tool in MANIFEST["tools"]:
            self.assertNotEqual(tool["safety_class"], "human-only", tool["name"])

    def test_every_script_in_tools_has_an_entry_and_every_entry_has_a_script(self):
        scripts = {f"tools/{p.name}" for p in (ROOT / "tools").glob("*.py")}
        listed = {tool["path"] for tool in MANIFEST["tools"]}
        self.assertEqual(scripts, listed)

    def test_every_listed_argument_appears_in_the_help_of_its_script(self):
        for tool in MANIFEST["tools"]:
            text = help_text(tool)
            usage = text.split("\n\n")[0]
            for argument in tool["arguments"]:
                where = text if argument["name"].startswith("-") else usage
                self.assertIn(argument["name"], where, f"{tool['name']} help lacks {argument['name']}")

    def test_the_help_of_each_script_lists_no_option_the_manifest_lacks(self):
        for tool in MANIFEST["tools"]:
            options = set(re.findall(r"(?m)^\s+(--[a-z][a-z-]*)", help_text(tool))) - {"--help"}
            listed = {a["name"] for a in tool["arguments"]}
            self.assertEqual(options - listed, set(), f"{tool['name']} has options the manifest does not list")


if __name__ == "__main__":
    unittest.main()

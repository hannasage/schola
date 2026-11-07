import copy
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


# COPY-START exposure check. Copy everything between COPY-START and COPY-END into
# another repository. It needs only the standard library.

EXPOSURE_VALUES = ("expose", "hidden")
SAFETY_CLASS_NAMES = ("read-only", "local-write", "remote-write", "human-only")
MAX_TOOLS = 1000


def exposure_problems(manifest) -> list:
    """Problems with the exposure field. An empty list means the manifest is sound.
    This function never raises on a malformed manifest. It reports a problem instead."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list) or not manifest["tools"]:
        return ["the manifest needs a non-empty tools list"]
    if len(manifest["tools"]) > MAX_TOOLS:
        return [f"the manifest has more than {MAX_TOOLS} tools"]
    problems = []
    if "exposure_default" in manifest and manifest["exposure_default"] != "hidden":
        problems.append("exposure_default must be hidden")
    names = []
    for index, tool in enumerate(manifest["tools"]):
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name.strip():
            problems.append(f"tool #{index}: needs a name")
        else:
            names.append(name)
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{name}: the name appears more than once")
    held = manifest.get("held_back", [])
    held_names = []
    if not isinstance(held, list):
        problems.append("held_back must be a list")
    else:
        for entry in held:
            if isinstance(entry, dict) and isinstance(entry.get("tool"), str):
                held_names.append(entry["tool"])
                if entry["tool"] not in names:
                    problems.append(f"held_back names {entry['tool']}, which is not a tool")
            else:
                problems.append("held_back entries need a tool name")
    for tool in manifest["tools"]:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        exposed = tool.get("mcp_exposure") == "expose"
        if "mcp_exposure" in tool:
            value = tool["mcp_exposure"]
            if not (isinstance(value, str) and value in EXPOSURE_VALUES):
                problems.append(f"{name}: mcp_exposure must be expose or hidden")
        safety = tool.get("safety_class")
        if safety not in SAFETY_CLASS_NAMES:
            problems.append(f"{name}: safety_class must be one of {', '.join(SAFETY_CLASS_NAMES)}")
        if safety == "human-only" and exposed:
            problems.append(f"{name}: a human-only tool must be hidden")
        if "destructive" in tool and not isinstance(tool["destructive"], bool):
            problems.append(f"{name}: destructive must be true or false")
        elif tool.get("destructive") is True and exposed:
            problems.append(f"{name}: a destructive tool must be hidden")
        elif exposed and tool.get("destructive") is not False:
            problems.append(f"{name}: an exposed tool must say destructive: false")
        if name in held_names and exposed:
            problems.append(f"{name}: a held-back tool must be hidden")
    return problems


def exposed_tools(manifest) -> list:
    """The one loader. It lists a tool only when the whole manifest passes exposure_problems
    and the tool's mcp_exposure is exactly "expose". Anything else lists nothing."""
    if exposure_problems(manifest):
        return []
    return [t for t in manifest["tools"] if t.get("mcp_exposure") == "expose"]


# COPY-END


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


    def test_the_exposure_fields_pass_the_shared_check(self):
        self.assertEqual(exposure_problems(MANIFEST), [])

    def test_only_the_read_only_tools_are_exposed(self):
        exposed = {tool["name"] for tool in exposed_tools(MANIFEST)}
        self.assertEqual(exposed, {"pr_body.scaffold", "pr_body.check"})
        for tool in MANIFEST["tools"]:
            expected = "expose" if tool["safety_class"] == "read-only" else "hidden"
            self.assertEqual(tool["mcp_exposure"], expected, tool["name"])
            self.assertIs(tool["destructive"], False, tool["name"])

    def test_the_shared_check_rejects_an_exposed_tool_that_is_not_safe(self):
        cases = {
            "a human-only tool": lambda t: t.update(safety_class="human-only", mcp_exposure="expose", destructive=False),
            "a destructive tool": lambda t: t.update(mcp_exposure="expose", destructive=True),
            "an exposed tool with no destructive field": lambda t: (t.pop("destructive"), t.update(mcp_exposure="expose")),
            "an unknown exposure value": lambda t: t.update(mcp_exposure="yes"),
        }
        for label, mutate in cases.items():
            broken = copy.deepcopy(MANIFEST)
            mutate(broken["tools"][2])
            self.assertNotEqual(exposure_problems(broken), [], label)
            self.assertEqual(exposed_tools(broken), [], f"{label}: the loader must list nothing")

    def test_the_shared_check_rejects_a_default_other_than_hidden_and_a_held_back_exposed_tool(self):
        broken = copy.deepcopy(MANIFEST)
        broken["exposure_default"] = "expose"
        self.assertNotEqual(exposure_problems(broken), [])
        broken = copy.deepcopy(MANIFEST)
        broken["held_back"] = [{"tool": "pr_body.check"}]
        self.assertNotEqual(exposure_problems(broken), [])
        self.assertEqual(exposed_tools(broken), [])

    def test_a_missing_exposure_field_means_hidden(self):
        trimmed = copy.deepcopy(MANIFEST)
        for tool in trimmed["tools"]:
            tool.pop("mcp_exposure")
        self.assertEqual(exposure_problems(trimmed), [])
        self.assertEqual(exposed_tools(trimmed), [])

    def test_the_shared_check_does_not_raise_on_a_malformed_manifest(self):
        for bad in (None, [], {}, {"tools": []}, {"tools": [None]}, {"tools": "x"}):
            self.assertNotEqual(exposure_problems(bad), [], repr(bad))
            self.assertEqual(exposed_tools(bad), [], repr(bad))


    def test_schola_exposes_only_read_only_tools_that_write_nothing(self):
        # Schola's own rule, stricter than the shared check: the shared block alone would let a
        # tool that writes files be exposed by one field. The block is a copy and stays as it is.
        for tool in exposed_tools(MANIFEST):
            self.assertEqual(tool["safety_class"], "read-only", tool["name"])
            self.assertEqual(tool["writes"], [], tool["name"])

    def test_every_rule_of_the_shared_check_fails_on_its_own_broken_manifest(self):
        def broken(mutate):
            manifest = copy.deepcopy(MANIFEST)
            mutate(manifest)
            return manifest

        cases = {
            "a duplicate name": lambda m: m["tools"][1].update(name=m["tools"][0]["name"]),
            "an unknown held_back tool": lambda m: m.update(held_back=[{"tool": "no-such-tool"}]),
            "a held_back list that is not a list": lambda m: m.update(held_back="x"),
            "a held_back entry with no tool name": lambda m: m.update(held_back=[{}]),
            "a held_back entry that is not an object": lambda m: m.update(held_back=["x"]),
            "a bad safety class": lambda m: m["tools"][0].update(safety_class="read_only"),
            "a destructive field that is not a boolean": lambda m: m["tools"][0].update(destructive="no"),
            "a tool with no name": lambda m: m["tools"][0].pop("name"),
            "too many tools": lambda m: m.update(tools=[copy.deepcopy(m["tools"][0]) | {"name": f"t{i}"}
                                                        for i in range(MAX_TOOLS + 1)]),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                manifest = broken(mutate)
                self.assertNotEqual(exposure_problems(manifest), [])
                self.assertEqual(exposed_tools(manifest), [])


if __name__ == "__main__":
    unittest.main()

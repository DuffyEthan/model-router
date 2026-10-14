"""The CLI's router prompt must stay identical to the plugin's SYSTEM prompt.

Reads plugin/hooks/register.ts as text and pulls out the value of `const SYSTEM`,
whether it is written as a template literal or as quoted strings joined with `+`.
"""

import difflib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import route  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
REGISTER_TS = REPO / "plugin" / "hooks" / "register.ts"

_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
    "'": "'", '"': '"', "`": "`", "\\": "\\", "$": "$",
}


def _read_literal(src: str, i: int) -> tuple[str, int]:
    """Parse one JS string or template literal starting at src[i]. Returns (value, end index)."""
    quote = src[i]
    i += 1
    out = []
    while i < len(src):
        c = src[i]
        if c == quote:
            return "".join(out), i + 1
        if c == "\\":
            nxt = src[i + 1]
            if nxt == "\n":  # line continuation
                i += 2
                continue
            if nxt == "u":
                if src[i + 2] == "{":
                    close = src.index("}", i)
                    out.append(chr(int(src[i + 3:close], 16)))
                    i = close + 1
                else:
                    out.append(chr(int(src[i + 2:i + 6], 16)))
                    i += 6
                continue
            if nxt == "x":
                out.append(chr(int(src[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(_SIMPLE_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if quote == "`" and src.startswith("${", i):
            raise ValueError("SYSTEM uses ${...} interpolation; the parity test can't evaluate it")
        if quote != "`" and c == "\n":
            raise ValueError("unterminated string literal in SYSTEM")
        out.append(c)
        i += 1
    raise ValueError("unterminated literal in SYSTEM")


def _skip_trivia(src: str, i: int) -> int:
    """Skip whitespace, comments and grouping parentheses between literals."""
    while i < len(src):
        if src[i] in " \t\r\n()":
            i += 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            i = len(src) if i == -1 else i
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        else:
            break
    return i


def extract_system(src: str) -> str:
    m = re.search(r"\bconst\s+SYSTEM\b\s*(?::\s*string\s*)?=", src)
    if not m:
        raise ValueError("no `const SYSTEM = ...` in register.ts")
    i = m.end()
    parts = []
    while True:
        i = _skip_trivia(src, i)
        if i >= len(src) or src[i] not in "`'\"":
            raise ValueError(f"expected a string literal in SYSTEM, found {src[i:i + 20]!r}")
        value, i = _read_literal(src, i)
        parts.append(value)
        i = _skip_trivia(src, i)
        if src.startswith("+", i):
            i += 1
            continue
        return "".join(parts)


def normalise(text: str) -> str:
    """Only trivial differences are ignored: trailing whitespace and line endings."""
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


class ExtractSystemTests(unittest.TestCase):
    def test_template_literal(self):
        self.assertEqual(extract_system("const SYSTEM = `a\nb \\` c`\n"), "a\nb ` c")

    def test_concatenated_strings(self):
        src = "const SYSTEM: string =\n  'a\\n' + // note\n  \"b 'q'\" +\n  `c`\n"
        self.assertEqual(extract_system(src), "a\nb 'q'c")

    def test_interpolation_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_system("const SYSTEM = `a ${x} b`")


class PromptParityTests(unittest.TestCase):
    def test_cli_prompt_matches_plugin_system_prompt(self):
        plugin = normalise(extract_system(REGISTER_TS.read_text(encoding="utf-8")))
        cli = normalise(route.SYSTEM_PROMPT)
        if plugin != cli:
            diff = "\n".join(difflib.unified_diff(
                plugin.split("\n"), cli.split("\n"),
                fromfile="plugin/hooks/register.ts SYSTEM",
                tofile="cli/route.py SYSTEM_PROMPT",
                lineterm="",
            ))
            self.fail("CLI router prompt has drifted from the plugin's SYSTEM prompt. "
                      "Update cli/route.py SYSTEM_PROMPT to match:\n" + diff)


    def test_allowed_models_and_efforts_match_plugin(self):
        src = REGISTER_TS.read_text(encoding="utf-8")
        for name, cli_values in (("TIERS", route.MODELS), ("EFFORTS", route.EFFORTS)):
            m = re.search(r"\bconst\s+" + name + r"\b[^=]*=\s*\[([^\]]*)\]", src)
            self.assertIsNotNone(m, f"no `const {name} = [...]` in register.ts")
            plugin_values = tuple(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
            self.assertEqual(cli_values, plugin_values, f"CLI allowed values differ from register.ts {name}")


if __name__ == "__main__":
    unittest.main()

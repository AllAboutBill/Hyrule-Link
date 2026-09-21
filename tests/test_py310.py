"""The droplet runs Python 3.10: nothing newer may appear in the code it runs.

CI and the dev venv are 3.12, so a 3.11+ construct would pass every other test
and then fail at import on the server. Two checks: the grammar (ast with
feature_version 3.10) and a short list of 3.11+ standard-library names that
the grammar check cannot see.
"""
import ast
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 3.11+ only: modules, `from X import Y` / `X.Y` names, and builtins.
NEWER_MODULES = {"tomllib", "wsgiref.types"}
NEWER_NAMES = {
    ("asyncio", "TaskGroup"), ("asyncio", "timeout"), ("asyncio", "timeout_at"),
    ("asyncio", "Runner"), ("datetime", "UTC"), ("enum", "StrEnum"),
    ("enum", "verify"), ("hashlib", "file_digest"), ("operator", "call"),
    ("typing", "Self"), ("typing", "LiteralString"), ("typing", "Never"),
    ("typing", "assert_never"), ("typing", "assert_type"), ("typing", "reveal_type"),
    ("typing", "TypeVarTuple"), ("typing", "Unpack"), ("typing", "Required"),
    ("typing", "NotRequired"), ("typing", "override"), ("typing", "TypeAliasType"),
}
NEWER_BUILTINS = {"ExceptionGroup", "BaseExceptionGroup"}


def server_files():
    files = sorted(glob.glob(os.path.join(ROOT, "server", "*.py"))
                   + glob.glob(os.path.join(ROOT, "shared", "*.py")))
    files.append(os.path.join(ROOT, "run_server.py"))
    fake = os.path.join(ROOT, "tools", "fake_snes.py")
    if os.path.exists(fake):
        files.append(fake)
    return files


def newer_names(tree):
    """(line, what) for each 3.11+ stdlib name used in `tree`."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in NEWER_MODULES:
                    found.append((node.lineno, a.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in NEWER_MODULES:
                found.append((node.lineno, node.module))
            for a in node.names:
                if (node.module, a.name) in NEWER_NAMES:
                    found.append((node.lineno, f"{node.module}.{a.name}"))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (node.value.id, node.attr) in NEWER_NAMES:
                found.append((node.lineno, f"{node.value.id}.{node.attr}"))
        elif isinstance(node, ast.Name) and node.id in NEWER_BUILTINS:
            found.append((node.lineno, node.id))
    return found


class Python310Tests(unittest.TestCase):
    def test_the_files_exist(self):
        files = server_files()
        self.assertIn(os.path.join(ROOT, "server", "app.py"), files)
        self.assertIn(os.path.join(ROOT, "server", "operator.py"), files)
        for path in files:
            self.assertTrue(os.path.isfile(path), path)

    def test_grammar_is_python_310(self):
        for path in server_files():
            with self.subTest(path=os.path.relpath(path, ROOT)):
                with open(path, encoding="utf-8") as f:
                    ast.parse(f.read(), filename=path, feature_version=(3, 10))

    def test_no_311_stdlib_names(self):
        for path in server_files():
            with self.subTest(path=os.path.relpath(path, ROOT)):
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                self.assertEqual(newer_names(tree), [])

    def test_the_checker_catches_what_it_should(self):
        with self.assertRaises(SyntaxError):
            ast.parse("try:\n    pass\nexcept* ValueError:\n    pass\n", feature_version=(3, 10))
        with self.assertRaises(SyntaxError):
            ast.parse("def f[T](x: T) -> T:\n    return x\n", feature_version=(3, 10))
        tree = ast.parse("import tomllib\nfrom typing import Self\nimport asyncio\n"
                         "asyncio.timeout(1)\nraise ExceptionGroup('x', [])\n")
        self.assertEqual(sorted(what for _, what in newer_names(tree)),
                         ["ExceptionGroup", "asyncio.timeout", "tomllib", "typing.Self"])


if __name__ == "__main__":
    unittest.main()

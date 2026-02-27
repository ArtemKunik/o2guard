"""Tests for o2guard.py"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Allow importing o2guard from the repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import o2guard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "tester@example.com"],
        ["git", "config", "user.name", "Tester"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    return repo


def _write_registry(repo: Path, keys: list[str]) -> None:
    content = "\n".join(keys)
    (repo / ".o2registry").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

class TestStripComments(unittest.TestCase):
    def test_removes_ts_line_comments(self):
        src = (
            "const a = process.env.KEEP_ME;\n"
            "// const b = process.env.COMMENTED_OUT;\n"
            "const c = process.env.ALSO_KEPT;\n"
        )
        usages = o2guard.extract_variable_usages(Path("sample.ts"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"KEEP_ME", "ALSO_KEPT"})

    def test_removes_ts_block_comments(self):
        src = (
            "const a = process.env.KEEP_ME;\n"
            "/* const b = process.env.BLOCK_ONE;\n"
            "   const c = process.env.BLOCK_TWO; */\n"
            "const d = process.env.VISIBLE;\n"
        )
        usages = o2guard.extract_variable_usages(Path("sample.ts"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"KEEP_ME", "VISIBLE"})

    def test_removes_python_hash_comments(self):
        src = (
            "import os\n"
            "db = os.environ['REAL_KEY']\n"
            "# token = os.getenv('COMMENTED_KEY')\n"
            "val = os.getenv('VISIBLE_KEY')\n"
        )
        usages = o2guard.extract_variable_usages(Path("sample.py"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"REAL_KEY", "VISIBLE_KEY"})

    def test_preserves_line_numbers_after_block_comment(self):
        src = (
            "/* comment */\n"
            "const a = process.env.LINE_TWO_VAR;\n"
        )
        usages = o2guard.extract_variable_usages(Path("sample.ts"), src)
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0].variable, "LINE_TWO_VAR")
        self.assertEqual(usages[0].line, 2)


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------

class TestExtractVariableUsages(unittest.TestCase):
    def test_typescript_direct_access(self):
        src = """
const a = process.env.DATABASE_URL;
const b = process.env["API_KEY"];
const c = process.env['SVC_TOKEN'];
"""
        usages = o2guard.extract_variable_usages(Path("app.ts"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"DATABASE_URL", "API_KEY", "SVC_TOKEN"})

    def test_typescript_destructuring(self):
        src = "const { FOO_URL, BAR_KEY: local } = process.env;\n"
        usages = o2guard.extract_variable_usages(Path("app.ts"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"FOO_URL", "BAR_KEY"})

    def test_python_all_patterns(self):
        src = """
import os
a = os.environ["BACKEND_URL"]
b = os.getenv("GEMINI_KEY")
c = os.environ.get("GITHUB_TOKEN")
"""
        usages = o2guard.extract_variable_usages(Path("worker.py"), src)
        found = {u.variable for u in usages}
        self.assertEqual(found, {"BACKEND_URL", "GEMINI_KEY", "GITHUB_TOKEN"})

    def test_unsupported_extension_returns_empty(self):
        src = 'process.env.SOME_VAR\n'
        usages = o2guard.extract_variable_usages(Path("config.json"), src)
        self.assertEqual(usages, [])


# ---------------------------------------------------------------------------
# Registry building
# ---------------------------------------------------------------------------

class TestBuildRegistry(unittest.TestCase):
    def test_loads_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".env").write_text("DATABASE_URL=postgres://localhost\nAPI_KEY=secret\n")
            registry = o2guard.build_registry(repo)
            self.assertIn("DATABASE_URL", registry)
            self.assertIn("API_KEY", registry)

    def test_loads_from_o2registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".o2registry").write_text(
                "# comment\n"
                "MY_SERVICE_URL=Backend service URL\n"
                "ANOTHER_KEY\n"
            )
            registry = o2guard.build_registry(repo)
            self.assertIn("MY_SERVICE_URL", registry)
            self.assertIn("ANOTHER_KEY", registry)

    def test_loads_from_docker_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docker-compose.yml").write_text(
                "services:\n  app:\n    environment:\n      - MY_SECRET_KEY\n      - DB_URL=postgres\n"
            )
            registry = o2guard.build_registry(repo)
            self.assertIn("MY_SECRET_KEY", registry)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidateFiles(unittest.TestCase):
    def test_flags_unknown_variable_with_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["DATABASE_URL"])

            target = repo / "app.ts"
            target.write_text("const x = process.env.DB_URI;\n")

            registry = o2guard.build_registry(repo)
            issues = o2guard.validate_files(repo, [target], registry)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].variable, "DB_URI")
            self.assertEqual(issues[0].suggestion, "DATABASE_URL")

    def test_passes_known_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["DATABASE_URL"])

            target = repo / "app.ts"
            target.write_text("const x = process.env.DATABASE_URL;\n")

            registry = o2guard.build_registry(repo)
            issues = o2guard.validate_files(repo, [target], registry)
            self.assertEqual(issues, [])

    def test_passes_builtin_whitelist_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["MY_KEY"])

            target = repo / "app.ts"
            # PORT and NODE_ENV are in BUILTIN_WHITELIST
            target.write_text(
                "const p = process.env.PORT;\n"
                "const e = process.env.NODE_ENV;\n"
                "const k = process.env.MY_KEY;\n"
            )

            registry = o2guard.build_registry(repo)
            issues = o2guard.validate_files(repo, [target], registry)
            self.assertEqual(issues, [])

    def test_skips_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["FOO"])
            registry = o2guard.build_registry(repo)

            issues = o2guard.validate_files(repo, [repo / "does_not_exist.ts"], registry)
            self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------

class TestAutoFix(unittest.TestCase):
    def test_replaces_variable_with_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["DATABASE_URL"])

            target = repo / "app.ts"
            target.write_text(
                "const db = process.env.DB_URI;\n"
                "const key = process.env.DATABASE_URL;\n"
            )

            registry = o2guard.build_registry(repo)
            issues = o2guard.validate_files(repo, [target], registry)
            self.assertEqual(len(issues), 1)
            self.assertIsNotNone(issues[0].suggestion)

            fixed = o2guard.auto_fix_issues(repo, issues)
            self.assertEqual(fixed, 1)
            content = target.read_text()
            self.assertIn("DATABASE_URL", content)
            self.assertNotIn("DB_URI", content)

    def test_skips_issues_without_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, [])
            issues = [
                o2guard.ValidationIssue(
                    file=repo / "fake.ts", line=1, variable="ZZZZZZZ", suggestion=None
                )
            ]
            fixed = o2guard.auto_fix_issues(repo, issues)
            self.assertEqual(fixed, 0)


# ---------------------------------------------------------------------------
# Ignore file
# ---------------------------------------------------------------------------

class TestIgnoreFile(unittest.TestCase):
    def test_reads_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".o2guardignore").write_text("# comment\ntests/\nsandbox/\n")
            patterns = o2guard.load_ignore_patterns(repo)
            self.assertEqual(patterns, ["tests/", "sandbox/"])

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            patterns = o2guard.load_ignore_patterns(Path(tmp))
            self.assertEqual(patterns, [])

    def test_suppresses_flagged_files_in_ignored_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))
            _write_registry(repo, ["REAL_KEY"])

            target = repo / "tests" / "fixtures.ts"
            target.parent.mkdir()
            target.write_text("const x = process.env.TOTALLY_FAKE;\n")

            (repo / ".o2guardignore").write_text("tests/\n")

            registry = o2guard.build_registry(repo)
            patterns = o2guard.load_ignore_patterns(repo)
            files = [f for f in [target] if not o2guard._is_ignored(f, repo, patterns)]
            issues = o2guard.validate_files(repo, files, registry)
            self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# Git staged files
# ---------------------------------------------------------------------------

class TestCollectStagedFiles(unittest.TestCase):
    def test_returns_supported_staged_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _git_repo(Path(tmp))

            ts_file = repo / "app.ts"
            py_file = repo / "worker.py"
            txt_file = repo / "notes.txt"

            ts_file.write_text("const x = 1;\n")
            py_file.write_text("x = 1\n")
            txt_file.write_text("ignore\n")

            subprocess.run(
                ["git", "add", "app.ts", "worker.py", "notes.txt"],
                cwd=repo, check=True, capture_output=True,
            )

            staged = o2guard.collect_staged_files(repo)
            rel = sorted(p.relative_to(repo).as_posix() for p in staged)
            self.assertEqual(rel, ["app.ts", "worker.py"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
O² Guard — environment-variable validator for Python and TypeScript/JavaScript codebases.

Scans staged (or specified) files for env-variable usages and validates each one
against a registry built from your project's .env files, docker-compose, and
an optional .o2registry file.  Unknown variables are flagged; typo suggestions
are provided via fuzzy matching.

Usage as a pre-commit hook:
    o2guard.py --staged

Usage in CI (scan specific files):
    o2guard.py --files src/app.ts src/worker.py

Auto-fix typos in place:
    o2guard.py --files src/app.ts --auto-fix

Ignore paths (or add .o2guardignore):
    o2guard.py --staged --exclude-paths tests/ sandbox/
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class RegistrySource(str, Enum):
    ENV_FILE = "EnvFile"
    DOCKER_COMPOSE = "DockerCompose"
    O2_REGISTRY = "O2Registry"


@dataclass(frozen=True)
class RegistryEntry:
    key: str
    source: RegistrySource
    description: str = ""


@dataclass(frozen=True)
class VariableUsage:
    file: Path
    line: int
    variable: str


@dataclass(frozen=True)
class ValidationIssue:
    file: Path
    line: int
    variable: str
    suggestion: str | None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".py"}

# Regex matching a valid ALL_CAPS env-variable name
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")

# Regex for KEY=value assignments in .env files
ENV_ASSIGN_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)

# TypeScript / JavaScript env usage patterns
TS_PATTERNS = (
    re.compile(r"\bprocess\.env\.([A-Z][A-Z0-9_]*)\b"),
    re.compile(r"\bprocess\.env\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
)

# TypeScript destructuring:  const { FOO, BAR } = process.env
TS_DESTRUCTURE_RE = re.compile(r"\{([^}]*)\}\s*=\s*process\.env\b", re.MULTILINE)

# Python env usage patterns
PY_PATTERNS = (
    re.compile(r"\bos\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
    re.compile(r"\bos\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\bos\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    re.compile(r"\benviron\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
)

# Comment patterns (for stripping before scanning)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(//|#).*$", re.MULTILINE)

# Well-known universal system / CI / framework variables – never project-specific.
# Extend this list in your .o2registry file with a leading `!` to un-whitelist.
BUILTIN_WHITELIST: frozenset[str] = frozenset(
    {
        # POSIX / Windows
        "HOME", "USER", "USERNAME", "USERPROFILE",
        "PATH", "SHELL", "LANG", "LC_ALL",
        "TEMP", "TMP", "TMPDIR",
        "PWD", "OLDPWD",
        "HOSTNAME", "COMPUTERNAME",
        "SYSTEMROOT", "WINDIR",
        "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
        # Generic server / framework
        "PORT", "HOST", "NODE_ENV", "PYTHON_ENV",
        # CI systems (GitHub Actions, Azure DevOps, GitLab, CircleCI …)
        "CI", "TF_BUILD", "GITHUB_ACTIONS", "GITLAB_CI",
        "BUILD_BUILDID", "BUILD_SOURCEBRANCH",
        "RUNNER_OS", "RUNNER_TEMP",
    }
)


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

def strip_comments(content: str) -> str:
    """
    Replace comment text with whitespace, preserving original line numbers.

    Handles:
    - Block comments  /* ... */   (JS/TS)
    - Line comments   // ...      (JS/TS)
    - Hash comments   # ...       (Python)
    """
    def _preserve_newlines(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    result = _BLOCK_COMMENT_RE.sub(_preserve_newlines, content)
    result = _LINE_COMMENT_RE.sub("", result)
    return result


# ---------------------------------------------------------------------------
# Registry builders
# ---------------------------------------------------------------------------

def _add_entry(
    registry: dict[str, RegistryEntry],
    key: str,
    source: RegistrySource,
    description: str = "",
) -> None:
    if ENV_NAME_RE.fullmatch(key):
        registry.setdefault(key, RegistryEntry(key=key, source=source, description=description))


def _load_from_env_files(repo_root: Path, registry: dict[str, RegistryEntry]) -> None:
    """Collect variable names from any .env* file in the repository."""
    for path in repo_root.rglob(".env*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for m in ENV_ASSIGN_RE.finditer(content):
            _add_entry(registry, m.group(1), RegistrySource.ENV_FILE)


def _load_from_docker_compose(repo_root: Path, registry: dict[str, RegistryEntry]) -> None:
    """Collect variable names from docker-compose.yml / docker-compose.yaml."""
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        path = repo_root / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"-\s*([A-Z][A-Z0-9_]*)\s*(?:=|$)", content, re.MULTILINE):
            _add_entry(registry, m.group(1), RegistrySource.DOCKER_COMPOSE)


def _load_from_o2registry(repo_root: Path, registry: dict[str, RegistryEntry]) -> None:
    """
    Collect variable names from .o2registry (one entry per line).

    Format:
        # comment
        MY_API_KEY          # just the name
        MY_OTHER_KEY=The description goes here   # name + optional description
    """
    path = repo_root / ".o2registry"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            key, _, desc = line.partition("=")
        else:
            key, desc = line, ""
        key = key.strip()
        _add_entry(registry, key, RegistrySource.O2_REGISTRY, desc.strip())


def build_registry(repo_root: Path) -> dict[str, RegistryEntry]:
    """Build the combined registry from all supported sources."""
    registry: dict[str, RegistryEntry] = {}
    _load_from_env_files(repo_root, registry)
    _load_from_docker_compose(repo_root, registry)
    _load_from_o2registry(repo_root, registry)
    return registry


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------

def _line_number(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _extract_direct(
    file_path: Path,
    content: str,
    patterns: Iterable[re.Pattern[str]],
) -> list[VariableUsage]:
    usages: list[VariableUsage] = []
    for pattern in patterns:
        for m in pattern.finditer(content):
            usages.append(
                VariableUsage(
                    file=file_path,
                    line=_line_number(content, m.start()),
                    variable=m.group(1),
                )
            )
    return usages


def _extract_ts_destructured(file_path: Path, content: str) -> list[VariableUsage]:
    """Handle:  const { FOO, BAR: localAlias } = process.env"""
    usages: list[VariableUsage] = []
    for m in TS_DESTRUCTURE_RE.finditer(content):
        line = _line_number(content, m.start())
        for item in m.group(1).split(","):
            key = item.strip().split(":", 1)[0].split("=", 1)[0].strip()
            if ENV_NAME_RE.fullmatch(key):
                usages.append(VariableUsage(file=file_path, line=line, variable=key))
    return usages


def extract_variable_usages(file_path: Path, content: str) -> list[VariableUsage]:
    """Return deduplicated, sorted list of env-variable usages in *content*."""
    suffix = file_path.suffix.lower()
    cleaned = strip_comments(content)

    usages: list[VariableUsage] = []
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        usages.extend(_extract_direct(file_path, cleaned, TS_PATTERNS))
        usages.extend(_extract_ts_destructured(file_path, cleaned))
    elif suffix == ".py":
        usages.extend(_extract_direct(file_path, cleaned, PY_PATTERNS))

    seen: dict[tuple[int, str], VariableUsage] = {}
    for u in usages:
        seen[(u.line, u.variable)] = u
    return sorted(seen.values(), key=lambda u: (u.line, u.variable))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_files(
    repo_root: Path,
    files: Sequence[Path],
    registry: dict[str, RegistryEntry],
    whitelist: frozenset[str] = BUILTIN_WHITELIST,
) -> list[ValidationIssue]:
    """Return validation issues across all *files*."""
    known = set(registry.keys()) | whitelist
    keys = sorted(registry.keys())
    issues: list[ValidationIssue] = []

    for path in files:
        absolute = path if path.is_absolute() else repo_root / path
        if not absolute.exists():
            continue
        content = absolute.read_text(encoding="utf-8", errors="ignore")
        for usage in extract_variable_usages(absolute, content):
            if usage.variable in known:
                continue
            suggestion = _suggest(usage.variable, keys)
            issues.append(
                ValidationIssue(
                    file=absolute,
                    line=usage.line,
                    variable=usage.variable,
                    suggestion=suggestion,
                )
            )

    return sorted(issues, key=lambda i: (i.file.as_posix(), i.line, i.variable))


def _suggest(variable: str, keys: Sequence[str]) -> str | None:
    matches = difflib.get_close_matches(variable, keys, n=1, cutoff=0.3)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------

def auto_fix_issues(repo_root: Path, issues: Sequence[ValidationIssue]) -> int:
    """Replace variables with their suggestions in-place.  Returns fix count."""
    fixable = [i for i in issues if i.suggestion]
    if not fixable:
        return 0

    grouped: dict[Path, list[ValidationIssue]] = {}
    for issue in fixable:
        grouped.setdefault(issue.file, []).append(issue)

    fixed = 0
    for path, file_issues in grouped.items():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for issue in file_issues:
            idx = issue.line - 1
            if idx >= len(lines):
                continue
            old_line = lines[idx]
            new_line = old_line.replace(issue.variable, issue.suggestion)  # type: ignore[arg-type]
            if new_line != old_line:
                lines[idx] = new_line
                rel = path.relative_to(repo_root).as_posix()
                print(f"  fixed {rel}:{issue.line}  {issue.variable!r} -> {issue.suggestion!r}")
                fixed += 1
        path.write_text("".join(lines), encoding="utf-8")
    return fixed


# ---------------------------------------------------------------------------
# Ignore file
# ---------------------------------------------------------------------------

def load_ignore_patterns(repo_root: Path) -> list[str]:
    """Read .o2guardignore from the repo root (gitignore-style globs)."""
    ignore_file = repo_root / ".o2guardignore"
    if not ignore_file.exists():
        return []
    return [
        raw.strip()
        for raw in ignore_file.read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    ]


def _is_ignored(path: Path, repo_root: Path, patterns: list[str]) -> bool:
    try:
        relative = path.relative_to(repo_root).as_posix()
    except ValueError:
        return False
    for pattern in patterns:
        norm = pattern.rstrip("/")
        if relative == norm or relative.startswith(norm + "/"):
            return True
        if fnmatch.fnmatch(relative, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Git integration
# ---------------------------------------------------------------------------

def collect_staged_files(repo_root: Path) -> list[Path]:
    """Return all staged files with a supported extension."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff --cached failed")
    return [
        repo_root / line.strip()
        for line in result.stdout.splitlines()
        if Path(line.strip()).suffix.lower() in SUPPORTED_SUFFIXES
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="o2guard",
        description="Validate env-variable usage against your project registry.",
    )
    p.add_argument("--repo-root", default=".", metavar="DIR",
                   help="Path to the repository root (default: .)")
    p.add_argument("--staged", action="store_true",
                   help="Scan git-staged files (use as a pre-commit hook)")
    p.add_argument("--files", nargs="*", default=[], metavar="FILE",
                   help="Explicit list of files to validate")
    p.add_argument("--auto-fix", action="store_true",
                   help="Auto-replace variables that have a close-match suggestion")
    p.add_argument("--exclude-paths", nargs="*", default=[], metavar="PATH",
                   help="Path prefixes (relative to repo root) to exclude")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    repo_root = Path(args.repo_root).resolve()

    registry = build_registry(repo_root)
    if not registry:
        print("o2guard: registry is empty — add a .o2registry file or .env files.")
        return 0

    if args.staged:
        files = collect_staged_files(repo_root)
    else:
        files = [Path(f) for f in args.files]

    ignore_patterns = load_ignore_patterns(repo_root) + list(args.exclude_paths)
    files = [
        f for f in files
        if not _is_ignored(f if f.is_absolute() else repo_root / f, repo_root, ignore_patterns)
    ]

    if not files:
        print("o2guard: no files to validate.")
        return 0

    issues = validate_files(repo_root, files, registry)

    if not issues:
        print(f"o2guard: ✓ passed ({len(files)} file(s), {len(registry)} registry entries).")
        return 0

    if args.auto_fix:
        fixed = auto_fix_issues(repo_root, issues)
        remaining = len(issues) - fixed
        print(f"o2guard: auto-fixed {fixed} issue(s), {remaining} remaining.")
        return 1 if remaining > 0 else 0

    print(f"o2guard: {len(issues)} issue(s) found.\n")
    for issue in issues:
        rel = issue.file.relative_to(repo_root).as_posix()
        hint = f"  (did you mean `{issue.suggestion}`?)" if issue.suggestion else ""
        print(f"  {rel}:{issue.line}  unknown env var `{issue.variable}`{hint}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

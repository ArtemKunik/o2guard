# O² Guard

**A zero-dependency environment-variable validator for Python and TypeScript/JavaScript codebases.**

Stop shipping code that references `DB_URI` when your `.env` says `DATABASE_URL`.  
O² Guard catches those typos at commit time — before they ever reach production.

---

## How it works

O² Guard builds a **registry** of allowed env-variable names from your project's:

| Source | File |
|--------|------|
| `.env` / `.env.local` / `.env.production` … | any `.env*` file in the repo |
| Docker Compose | `docker-compose.yml` / `docker-compose.yaml` |
| Explicit registry | `.o2registry` (one name per line) |

Then it scans your source files for env-variable usages:

| Language | Patterns detected |
|----------|-------------------|
| TypeScript / JavaScript | `process.env.FOO`, `process.env["FOO"]`, `const { FOO } = process.env` |
| Python | `os.environ["FOO"]`, `os.getenv("FOO")`, `os.environ.get("FOO")` |

Comments (`// …`, `/* … */`, `# …`) are stripped before scanning, so commented-out references never trigger false positives.

Unrecognised variables are flagged with a fuzzy-match suggestion:

```
o2guard: 1 issue(s) found.

  src/db.ts:12  unknown env var `DB_URI`  (did you mean `DATABASE_URL`?)
```

---

## Install

No PyPI package needed — it's a single file.

```bash
curl -O https://raw.githubusercontent.com/ArtemKunik/o2guard/main/o2guard.py
```

Or clone:

```bash
git clone https://github.com/ArtemKunik/o2guard.git
```

Python ≥ 3.10 required.  No third-party dependencies.

---

## Usage

### As a pre-commit hook (recommended)

```bash
# .git/hooks/pre-commit
#!/bin/sh
python o2guard.py --staged || exit 1
```

```bash
chmod +x .git/hooks/pre-commit
```

### Scan specific files

```bash
python o2guard.py --files src/app.ts src/worker.py
```

### Auto-fix typos

```bash
python o2guard.py --files src/app.ts --auto-fix
```

### Exclude paths

```bash
python o2guard.py --staged --exclude-paths tests/ sandbox/
```

---

## Registry sources

### `.o2registry` (explicit list)

Create a `.o2registry` file in your repo root:

```
# Database
DATABASE_URL=PostgreSQL connection string
DATABASE_POOL_SIZE

# API keys
OPENAI_API_KEY
STRIPE_SECRET_KEY=Stripe secret key (never expose client-side)

# Services
REDIS_URL
BACKEND_URL=Internal backend service URL
```

Lines starting with `#` are comments.  Everything after `=` is an optional description.

### `.env` files

Any file matching `.env*` anywhere in the repo is parsed for `KEY=value` assignments.

### `docker-compose.yml`

Variables listed under `environment:` are automatically included.

---

## Ignore file

Create `.o2guardignore` in the repo root (same syntax as `.gitignore`):

```
# Don't validate test fixtures
tests/
e2e/fixtures/

# Don't validate generated code
src/generated/

# Glob patterns work too
**/*.spec.ts
```

---

## CLI reference

```
usage: o2guard [-h] [--repo-root DIR] [--staged] [--files [FILE ...]]
               [--auto-fix] [--exclude-paths [PATH ...]]

options:
  --repo-root DIR         Path to the repository root (default: .)
  --staged                Scan git-staged files (use as a pre-commit hook)
  --files FILE [...]      Explicit list of files to validate
  --auto-fix              Auto-replace variables that have a close-match suggestion
  --exclude-paths PATH    Path prefixes (relative to repo root) to exclude
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | All variables known — clean |
| `1` | Unknown variables found (or unfixed issues after `--auto-fix`) |

---

## Running the tests

```bash
python -m pytest tests/ -v
```

---

## Builtin whitelist

A set of universal system / CI variables is always allowed and never needs to be in your registry:

`CI`, `TF_BUILD`, `GITHUB_ACTIONS`, `PORT`, `HOST`, `NODE_ENV`, `PATH`, `HOME`, `USER`, `TEMP`, `TMP`, and standard Windows system vars.

---

## License

MIT

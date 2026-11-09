# Contributing

## Language policy (English-only)

All content in this repository MUST be written in English. This is a hard rule and applies to:

- source code identifiers (modules, classes, functions, variables, constants);
- code comments and docstrings;
- documentation (`README.md`, `SPECIFICATION.md`, `core/*.md`, etc.);
- configuration files and their comments (`.env.example`, `pyproject.toml`, CI workflows, etc.);
- commit messages, branch names and pull-request titles/descriptions;
- test names, assertion messages and fixtures.

No Russian (Cyrillic) text is allowed anywhere in the tracked repository. New contributions must
be in English from the start — do not add Russian text with the intention of translating it later.

This rule is enforced automatically: a CI-blocking test (`tests/test_language_policy.py`) fails if
any Cyrillic character is found in tracked files. If you must reference a proper noun that only
exists in Cyrillic, transliterate it to the Latin alphabet.

### Exemptions

The following documents are intentionally kept in Russian by owner decision and are whitelisted in
`tests/test_language_policy.py` (keep this list in sync with the test's `_EXEMPT` set):

- `IMPLEMENTATION_PLAN.md` — internal planning document;
- `core/EXPLANATORY_NOTE.md` — explanatory note.

(`deepseek-criticism.md` is git-ignored and therefore not subject to the policy.)

## Tooling

- Backend: `pip install -e ".[dev]"`, then `ruff check .`, `ruff format --check .`, `mypy`, `pytest -q`.
- Frontend: `pnpm install` and `pnpm -r lint && pnpm -r build`.

## Commits

- Write commit messages in English, imperative mood (e.g. "Add RLS contract tests").
- Keep the subject line concise; use the body to explain *why* when needed.

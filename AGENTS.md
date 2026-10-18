# Agent instructions (Codex, Gemini CLI, Claude Code)

Source of truth: [docs/PLAN.md](docs/PLAN.md). Read it first; keep changes inside your owner's folders (listed there).

Non-negotiables:
- Snowflake computes every answer. Gemma plans the SQL and writes answer templates; no arithmetic in Python or in the model.
- Model SQL runs only after `datamind/gate.py` accepts it, under the read-only role `DATAMIND_READER`.
- Every answer shows its executed SQL, result rows and Snowflake query ID.
- Secrets live only in the git-ignored `.env`: never read, print or commit it. Run `python scripts/secret_scan.py` and `python -m pytest` before every commit.
- No TPC-H data rows in the repo: commit SQL, query IDs and hashes only.

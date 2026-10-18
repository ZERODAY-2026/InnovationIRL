---
name: commit-check
description: Use before every git commit in the DataMind repo. Runs the secret scan and the test suite and commits only if both pass.
---

1. Run `.venv/Scripts/python scripts/secret_scan.py`. It must print `secret scan: clean`. On findings, fix or unstage the file; never print matched values.
2. Run `.venv/Scripts/python -m pytest`. It must pass. Report the pass count and at most the first 3 failures.
3. Stage explicit paths only (no `git add -A`), never `.env` or key files, and commit with a short imperative subject.
4. Never push, pull or touch the remote: the team does that.

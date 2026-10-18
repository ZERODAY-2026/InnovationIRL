---
name: eval-run
description: Use when asked to measure DataMind accuracy or compare Gemma tiers. Runs an eval set through the live pipeline against gold SQL and reports counts.
---

1. Pick the set: `eval/dev_pipeline.yaml` is for tuning. Held-out sets (`eval/heldout_*.yaml`) are run only at checkpoints and never used to edit prompts or profiles.
2. Run serially, one model call at a time:
   `.venv/Scripts/python -m datamind.evaluate <set> --label <name> [--plan-model gemma-4-31b-it] [--plan-thinking minimal|default] [--answers model]`
   Run it in the background and read only the final summary lines. A 28-question set takes about 3 minutes with minimal thinking and about 10 minutes with thinking on.
3. Report as `overall x/y, answers x/y, refusals x/y, plan median ms`, then the first 3 failures with their reasons. Never edit results.
4. Results go to `eval/results/*.json` (SQL, query IDs and verdicts only). Summarise them honestly in `docs/EVAL.md`.

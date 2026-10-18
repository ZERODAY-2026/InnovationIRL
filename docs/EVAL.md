# Evaluation

Last updated 18 Oct 2026, 3:25 PM IST. All numbers below come from saved runs in `eval/results/`.

## Method
Every evaluation question has **gold SQL written by hand**, independently of DataMind. For each question:
1. The gold SQL runs in Snowflake.
2. The question goes through the full DataMind pipeline: Gemma plan, gate, `EXPLAIN`, execution in Snowflake, and the answer.
3. The two results are compared by value:
   - Integers must match exactly.
   - Decimals may differ only by Snowflake's 6-decimal division noise.
   - Rounding counts as a match only if at least 2 decimals or 4 significant digits are kept.
   - A percentage may match a fraction (×100).
   - Extra columns and a different column order are allowed.
   - Row order counts only for rankings.

Questions that should be refused pass only if the status is UNSAFE or UNSUPPORTED **and no query ran**.

Each run file stores the generated SQL, the Snowflake query IDs of both queries, and the verdicts. It never stores data rows.

## Question sets
| Set | Written by | Purpose | Status |
|---|---|---|---|
| `eval/dev_pipeline.yaml`, 28 questions: 24 to answer, 2 unsafe, 2 unsupported | The pipeline lane, which also writes the prompts | Development and tier comparison | Run (below) |
| Held-out A | Snowflake and eval lane, never shown to prompt writers | Checkpoint | **Not run yet** |
| Held-out B (sealed) | UI and docs lane | Final, run once | **Not run yet** |

The development set covers aggregates, filters, joins, rankings, time series, multi-step questions, follow-ups, ambiguity, out-of-coverage periods, empty results and safety. **Its scores are optimistic**, because the same lane wrote the questions and the prompts.

## Results: development set
| Run | Started (UTC) | PLAN model | Thinking | Wording | Overall | Answers | Refusals | Plan median | Repairs | Failed |
|---|---|---|---|---|---|---|---|---|---|---|
| 26b-minimal | 2026-10-18T09:25 | gemma-4-26b-a4b-it | minimal | code | 26/28 | 22/24 | 4/4 | 3.9 s | 4 | d17, d25: pipeline bugs (fixed, see below) |
| 26b-minimal-v2 | 2026-10-18T09:34 | gemma-4-26b-a4b-it | minimal | code | 27/28 | 23/24 | 4/4 | 5.3 s | 5 | d17: plan-parser bug (fixed after this run) |
| 26b-thinking | 2026-10-18T09:34 | gemma-4-26b-a4b-it | default (thinking on) | code | 26/28 | 22/24 | 4/4 | 17.9 s | 0 | d17, d18: Gemini timed out (>60 s) |
| 31b-minimal | 2026-10-18T09:34 | gemma-4-31b-it | minimal | code | 22/28 | 18/24 | 4/4 | 29.6 s | 2 | d06, d08, d11, d15, d17, d28: Gemini HTTP 500/503 service errors |
| **26b-minimal-final** | 2026-10-18T09:46 | gemma-4-26b-a4b-it | minimal | Gemma | **28/28** | **24/24** | **4/4** | cached | 1 | none |
| 26b-minimal-final-coherence | 2026-10-18T09:50 | gemma-4-26b-a4b-it | minimal | Gemma | 28/28 | 24/24 | 4/4 | cached | 1 | none (re-check after the row-coherence rule) |

"Wording" says who wrote the answer text: `code` means the deterministic answer, used for plan-only runs; `Gemma` means the full pipeline. "Cached" means the plans came from the prompt-hash cache. The prompts were identical to the earlier run, and plans are deterministic at temperature 0.

### Answer grounding (full pipeline)
In the final run, 22 questions were answered with Gemma's wording, and all 22 were grounded. Every displayed number is either a result cell or comes from the question, the row count or an audit value (data coverage, or a filter in the executed SQL). Gemma never put a cell's value next to the wrong row's label. No Gemma text had to be rejected. One answer (d25, an empty result) is deterministic by design and names the filter that matched nothing.

## Tier decision (PLAN role)
| Tier | Accuracy on completed questions | Latency | Reliability | Decision |
|---|---|---|---|---|
| 26B, minimal thinking | 24/24 | about 5 s median | rate-limited at about 8 calls a minute; the runner pauses 30 s on a 429 | **Default** |
| 26B, thinking on | 22/22 completed | 17.9 s median | 2 timeouts above 60 s on multi-step questions | Available via `GEMINI_THINKING_PLAN=default` |
| 31B, minimal thinking | 18/18 completed | 29.6 s median | 6 of 28 questions failed on Gemini 500/503 | Not used; 26B already meets the accuracy target |
| Gemma 4 E4B, local (Ollama) | not measured | | `OLLAMA_HOST` was not set on the evaluation machine | Backup path covered by mocked tests only |

## Changes made after seeing development failures
All of these are generic fixes. No prompt or profile was edited for a specific question.

- **d17**, a legitimate cross join of single-row totals, was rejected by the cartesian check. The check now walks Snowflake's `EXPLAIN` plan (`parentOperators`), and flags only table scans that feed a cartesian join.
- **d25**: Gemma compared a numeric key with a customer name, and the query failed while running. The gate now rejects number or date columns compared with text, and errors at run time get a repair round.
- **Row-coherence rule**, found in Verified Insights rather than in this set: a clause may not pair one row's label with another row's value.

## Evidence
- `eval/results/*_dev_pipeline_*.json`: generated SQL, Snowflake query IDs (system and gold) and verdicts for every question.
- `eval/results/query_history_*.csv`: Snowflake's own `QUERY_HISTORY` for every DataMind-tagged query. Exported with `python -m datamind.history`.
- The live Phase 2 exit-test query IDs are in `docs/PLAN.md`.

## Known limitations
- No held-out score yet. Treat the development numbers as an upper bound.
- The free Gemini tier rate-limits. Long sessions see 30 s pauses unless the local Ollama backup is configured.
- With `OLLAMA_NUM_CTX=2048`, the local backup only fits simpler, mostly single-table prompts.

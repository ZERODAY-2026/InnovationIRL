# DataMind (PS1): plan and status

Last updated 18 Oct 2026, 3:25 PM IST. Final deadline 6:00 PM IST.

## Status: done / next
Something is listed as done only after it has actually been run.

**Done (verified by 77 offline tests)**
- Problem analysis and the approved build plan (this document).
- Checkpoint 1 commit: a README with the problem statement first, the Apache-2.0 licence, a `.gitignore` covering secrets and key files, and `.env.example`.
- Pinned Python 3.13 environment.
- TPC-H semantic layer, which loads and validates.
- Read-only SQL gate:
  - 17 valid queries pass.
  - 32 attacks and mistakes are rejected, including DML/DDL, stacked statements, `SYSTEM$` calls, table functions, other databases, unknown columns, undeclared key joins, and constant-only "answers".
- Gemma client logic, tested against mocked APIs:
  - the Gemini tier, with probing for system-instruction and JSON-mode support
  - Ollama backup on a 429, a timeout or a connection error
  - pydantic-validated JSON
  - the local context budget, with truncation detection
  - the plan cache

**Vertical slice code (verified by 106 offline tests with fake Gemma and Snowflake):**
- Pipeline: plan → gate → `EXPLAIN` → up to 2 repairs → execute → answer.
  - Refusals never reach Snowflake.
  - Empty results get an honest "no data matched".
- Cell-bound renderer. Invented numbers switch to a deterministic answer (tested on "top 5 nations by revenue in 1995").
- Receipt compiled from the executed SQL.
- CLI: `python -m datamind ask`.
- Review page: `app/slice_view.py`.
- Introspection.
- README setup for both Gemma paths.

**Live checks (run 18 Oct, 14:40–14:55 IST; 112 offline tests now)**
- Smoke test passed. The app connects as `DATAMIND_READER` on `DATAMIND_WH` (query `01c7cf2a-000e-16cf-0002-b98a0001603e`).
  - A DELETE as the app role is denied: insufficient privileges.
  - `SNOWFLAKE_SAMPLE_DATA` is not visible to the app.
- Gemma via the Gemini API returns schema-valid JSON on both model IDs, with system instructions and JSON mode supported. Gemma 4 thinks by default; `thinkingLevel: minimal` turns that off. Latency for a 1-line prompt:

  | Model | Thinking on | Minimal thinking |
  |---|---|---|
  | `gemma-4-26b-a4b-it` | 12.5 s | 1.1 s |
  | `gemma-4-31b-it` | 61 s | 23 s |

- Introspection cached the date coverage. Orders run from 1992-01-01 to 1998-08-02.
- Phase 2 exit test passed. Five questions were answered end to end from Snowflake with grounded values:

  | Question | Query ID |
  |---|---|
  | Top 5 nations by revenue in 1995 | `01c7cf2b-000e-1619-0000-0002b98ad319` |
  | Orders per priority | `01c7cf2c-000e-1673-0000-0002b98ac33d` |
  | Average discount per ship mode | `01c7cf2c-000e-16cf-0002-b98a00016056` |
  | Top 3 AUTOMOBILE customers by balance | `01c7cf2d-000e-16d0-0002-b98a00015136` |
  | Share of late line items shipped in 1997 | `01c7cf2d-000e-16d0-0002-b98a00015156` |

  "Delete all orders from 1992" was refused as UNSAFE and never reached Snowflake.
- Planning on 26B with thinking on took 8–32 s per question; the explain step with minimal thinking took 2–3 s.

**Phases 3–5 (as of 3:25 PM; 154 offline tests):**
- **Phase 3 (measured on the development set only):**
  - eval runner
  - value hints
  - out-of-coverage notes
  - zero-row diagnosis backed by a probe query
  - follow-ups
  - type-mismatch and `SELECT *` guards
  - run-time repair

  The development set scores 28/28, with 22/22 grounded answer texts. Tier decision: 26B with minimal thinking, now the default for every role. See `docs/EVAL.md`.
- **Phase 4 (built):**
  - re-run with a cell-level diff (CLI `ask --save` / `rerun`, UI button)
  - `snowflake/demo_change.sql` and `demo_revert.sql`
  - Snowflake query-history export
  - the OWID profile, subset CSV and seed generator, added through configuration only
- **Phase 5 (built):** Verified Insights. A live run showed 1 finding with its query ID, and dropped 2 unverifiable ones with reasons.

**Phase 4 live: the answer changes when the data changes (run 18 Oct, about 3:35 PM).** The question was "Which 5 nations had the highest revenue in 1995?"

| | Query ID | Top 5 |
|---|---|---|
| Before | `01c7cf45-000e-16d0-0002-b98a0001523a` | INDONESIA, FRANCE, RUSSIA, MOZAMBIQUE, CANADA |
| After `demo_change.sql` (one tagged order of 60,000,000 for a JAPAN customer, run as `DATAMIND_ADMIN`) | `01c7cf65-000e-1673-0000-0002b98ac51d` | INDONESIA, JAPAN, FRANCE, RUSSIA, MOZAMBIQUE |

- The diff reported "JAPAN: new in the result" and "CANADA: no longer in the result".
- The same SQL ran again with no new plan and no new model call. The answer text re-rendered from Gemma's original cell-pointer template, now naming JAPAN.

**Waiting on a team action:**
- `demo_revert.sql`.
- The OWID seed SQL, run as `DATAMIND_ADMIN` (the app role is read-only).
- Held-out set A.

**Not working yet:**
- No held-out score.
- The Ollama backup is untested live.
- No chart; the review UI is minimal, and the main UI belongs to the UI lane.

## Approach: compile, don't chat
1. **Plan (Gemma).** One structured call returns:
   - a status: answer, clarify, unsupported or unsafe
   - a standalone version of the question
   - the assumptions made
   - the SQL

   It works from a YAML semantic layer: tables, declared join paths, metric definitions, synonyms, valid values and date coverage.
2. **Gate (deterministic).**
   - sqlglot accepts only a single read-only SELECT over allow-listed tables.
   - Every column must exist.
   - Key joins must match declared paths.
   - A row limit is added.
   - Snowflake `EXPLAIN` then compiles the query without running it and rejects cartesian joins.
   - A failed check gets at most two repair attempts, then the system refuses clearly.
3. **Execute (Snowflake).** The query runs under a read-only role on an XS warehouse, with a timeout and a query tag.
4. **Explain (Gemma).** Gemma writes the answer as a template. Every result value is a `{{r_i.COL}}` pointer that code fills in and formats: thousands separators, 2 decimals for money, no float noise.
   - A plain number is allowed in the prose only if it appears in the question, matches query metadata (row count, date coverage), or equals a cell value.
   - Anything else switches to a deterministic answer.
5. **Receipt.** A plain-language account of the tables, joins, filters, metric definitions and limits, compiled from the executed SQL. It is shown next to the SQL, the rows and the query ID.

Snowflake computes every number. Python only validates, renders and compares.

## Why this design
Three designs other teams are likely to build, and how ours differs:

| Common design | Weakness | Our answer |
|---|---|---|
| Full schema to an LLM, then LLM-written SQL, then an LLM summary | Numbers and explanations are free prose | Values are cell pointers; the explanation is compiled from what ran |
| Cortex Analyst with Gemma as summariser | Gemma does no central work | Gemma plans every query |
| ReAct or multi-agent loop with vector RAG over the schema | Many calls, slow, hard to trace | Two bounded Gemma calls with deterministic gates between them |

| Novel element | Rubric target | How we prove it |
|---|---|---|
| Cell-bound answers | Evidence and traceability; grounding | Eval log traces 100% of displayed values; injected-number tests |
| Receipt compiled from the executed SQL | Explanation matches what ran | Unit tests |
| Semantic gate, read-only role, query tags | Snowflake integration; safety | ≥25 adversarial tests; a DELETE run as the app role is denied |
| Data-anchored honesty: dates checked against coverage, fuzzy value matching, "0 rows matched" diagnosis | Ambiguity and missing data | Eval cases for each |
| Live re-run diff: plans cached, results never | Answers change when the data changes | Recording with two query IDs |
| A new dataset needs only YAML and setup SQL | Reusability | OWID profile with no code changes |

**Main technical contribution (draft):** DataMind compiles questions instead of chatting about them.
- Gemma writes a typed plan over a semantic layer defined in config.
- A deterministic gate decides what may run, and Snowflake computes every number.
- Each answer is a template whose values point to cells of the executed query, explained by a receipt compiled from that query's AST.

Claim, query ID and row are linked by construction, so answers re-render correctly when the data changes.

## Gemma tiers
- **Primary, for every role (plan, explain, insights):** Gemma through the Gemini API, `gemma-4-26b-a4b-it`. If the 26B model misses the accuracy target for planning, we try `gemma-4-31b-it`.
- **Backup:** Ollama Gemma 4 E4B (QAT build) on a teammate's RTX 3050, reached on the LAN through `OLLAMA_HOST`.
  - `OLLAMA_NUM_CTX` is set per machine, 2048–4096, with at most 1–2 concurrent calls.
  - Prompt tokens are counted before every local call. Prompts are shrunk if needed, or the call fails loudly. Ollama is never allowed to truncate silently.
- **Fallback:** only on a 429, a timeout or a connection error. Every answer shows which tier produced it. Plans are cached by prompt hash.
- **Cloud data:** the cloud receives only the schema, the question and aggregated rows.
- **Measurement (about 3:15 PM):** the dev set is run through the plan role on 26B and 31B, with local E4B as a reference row. We record accuracy and latency in `docs/EVAL.md`.
- **README:** documents both the keyed cloud setup and the no-key local setup.

## Verified Insights ("Dig deeper", optional)
1. Gemma proposes up to 3 follow-up analyses: trend, share, outlier or period comparison.
2. Each analysis's SQL must output explicit computed columns such as PCT_CHANGE, SHARE or Z_SCORE.
3. Each query passes the same gate, with caps: LIMIT 50, 20 s, 3 queries per click.
4. Snowflake runs them.
5. Every sentence is checked: numbers must be cell pointers, and trend words must cite a matching column. Sentences that fail are dropped and counted.

Insights appear in a separate panel with query IDs.

## Phases (IST)
| Time | Phase | Exit test |
|---|---|---|
| 1:10–1:45 | 1 Foundations | Tests green (≥25 gate cases); Gemini returns schema-valid JSON; Ollama tested only if `OLLAMA_HOST` is set; Snowflake smoke test once the account exists |
| 1:45–2:45 | 2 Vertical slice (CLI and Streamlit) | 5 dev questions answered end to end with query IDs, all values grounded, DML refused |
| 2:45–3:45 | 3 Value anchoring, few-shot examples, repairs, statuses, empty-result handling, follow-ups, eval runner, tier measurement | Held-out set A reported in `docs/EVAL.md`; target ≥80% correct, 100% of unsafe requests refused |
| 3:45–4:30 | 4 Re-run diff, data-change scripts, query-history export, chart, OWID profile | An answer changes across two query IDs; OWID answers 3 questions |
| 4:30–5:15 | 5 Verified Insights, accessibility | Only grounded findings are shown |
| 5:15–6:00 | 6 Sealed held-out set B (run once), docs, demo recording, security review, final commit at 5:45 | README steps work from a fresh clone |

**Cut-line, in order:**
1. masking policy
2. Insights
3. chart
4. zero-row attribution (the plain "0 rows matched" message stays)
5. follow-ups
6. OWID
7. local Ollama backup

**Never cut:** the gate, the read-only role, the evidence panel, cell-bound answers, the held-out eval, the data-change demo, setup docs.

## Risks
| Risk | Mitigation |
|---|---|
| MFA blocks the connector from logging in | Key-pair auth on a `TYPE=SERVICE` user |
| Free-tier rate limits | Backoff, plan cache, LAN Ollama backup |
| Low accuracy on unseen questions | Metric definitions, value lists, few-shot examples, join checks, repairs; honest reporting |
| Credits run out or the trial expires | XS warehouse, auto-suspend after 60 s, resource monitor, timeouts; recorded run and query-history export |
| TPC-H redistribution | The data stays inside our Snowflake account; the repo holds only SQL, query IDs and hashes |

## Work split
| Who | Owns |
|---|---|
| Pipeline (with Claude Code) | `datamind/`, `tests/`, requirements, `.env.example`, `snowflake/setup.sql`, `scripts/secret_scan.py`, `scripts/gen_keypair.py`, this plan |
| Snowflake, data and eval | Snowflake account, `profiles/` semantics, `eval/` question sets with independent gold SQL, data-change scripts, OWID seed |
| Repo, UI, docs and demo | `app/` (Streamlit), `docs/` (except this plan), README, demo recording |

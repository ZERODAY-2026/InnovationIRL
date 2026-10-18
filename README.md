# InnovationIRL
# DataMind — PS1: Talk to Your Data

> **Problem statement.** *ZeroDay OSS Hacktoberfest 2026 (IIIT Dharwad), PS1 — DataMind: Talk to Your Data.* The people who understand the questions best are often the least able to reach the data: answers sit in databases and spreadsheets behind SQL skills, specialised queries or hours of manual searching. Build an AI-powered data experience where anyone can ask a question in everyday language and get a useful answer grounded in the data actually examined, with enough explanation that the user can trust it. The goal is not fluent text: the system must really query and analyse data, show its work, flag gaps, and change its answer when the data changes.

**Status: work in progress.** The design and plan are complete and implementation is starting. A feature is only described as working once its box is ticked in the [roadmap](#roadmap).

## What DataMind does

DataMind turns an everyday-language question into a checked, read-only SQL query, runs it in Snowflake, and answers using only what came back. Every answer shows the exact SQL, the result rows and the Snowflake query ID, so anyone can verify it independently. If a question is ambiguous, unsupported, unsafe or returns no rows, DataMind says so instead of guessing.

## How it works

1. **Plan:** Gemma reads the question together with a semantic layer (tables, join paths, metric definitions, synonyms, valid values and date coverage, all declared in a YAML profile) and returns a typed plan: answer, ask for clarification, unsupported, or unsafe, plus its assumptions and the SQL.
2. **Gate:** deterministic checks (sqlglot) allow only a read-only SELECT over allow-listed tables and declared join paths. Snowflake `EXPLAIN` compiles the query without running it. A failed check gets at most two repair attempts, then a clear refusal.
3. **Execute:** the query runs in Snowflake under a read-only role on a small warehouse, with a timeout and a query tag.
4. **Explain:** Gemma drafts the answer as a template whose values are pointers to cells of the executed result.
5. **Render:** the app fills in the values from the result, rejects any other number that is not in the result, the question or the query metadata, and shows a plain-language receipt (tables, joins, filters, assumptions) next to the SQL, rows and query ID.

Design rules: Snowflake computes every number; Gemma plans and explains but never does arithmetic; a new dataset needs configuration (a YAML profile and setup SQL), not new code.

## Technology

- **Snowflake:** storage, query execution and access control.
- **Google Gemma 4:** Gemma through the Gemini API (`gemma-4-26b-a4b-it`) is the primary tier. Gemma 4 E4B on Ollama (local or LAN) is the backup, used automatically on rate limits, timeouts or connection errors. A no-key setup runs entirely on Ollama.
- **Python:** snowflake-connector-python, sqlglot, pydantic, rapidfuzz, streamlit.

## Datasets and licences

| Dataset | Source | Licence | Use |
|---|---|---|---|
| TPC-H SF1 | Snowflake sample data (`SNOWFLAKE_SAMPLE_DATA`) | No open licence stated; used only inside our Snowflake account and never copied into this repository | Primary dataset: joins and aggregates with checkable ground truth |
| Our World in Data CO₂ and energy indicators | [ourworldindata.org](https://ourworldindata.org/) | CC BY 4.0 (to be re-checked on the dataset page when added) | Second dataset: configuration-only reuse and real missing-data handling |

## Planned repository layout

```text
datamind/    core pipeline (plan, gate, execute, explain, render)
profiles/    YAML semantic layers, one per dataset
snowflake/   reproducible setup SQL (roles, warehouse, tables)
eval/        evaluation question sets with independent gold SQL, and the runner
app/         Streamlit interface
scripts/     data-change demo, evidence export
tests/       unit and guard tests
docs/        architecture, evaluation results, evidence
```

## Roadmap

- [x] Problem analysis, scoring-driven design and plan
- [ ] Phase 1: foundations (contracts, profile loader, SQL gate, Snowflake executor, Gemma client, tests)
- [ ] Phase 2: vertical slice (question to evidence-backed answer)
- [ ] Phase 3: ambiguity and missing-data handling, follow-ups, evaluation harness
- [ ] Phase 4: live data-change demo and second dataset
- [ ] Phase 5: optional verified insights
- [ ] Phase 6: documentation, held-out evaluation, demo recording

## Evaluation

Accuracy will be measured on question sets whose ground truth is computed by independent, hand-written SQL: a development set and sealed held-out sets. Results will be published as measured in `docs/EVAL.md`. There are no results yet.

## Security and responsible data use

The app connects with a read-only Snowflake role using key-pair authentication. Secrets live in a git-ignored `.env` and are never committed. Only public benchmark data is used, and the optional cloud model sees only the schema, the question and aggregated rows.

## Setup and usage

Prerequisites, the Snowflake setup script, run instructions, configuration and troubleshooting will be added here as each phase lands.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
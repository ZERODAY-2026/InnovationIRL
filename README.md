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

### What each person needs
Each person keeps their own `.env`, copied from `.env.example`. Never commit it.

| You need | For | `.env` entries |
|---|---|---|
| Python 3.12+ (tested with 3.13.5 on Windows 11) | everything | none |
| The Snowflake account identifier, plus your own private key registered for `DATAMIND_APP` | running queries | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PRIVATE_KEY_PATH` |
| Gemma, through **either** a Gemini API key **or** an Ollama machine with Gemma 4 E4B | planning and explaining | `GEMINI_API_KEY`, or `OLLAMA_HOST` / `OLLAMA_MODEL` |

### 1. Install
```powershell
git clone <this repository>
cd InnovationIRL
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```
On macOS or Linux, use `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.txt` and `cp .env.example .env`.

### 2. Snowflake
1. **Once per account.** In a Snowsight worksheet, as `ACCOUNTADMIN`, run [snowflake/setup.sql](snowflake/setup.sql). It creates:
   - an XS warehouse with a 10-credit daily cap
   - the read-only `DATAMIND_READER` role
   - the `DATAMIND` database, holding the TPC-H SF1 copies
   - the `DATAMIND_APP` service user
2. **On each laptop.** Run `.venv\Scripts\python scripts/gen_keypair.py`. Then:
   - Run the `ALTER USER` line it prints, as `ACCOUNTADMIN`. A second laptop uses `--slot 2`; a third uses its own user (see `setup.sql`).
   - Put the printed `SNOWFLAKE_PRIVATE_KEY_PATH` and your account identifier (`ORGNAME-ACCOUNTNAME`) in `.env`.
3. **After setup and after any data change.** Run `.venv\Scripts\python -m datamind.introspect`. It caches date coverage and value lists in `profiles/cache/`, which is git-ignored.

### 3. Gemma: pick one setup
- **Keyed cloud (default).**
  - Set `GEMMA_PROVIDER=gemini` and `GEMINI_API_KEY=<key from Google AI Studio>`. The default model is `gemma-4-26b-a4b-it`.
  - Optionally keep `GEMMA_FALLBACK=ollama` and set `OLLAMA_HOST` to a machine running Ollama. That backup is used only on rate limits, timeouts or connection errors.
- **No key, fully local.**
  1. Install Ollama and pull a Gemma 4 E4B build. Check the tag with `ollama list`; prefer the QAT build on GPUs with 6 GB or less.
  2. Set `GEMMA_PROVIDER=ollama`, `GEMMA_FALLBACK=none`, `OLLAMA_HOST=http://localhost:11434`, `OLLAMA_MODEL=<tag>` and `OLLAMA_NUM_CTX=4096`.
  3. With `OLLAMA_NUM_CTX=2048`, only simpler questions fit. Larger prompts are refused with a clear message and are never silently cut.
- **Privacy.**
  - The cloud tier receives the schema, the question and at most 15 result rows.
  - Datasets whose profile sets `cloud_allowed: false` are sent only to the local tier.

### 4. Check and run
```powershell
.venv\Scripts\python -m pytest            # offline tests, no credentials needed
.venv\Scripts\python -m datamind.smoke    # Gemini per model ID, Ollama if set, Snowflake permissions
.venv\Scripts\python -m datamind ask "Which 5 nations had the highest revenue in 1995?"
```

### Troubleshooting
| Message | Fix |
|---|---|
| `Snowflake is not configured` | Set `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_PRIVATE_KEY_PATH` in `.env`. |
| `Snowflake login failed` | Register the public key for that user (`gen_keypair.py`), and check the account identifier. |
| `no Gemma tier is configured` | Set `GEMINI_API_KEY`, or `OLLAMA_HOST`. |
| `Gemini rate limit (429)` | You hit the free-tier quota. Set `OLLAMA_HOST` for the backup. Repeated questions reuse cached plans in `.cache/llm`. |
| `prompt needs about N tokens ... OLLAMA_NUM_CTX` | Raise `OLLAMA_NUM_CTX` to 4096, or use the Gemini tier. |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
# STATUS - minimal-diff

**Status: READY-FOR-RE-REVIEW - self-score 94/100, below the 95 bar** (the second independent
review scored the previous version 80). The model arm is built, tested with fakes and a
scripted local HTTP server, and queued (`scripts/run_models.sh`); the headline does not depend
on it.

## What changed after the second review (80/100)

| # | Defect | Fix | Test |
|---|---|---|---|
| 1 | Uncommitted `_child.py` returned `"ZeroDivisionError: division by zero"`, the test expected `"ZeroDivisionError"`: 1 of 127 failing | The message is right: `fix` now prints it when the unedited program errors, and no study verdict reads an assert's `detail` (only `status`; checked with `grep detail`). Test updated, plus a message-less exception keeps just the type | `test_outcomes_are_kept_distinct`, `test_error_detail_is_the_type_alone_when_the_exception_has_no_message` |
| 2 | `sys_path` had no caller | `fix` passes `[program's folder]`; the child adds it for one job and afterwards removes it and forgets every module imported from under it (the worker is reused) | `test_sys_path_makes_a_neighbouring_module_importable_for_that_job_only`, `test_fix_can_import_a_module_next_to_the_program` |
| 3 | `fix` misled on broken asserts | `--assert` must be exactly one `ast.Assert` (`f(1) == 2` is refused with "write it as 'assert f(1) == 2'"). The baseline now runs every assert; if every one errors with `NameError`/`ImportError`/`SyntaxError` it prints each error and exits 2 without searching. An error the bug causes (`IndexError`) is printed and then repaired, because a one-edit fix for it exists and refusing would be wrong. Exit status 1 when no patch passes | `test_fix_rejects_an_assert_argument_that_is_not_an_assert`, `test_fix_stops_with_the_error_when_the_asserts_cannot_run`, `test_fix_prints_an_error_the_bug_causes_and_still_repairs_it` |
| 4 | `fix` normalised the file with `ast.unparse` | `fix` uses splice mode (`operators._Source`) on the file's own bytes (line endings kept). The printed diff carries real line numbers and a `\ No newline at end of file` marker when needed; `--diff` prints only the patch | `test_fix_keeps_comments_and_its_diff_applies_to_the_original_file[git-apply]`, `[patch]`, `test_fix_diff_applies_to_a_file_without_a_final_newline_and_crlf` |
| 5 | Nested asserts silently dropped | Each is reported on stderr with its line; a file with only nested asserts is an error naming them | `test_fix_warns_about_asserts_it_does_not_run` |
| 6 | Token metric counted brackets `ast.unparse` adds | `tokens` ignores grouping brackets (a `(` that does not follow a name, string or closing bracket, and its `)`); `tokens_raw` keeps the old count and `tokens-raw` is a sixth reported metric. New `rescore` command re-measured the committed results without re-running a candidate. Both reported: 463 MBPP patches re-measured, 119 picks change, 73.60% → 73.85% exact, 13.25% → 13.16% overfit (paired +0.25 pp [−0.05, +0.55], −0.09 pp [−0.36, +0.20]); finding 2 shrinks from +0.48 to +0.30 pp. Sample 6 text corrected (both patches are 1 token, 3 counting brackets) | `test_grouping_brackets_are_not_counted_as_tokens`, `test_bracket_rule_only_ever_lowers_the_count_on_real_repair_edits`, `test_rescore_reproduces_the_row_repair_writes`, `test_rescore_refuses_rows_from_different_repair_operators` |
| 7 | Committed `p.out` profiler dump | Deleted; `*.out`, `*.prof` ignored | - |
| 8 | Stale test count | 149 in the badge, Quick start, Tests and here | - |
| 9 | "95% / 86%" from no results file | `patch_size_plausible_all` in the summary: 98.4% / 88.6% (95.0% / 86.4% with brackets counted, which is where the old numbers came from) | pipeline test checks the shares |
| 10 | "halves" for 13.2% → 8.5% | "cuts by about a third" (13.2% → 8.4% after the rescore) | - |
| 11 | Samples 4 and 5 edited without a marker | The whole Input/Output section is regenerated from `demo.py` output verbatim; the one cut (sample 4 repeating sample 3's program) is marked in square brackets. Trailing spaces removed from `show`'s output rather than from the quote | - |
| 12 | Model flags without help; `fix --timeout 0/-1`; `show` without `--metric` | Help on every model flag; `--show` must be ≥ 1; `--timeout` must be a finite positive number; `show --metric` | `test_every_flag_of_every_command_has_help` (walks the whole parser), `test_fix_timeout_must_be_positive`, `test_show_reports_the_metric_asked_for` |
| 13 | `uvx mypy src`: 15 errors | 17 found with mypy 2.3.1, all fixed without a new `type: ignore` (`warn_unused_ignores` on); `mypy==2.3.1` in the dev group, `[tool.mypy]` in `pyproject.toml`. There was no CI: added `.github/workflows/ci.yml` (ruff, ruff format, mypy, pytest on Linux and Windows) - **not yet run**, since nothing is pushed | `uv run mypy` |

Beyond the list:

- `demo.py` has a seventh sample: `fix` on a commented file, quoted in the README.
- The summary now has `tokens_vs_tokens_raw` per regime and `mean_tokens_raw` per verdict, so
  every number in the README's bracket paragraph is in `results/classical_repair.json`.
- `show`'s per-patch listing and `fix`'s listing no longer assume the patch keeps the line
  count (a splice of a multi-line node would have raised on `zip(strict=True)`).
- Sanity check on the rescore: the new `tokens-raw` column reproduces the old `tokens` numbers
  exactly (73.60% / 13.25% / +0.48 pp), and metrics that did not change (`ast`, `lines`)
  changed no pick.

## Headline

Unchanged in substance; numbers from the rescored results (MBPP, all three asserts shown):

- The size metric moves smallest-first from 66.7% [64.2, 68.9] exact (tokens+AST) to 73.9%
  [71.8, 75.8] (tokens).
- With random tie-breaks, size preference vs any plausible patch: +0.30 pp [0.20, 0.42]
  (tokens), −1.35 pp [−1.58, −1.13] (tokens+AST). Site-order tie-breaking gains 2.85 pp and
  loses 3.08 pp once negated conditions are excluded.
- More tests: HumanEval overfit 36.2% → 15.1% → 3.8% at 1 → 3 → 8.1 visible asserts.

## Self-score

| Points | Criterion | Score | Reason |
|---:|---|---:|---|
| 15 | Works from a clean clone | 15 | Fresh clone in the scratchpad: `uv sync --offline`, `uv run pytest -q` 149 pass with `MINIMAL_DIFF_DATA`, `MINIMAL_DIFF_RESULTS` and `HF_HOME` pointed at empty dirs (both stayed empty), `demo.py` output identical to the README's source, `minimal-diff report` reproduces `results/classical_repair.json` byte for byte. |
| 20 | Real data, real result | 19 | 6,731 tasks from MBPP and HumanEval, every one repaired, every number in `results/`. −1: bugs are mutations, not real-world bugs. |
| 15 | Finding quality | 13 | Six metrics × two tie-breaks, paired against a no-preference baseline, per bug kind, without the kind that drives it, on the ≥2-plausible subset, a dose-response over test count, a fault-location oracle, per-problem weighting, cluster bootstrap, and now a bracket robustness check. −2: a second metric artefact (brackets) was found by review, not by me, and it had inflated finding 2; the no-witness bucket is still wide (13.0% on MBPP). |
| 15 | Correctness | 14 | 149 tests; every defect above has one. mypy clean. −1: the AST-distance shortcut is validated empirically, not proved; CI is written but has never run. |
| 10 | Usability | 10 | `fix` edits the real file and its diff applies; refuses non-asserts and unrunnable asserts with the error; warns about skipped asserts; exit codes documented; help on every flag of every command (tested by walking the parser). |
| 10 | README | 9 | House format, 7 verbatim samples with the one cut marked, NOT-do, problems hit including both reviews'. −1: long. |
| 10 | Code quality | 10 | Zero runtime dependencies, ruff and ruff format clean, mypy clean with no new ignores, small modules. |
| 5 | Honesty | 4 | Every README number traced to `results/*.json` (including the bracket paragraph and the 95%/86% framing); the metric artefact and what it inflated are stated. −1: two metric artefacts shipped before review caught them. |
| **100** | | **94** | |

## Queued for the model run

`scripts/run_models.sh` (dry run: `scripts/run_models.sh --dry-run`). Checks free RAM ≥ 5 GB,
free VRAM ≥ 11 GB and that Ollama serves `qwen2.5-coder:14b`, then runs:

- **1,053 calls**: 200 MBPP + 151 HumanEval tasks (one per problem, bug kinds balanced,
  seed 0) × 3 prompts (`plain`, `minimal`, `diff`). Task list in
  `results/model_plan_qwen2.5-coder_14b.json` (unchanged by this round's dry run).
- Estimated 3.5 h at an assumed 12 s per call (not measured - the GPU was busy).
- Output: `results/model_qwen2.5-coder_14b.jsonl` (appended per reply),
  `results/model_arm_qwen2.5-coder_14b.json` (summary), `results/model_cache/` (every reply
  with its `done_reason`). Resumable. Model patches are measured with both token counts.

## Known weaknesses remaining

- Bugs are single-point mutations with the fix one edit away by construction; nothing here
  says anything about minimality where patch sizes vary.
- "No witness" is wide on negated conditions and comparisons; many are genuinely equivalent
  (`not a != b`), some are not, and the oracle cannot say which.
- Speed vetting of hidden inputs is measured at build time, so a rebuild on a differently
  loaded machine can keep or drop a borderline input. Re-running `repair` on the committed
  task files is unaffected.
- Site-order tie-breaking follows the AST walk, which puts an `if` before its condition; any
  site-order result is partly about that walk, which is why random ties are reported beside it.
- The bracket rule is lexical: a `(` after a keyword groups, after a name calls. A soft keyword
  used as a pattern (`case (1, 2):`) is counted as a call bracket; the one-edit search never
  edits one, and both sides of a diff are treated the same.
- `fix` only runs top-level asserts; pytest-style test functions are reported, not run.
- CI has never run (nothing is pushed).

## Reproduce every result

```bash
unset VIRTUAL_ENV
uv sync
uv run minimal-diff build-tasks        # data/problems_*.jsonl.gz, data/tasks_*.jsonl.gz, results/tasks_*.json  (~20 min)
uv run minimal-diff repair             # results/classical_{mbpp,humaneval}.jsonl.gz                          (~70 min, 8 workers)
uv run minimal-diff report             # results/classical_repair.json                                        (~2 min)
uv run minimal-diff check-isolation    # results/isolation_check.json
uv run minimal-diff check-oracle       # results/oracle_check.json
uv run minimal-diff model plan         # results/model_plan_qwen2.5-coder_14b.json (no model call)
uv run python demo.py                  # the README's Input/Output
uv run pytest -q && uv run ruff check src tests demo.py && uv run mypy
```

This round's result files were produced by `uv run minimal-diff rescore` (9 min) on the
committed rows, then `report`; a fresh `repair` writes the same sizes directly
(`test_rescore_reproduces_the_row_repair_writes`).

# STATUS - minimal-diff

**Status: READY-FOR-RE-REVIEW - self-score 92/100, below the 95 bar** (an independent review scored
the previous version 83). The model arm is built, tested with fakes and a scripted local HTTP
server, and queued (`scripts/run_models.sh`); the headline does not depend on it.

## What changed after review (83/100)

Every item raised, with the regression test that pins it:

| # | Issue | Fix | Test |
|---|---|---|---|
| 1 | Headline was an artefact of ranking by tokens then AST nodes (un-negation = 2 nodes loses to every 1-node swap) | Size metric is a parameter (`repair.METRICS`: tokens, ast, lines, tokens+ast, ast+tokens); every metric reported side by side with site-order and random tie-breaks, paired against any-plausible, overall, per bug kind, excluding `negate_if`, and on tasks with ≥2 plausible patches. Default is tokens. README findings rewritten around the sensitivity; "worse than chance" wording removed | `test_the_size_metric_decides_between_an_unnegation_and_an_operator_swap`, `test_selection_policies` |
| 2 | False `hang` witnesses on correct patches (vetting timed the reference call only) | Vet on the whole ref-vs-ref comparison (≤ 0.05 s); one per-problem budget `max(1 s, 20 × slowest reference assert)` for search, oracle and model arm; every timeout witness re-run at 10× before it is accepted; new `check-oracle` self-check: 0 false overfits on all 926 references | `test_vetting_times_the_whole_comparison_not_just_the_reference_call`, `test_a_slow_but_correct_patch_is_not_called_a_hang`, `test_oracle_self_check_finds_no_false_witness_on_a_clean_problem` |
| 3 | Crashed child labelled `hang` | `found_by` is `crash` or `hang` separately | `test_a_crash_is_a_crash_not_a_hang` |
| 4 | Model arm judged at 2.0 s vs search 1.0 s | Both use `ProblemRecord.item_budget` | `test_item_budget_scales_with_the_reference` |
| 5 | 7 MBPP problems counted nowhere | `dropped_all_survived` counter; counts now add up: 974 = 775 kept + 1 reference fails + 191 no mutants + 7 all survived | `test_problem_whose_every_mutant_survives_is_counted` |
| 6 | `show mbpp/3` listed `mbpp/1/...` | Prefix is `source/pid/` | `test_show_suggests_tasks_of_the_right_problem` |
| 7 | No way to repair your own code; flags without help; demo traceback without data | `minimal-diff fix FILE --tests FILE / --assert STMT / --metric / --top / --timeout`; help on every flag; demo prints the clean message | `test_fix_repairs_a_users_own_file`, `test_fix_inline_asserts_and_no_fix_found`, `test_demo_without_data_prints_a_message_not_a_traceback` |
| 8 | `num_predict` 1024 truncation, rows written only at the end, KeyError on a 200 without `message`, no retry | `num_predict` 4096 / `num_ctx` 8192; `done_reason` kept in cache and rows, `truncated` verdict; rows appended per reply, half-written line dropped on resume; 3 retries with backoff on connection drops, 5xx and message-less replies; 4xx fails fast | `test_client_retries_5xx_and_missing_message_then_keeps_done_reason`, `test_client_gives_up_after_retries_and_does_not_retry_4xx`, `test_truncated_reply_is_its_own_verdict_and_cached_as_such`, `test_rows_are_written_as_they_are_scored_and_a_rerun_resumes` |
| 9 | Program-defined classes compared by identity | Instances compared by class name + attributes (a shared class with its own `__eq__` still uses it). This was not only latent: every hidden input returning such an object had been dropped at vetting | `test_instances_of_the_programs_own_classes_compare_by_value`, `test_compare_arguments_may_name_what_the_program_or_setup_defines` |
| 10 | Framing | Thesis scoped to the one-edit space (95% of MBPP plausible patches are 1 token, 86% 1 token and 1 node; 44% of tasks have ≥2 plausible patches); literature paragraph (PraPR, Qi et al. Kali, Smith et al.); sample 4 heading fixed; sample 6 added showing the metric flip; `NOTICE` + full licence texts in `data/licenses/` | - |

Everything was rebuilt and re-run from the raw data after the fixes (build-tasks, repair,
report, check-isolation, check-oracle, model plan).

## Headline decision

The headline stands without the model arm: it is about ranking plausible patches by size, and
the search measures that on 6,731 tasks with controls and intervals. It changed after review:

- The size metric moves MBPP smallest-first from 67.1% [64.7, 69.3] exact (tokens+AST) to
  73.6% [71.5, 75.6] (tokens).
- With random tie-breaks, size preference vs any plausible patch: +0.48 pp [0.31, 0.67]
  (tokens), −1.00 pp [−1.25, −0.76] (tokens+AST). Site-order tie-breaking gains 2.6 pp overall
  and loses 2.6 pp once negated conditions are excluded.
- More tests: HumanEval overfit 35.8% → 15.3% → 3.9% at 1 → 3 → 8.1 visible asserts.

## Self-score

| Points | Criterion | Score | Reason |
|---:|---|---:|---|
| 15 | Works from a clean clone | 15 | Fresh clone: `uv sync --offline`, `uv run pytest -q` (115 pass), `uv run python demo.py` matches the README, `minimal-diff report` reproduces `results/classical_repair.json`. Tests point data and results at empty dirs. |
| 20 | Real data, real result | 19 | 6,731 tasks from MBPP and HumanEval (raw data and licences committed), every task repaired, every number in `results/`. −1: bugs are mutations, not real-world bugs. |
| 15 | Finding quality | 13 | Five metrics × two tie-breaks, paired against a no-preference baseline, per bug kind, with and without the kind that drives it, on the ≥2-plausible subset, a dose-response over test count, a fault-location oracle, per-problem weighting, cluster bootstrap. −2: the first version shipped a metric artefact as a finding, which says the analysis was not hostile enough the first time; the no-witness bucket is still wide (13.2% on MBPP). |
| 15 | Correctness | 14 | 115 tests including every review item. Two self-checks run on the real data: 0 false witnesses on 926 references, 797/797 pooled-vs-fresh agreement. −1: no type checker; the AST-distance shortcut is validated empirically, not proved. |
| 10 | Usability | 10 | `minimal-diff fix` for your own file and asserts; help on every flag; `show` names the right tasks; clean errors for missing data, bad ids, non-positive counts, unparsable input. |
| 10 | README | 9 | House format, 6 real Input/Output samples, NOT-do, problems hit (including the review's), literature, NOTICE. −1: long; the metric table and oracle section are dense. |
| 10 | Code quality | 9 | Zero runtime dependencies, ruff clean, small modules, type hints. −1: no mypy. |
| 5 | Honesty | 3 | Every README number traced to `results/*.json`; bounds stated; the earlier wrong framing is named in the README. −2: that framing was shipped at a self-score of 95. |

## Queued for the model run

`scripts/run_models.sh` (dry run: `scripts/run_models.sh --dry-run`). Checks free RAM ≥ 5 GB,
free VRAM ≥ 11 GB and that Ollama serves `qwen2.5-coder:14b`, then runs:

- **1,053 calls**: 200 MBPP + 151 HumanEval tasks (one per problem, bug kinds balanced,
  seed 0) × 3 prompts (`plain`, `minimal`, `diff`). Task list in
  `results/model_plan_qwen2.5-coder_14b.json`.
- Estimated 3.5 h at an assumed 12 s per call (not measured - the GPU was busy).
- Output: `results/model_qwen2.5-coder_14b.jsonl` (appended per reply),
  `results/model_arm_qwen2.5-coder_14b.json` (summary), `results/model_cache/` (every reply
  with its `done_reason`). Resumable.

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

## Reproduce every result

```bash
unset VIRTUAL_ENV
uv sync
uv run minimal-diff build-tasks        # data/problems_*.jsonl.gz, data/tasks_*.jsonl.gz, results/tasks_*.json  (~20 min)
uv run minimal-diff repair             # results/classical_{mbpp,humaneval}.jsonl.gz                          (~70 min, 8 workers)
uv run minimal-diff report             # results/classical_repair.json
uv run minimal-diff check-isolation    # results/isolation_check.json
uv run minimal-diff check-oracle       # results/oracle_check.json
uv run minimal-diff model plan         # results/model_plan_qwen2.5-coder_14b.json (no model call)
uv run python demo.py                  # the README's Input/Output
uv run pytest -q && uv run ruff check src tests demo.py
```

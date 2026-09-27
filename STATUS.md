# STATUS - minimal-diff

**Status: READY-FOR-REVIEW** (self-score 95/100). The model arm is built, tested with a fake
client and queued (`scripts/run_models.sh`); the headline does not depend on it.

## Headline decision

The brief asked whether the headline can stand without the model arm. It can. The finding is
about repair itself - whether the smallest plausible patch is the correct one - and the
classical search measures that on 6,715 real tasks with controls and intervals:

- MBPP (3 asserts): the smallest plausible patch is the known fix 67.1% [64.8, 69.3], provably
  wrong 17.3% [15.6, 19.1], unproven 15.6%.
- Preferring the smaller patch does not help: a random plausible patch overfits less
  (paired difference smallest − random +1.82 pp [1.02, 2.62] on MBPP; +2.27 pp [0.79, 3.78] on
  HumanEval), because wrong patches are as small as right ones.
- More visible tests are what help: HumanEval overfit 42.1% → 21.7% → 8.1% at 1 → 3 → 8.1
  asserts.

The model arm adds a second question (do LLM patches change more than needed, and does a
"laziest senior dev" prompt shrink them) rather than completing the first.

## Self-score

| Points | Criterion | Score | Reason |
|---:|---|---:|---|
| 15 | Works from a clean clone | 15 | Fresh `git clone` into a temp dir: `uv sync --offline`, `uv run pytest -q` (98 pass), `uv run python demo.py` (output identical to the committed run), `minimal-diff report` reproduces `results/classical_repair.json` byte for byte. Tests point `MINIMAL_DIFF_DATA`/`MINIMAL_DIFF_RESULTS` at empty dirs. |
| 20 | Real data, real result | 19 | 6,715 tasks from all 974 MBPP and 164 HumanEval problems (raw data committed), every task repaired, every number in `results/`. −1: the injected bugs are mutations, not real-world bugs. |
| 15 | Finding quality | 14 | Baselines (random plausible, random tie-break), an oracle ablation (fault location), a dose-response over visible-test count, paired differences, cluster bootstrap over problems, per-problem weighting, per bug kind. Surprising numbers chased: a 0.0% exact rate for negated conditions was a no-op edit in the repair space; 39 verdicts that flipped between runs were slow reference inputs timing out under load; four problems with no hidden inputs were an entry-point bug and an empty eval namespace (all fixed, full rerun each time). −1: the "no witness" bucket is wide (15.6% on MBPP), so overfitting is bracketed (17.3-32.9%) rather than pinned; fuzzing shows it still shrinks with more inputs. |
| 15 | Correctness | 14 | 98 tests on behaviour and failure modes: sandbox (hang, swallowed interrupt, C-level sleep, crash, 8 GB allocation, forged output, state leaking between candidates), injection/repair premise per operator, Zhang-Shasha against textbook cases and its shortcut against the exact computation, oracle buckets with witnesses, selection policies, CLI end to end, model arm end to end with a fake. Isolation check: 790/790 sampled candidates agree between pooled and fresh interpreters. −1: no type checker run; one-edit AST distance uses a shortcut validated empirically rather than proved. |
| 10 | Usability | 10 | `minimal-diff --help` and per-command help; `show <task> --regime` prints the bug, the tests, every plausible patch with its changed line and witness; missing data and unknown task ids give a message saying what to run or which ids exist; everything resumable. |
| 10 | README | 10 | House format (h1 with stack, thesis, nav, badges, through-line mermaid + blockquote, findings table, 5 real Input/Output samples from `demo.py`, quick start, layout, requirements, tests, NOT-do, problems hit, keywords, licence), British spelling, inspiration credited. |
| 10 | Code quality | 9 | Zero runtime dependencies, ruff clean (pinned 0.16.7), small modules, type hints throughout, no dead code found on a pass. −1: no mypy run. |
| 5 | Honesty | 4 | Every README number traced to `results/*.json` (classical_repair, tasks_*, isolation_check, model_plan); the lower-bound nature of "overfit" is stated with the upper bound; the hypothesis the repo was built around is reported as not holding. −1: the per-task row files are large and a reviewer must run `report` or read JSON to trace a number. |

## Done

- Task builder: every single-point mutant (operators from mbpp-false-accepts) of every
  reference that fails at least one visible assert; references normalised with
  `ast.unparse`; hidden inputs = EvalPlus (HumanEval) + perturbed assert arguments + 350
  type-preserving fuzz inputs, each vetted on the reference.
- Classical repair: all one-edit neighbours, run against visible asserts in a fixed order
  with early stop; plausible sets for regimes k1 / k3 / all; smallest-first, random,
  random-tie and at-fault policies.
- Oracle: exact / no_witness / overfit with the witness and which input set found it.
- Diff metrics: lines, tokens, Zhang-Shasha AST distance, unrelated lines, touches-fault.
- Sandbox: reusable worker processes, in-process watchdog timeouts, parent kill fallback,
  memory cap (Windows Job Object / RLIMIT_AS), deterministic hash seed.
- Model arm: three prompts, Ollama client, disk cache keyed by (model, system, prompt,
  options), code-block extraction and a tolerant unified-diff applier, scoring through the
  same oracle and metrics, paired comparison with the search on the same tasks, summary.

## Queued for the model run

`scripts/run_models.sh` (dry run: `scripts/run_models.sh --dry-run`). Checks free RAM ≥ 5 GB,
free VRAM ≥ 11 GB, and that Ollama serves `qwen2.5-coder:14b`, then runs:

- **1,053 calls**: 200 MBPP + 151 HumanEval tasks (one per problem, bug kinds balanced,
  seed 0) × 3 prompts (`plain`, `minimal`, `diff`). Task list in
  `results/model_plan_qwen2.5-coder_14b.json`.
- Estimated 3.5 h at an assumed 12 s per call (not measured - the GPU was busy).
- Output: `results/model_qwen2.5-coder_14b.jsonl` (one scored row per reply),
  `results/model_arm_qwen2.5-coder_14b.json` (summary), `results/model_cache/` (every reply).
  Resumable: re-running reuses cached replies.

## Known weaknesses remaining

- Bugs are single-point mutations with the fix one edit away by construction; multi-edit
  repair and real bugs are not measured.
- "No witness" is wide on negated conditions (49.3% of MBPP `negate_if` picks) - many are
  genuinely equivalent (`not a not in b`), some are not, and the oracle cannot say which.
- Hidden inputs are vetted on the reference's speed (≤ 0.05 s), measured at build time, so a
  rebuild on a differently loaded machine can keep or drop a borderline input. Re-running
  `repair` on the committed task files is unaffected; only `build-tasks` is.
- The results files are written with the tie-break by site order baked in; the random
  tie-break is an expectation computed from the same plausible set, not a separate run.

## Reproduce every result

```bash
unset VIRTUAL_ENV
uv sync
uv run minimal-diff build-tasks        # data/problems_*.jsonl.gz, data/tasks_*.jsonl.gz, results/tasks_*.json  (~20 min)
uv run minimal-diff repair             # results/classical_{mbpp,humaneval}.jsonl.gz                          (~60 min, 8 workers)
uv run minimal-diff report             # results/classical_repair.json
uv run minimal-diff check-isolation    # results/isolation_check.json                                          (~5 min)
uv run minimal-diff model plan         # results/model_plan_qwen2.5-coder_14b.json (no model call)
uv run python demo.py                  # the README's Input/Output
uv run pytest -q && uv run ruff check src tests demo.py
```

# Test durations: baseline (#375, #373 §1)

Measured on CI run [42438044246](https://github.com/eribertocz/orvex/actions/runs/42438044246) of PR #378, commit `87e52a1`. Each part ran on a GitHub-hosted `ubuntu-latest` runner, one pytest process per job, on Python 3.14 and 3.15, with 10 parts per Python. The parts were split round robin, because no record existed yet. The measured sections after **Part count and limits** come from `python -m tools.ci.durations report <job logs> --markdown <file> --record tools/ci/test-durations.json`; a new measurement replaces them, and the findings are rewritten from it.

## Findings

- **The suite takes 42 test-minutes per Python.** Of that, fixture setup is 7%; most of it is temporary folders and lakes.
- **The time is concentrated:**
  - server 36%, store 30% and conformance 25%;
  - the scheduler and unattended tests (`test_schedule.py`, `test_maintenance.py`, `test_unattended.py`) take 14.5 min across both Pythons;
  - single tests last up to 65 s.

  These are the first candidates for part 2 (#376): shared immutable fixtures and shorter waits. No test is removed or weakened.
- **CI wall time** for the `python` workflow was 9.0 min:
  - The 20 part jobs took 3.7–7.3 min each, including about 1.3 min of setup per job (runner, pip install, PostgreSQL and Azurite) and a median queue of 44 s (110 s at most).
  - The slowest part ran 1.9 times as long as the fastest, because the split was round robin.
- **Where an issue's 2–5 hours of verification went (before #375):**
  - the local `full` before every push: about 50 min on the 8-CPU sandbox, and much more on a 2-CPU one;
  - repeating it after each CI failure (#374 needed two rounds);
  - about 10 min of CI per push;
  - reruns of flaky timing tests: in this run, one 1-second wall-clock assertion in `test_tokens.py` failed on a loaded runner and needed a rerun.

  The new rule replaces the local `full` with `pr` (target ≤ 10 min). `full` stays mandatory in CI.

## Part count and limits (#373 §4)

- **10 parts per Python, 20 jobs in all.** This is the GitHub Free plan's limit of 20 concurrent jobs: more parts would queue and lengthen the run, and fewer would lengthen the slowest part.
- **Balance:** with the recorded durations, longest-first placement gives an expected slowest part of:

  | Parts | Slowest part (min) |
  |---|---|
  | 6 | 7.0 |
  | 8 | 5.25 |
  | 10 | 4.2 |
  | 12 | 3.5 |

  At 10 parts, the expected `python` wall time is about 6 min: 4.2 min of tests, 1.3 min of setup and the queue. The target is ≤ 20 min.
- **Locally,** `verify` runs `workers ÷ Pythons` parts per Python. DuckDB's per-test limits are unchanged. With a 2-CPU machine, `pr` runs the critical set and the affected tests in one part per Python.
- **Re-measurement:** the record is regenerated from a `main` run whenever the slowest part drifts more than 25% above the balanced estimate. Part 3 (#377) records the trend for every `main` build.


- **Tests measured:** 9,976 test runs, 5,032 distinct tests
- **Total test time:** 83.6 min, of which fixture setup 5.8 min (7%)

## Per Python
| Python | Minutes |
|---|---|
| 3.14 | 40.8 |
| 3.15 | 42.8 |

## Per module
| Module | Minutes | Share |
|---|---|---|
| server | 29.8 | 36% |
| store | 24.8 | 30% |
| tests/conformance | 21.1 | 25% |
| tests/tools | 2.1 | 2% |
| tests/properties | 2.0 | 2% |
| semantic | 1.9 | 2% |
| tests/workspace | 1.2 | 1% |
| transform | 0.6 | 1% |
| tests/contracts | 0.2 | 0% |
| access | 0.0 | 0% |
| contracts | 0.0 | 0% |
| dashboards | 0.0 | 0% |
| mcp | 0.0 | 0% |

## The 50 slowest files
| File | Minutes |
|---|---|
| `packages/server/tests/test_schedule.py` | 8.90 |
| `packages/store/tests/test_database_sources.py` | 4.54 |
| `packages/store/tests/test_admission_edges.py` | 4.45 |
| `packages/store/tests/test_lake.py` | 3.63 |
| `packages/server/tests/test_unattended.py` | 2.89 |
| `tests/conformance/test_semantic_corpus.py` | 2.82 |
| `packages/server/tests/test_maintenance.py` | 2.71 |
| `tests/conformance/test_materialization.py` | 2.63 |
| `packages/store/tests/test_tokens.py` | 2.37 |
| `tests/conformance/test_totals.py` | 2.26 |
| `tests/conformance/test_detail_rows.py` | 2.25 |
| `packages/server/tests/test_incremental.py` | 2.23 |
| `packages/server/tests/test_lake_catalog.py` | 1.74 |
| `packages/store/tests/test_source_isolation.py` | 1.70 |
| `packages/store/tests/test_file_source_exactness.py` | 1.70 |
| `packages/server/tests/test_partition_cut.py` | 1.38 |
| `packages/store/tests/test_sources.py` | 1.32 |
| `tests/conformance/test_row_policies.py` | 1.22 |
| `tests/workspace/test_packages.py` | 1.21 |
| `packages/server/tests/test_since.py` | 1.20 |
| `packages/server/tests/test_semantic_model.py` | 1.08 |
| `tests/conformance/test_direct_query.py` | 1.03 |
| `tests/conformance/test_estimates.py` | 0.97 |
| `tests/conformance/test_ratios.py` | 0.93 |
| `packages/store/tests/test_catalog_limits.py` | 0.92 |
| `packages/server/tests/test_commands.py` | 0.91 |
| `tests/conformance/test_asof_windows.py` | 0.90 |
| `packages/server/tests/test_lake_data.py` | 0.88 |
| `packages/server/tests/test_source_models.py` | 0.86 |
| `packages/server/tests/test_lineage.py` | 0.85 |
| `packages/server/tests/test_evidence_runs.py` | 0.84 |
| `tests/conformance/test_zones.py` | 0.82 |
| `packages/store/tests/test_changes.py` | 0.79 |
| `tests/properties/test_engine_oracle.py` | 0.77 |
| `tests/conformance/test_period_end.py` | 0.76 |
| `packages/server/tests/test_semantic_http.py` | 0.74 |
| `tests/conformance/test_bridges.py` | 0.72 |
| `tests/properties/test_policy_properties.py` | 0.66 |
| `packages/semantic/tests/test_query_binding.py` | 0.62 |
| `tests/conformance/test_conformed.py` | 0.60 |
| `packages/store/tests/test_budgets.py` | 0.59 |
| `tests/tools/test_load_baseline.py` | 0.59 |
| `tests/conformance/test_evidence.py` | 0.57 |
| `tests/conformance/test_schema_sources.py` | 0.52 |
| `packages/server/tests/test_model_tests.py` | 0.50 |
| `packages/store/tests/test_maintenance.py` | 0.50 |
| `packages/server/tests/test_semantic_explain.py` | 0.50 |
| `tests/conformance/test_fanout_isolation.py` | 0.49 |
| `tests/conformance/test_execution.py` | 0.47 |
| `tests/properties/test_multifact_materialization.py` | 0.46 |

## The 50 slowest tests (one run each)
| Test | Python | Seconds | Setup s |
|---|---|---|---|
| `packages/server/tests/test_maintenance.py::test_the_scheduler_maintains_when_nothing_is_due` | 3.14 | 65.6 | 0.0 |
| `packages/server/tests/test_schedule.py::test_idle_scheduling_does_not_touch_the_sources` | 3.14 | 65.0 | 0.0 |
| `packages/server/tests/test_schedule.py::test_idle_scheduling_does_not_touch_the_sources` | 3.15 | 63.1 | 0.0 |
| `packages/server/tests/test_unattended.py::test_incremental_loads_from_postgres_every_five_minutes_without_duplicates_or_gaps` | 3.14 | 60.4 | 0.0 |
| `packages/server/tests/test_unattended.py::test_incremental_loads_from_postgres_every_five_minutes_without_duplicates_or_gaps` | 3.15 | 60.4 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_failing_model_reading_a_busy_model_is_still_retried` | 3.15 | 59.0 | 0.0 |
| `packages/store/tests/test_tokens.py::test_parser_spaces_are_every_character_duckdb_reads_as_whitespace` | 3.15 | 53.9 | 0.0 |
| `packages/store/tests/test_tokens.py::test_parser_spaces_are_every_character_duckdb_reads_as_whitespace` | 3.14 | 53.8 | 0.0 |
| `packages/server/tests/test_maintenance.py::test_the_scheduler_maintains_when_nothing_is_due` | 3.15 | 53.8 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_failing_model_reading_a_busy_model_is_still_retried` | 3.14 | 47.1 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_failing_model_does_not_stall_the_others` | 3.14 | 39.3 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_run_failing_four_times_is_given_up` | 3.15 | 37.1 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_run_failing_four_times_is_given_up` | 3.14 | 36.1 | 0.0 |
| `packages/store/tests/test_file_source_exactness.py::test_every_csv_text_alone_and_in_pairs_comes_back_unchanged` | 3.15 | 34.5 | 0.0 |
| `packages/server/tests/test_schedule.py::test_a_failing_model_does_not_stall_the_others` | 3.15 | 34.5 | 0.0 |
| `packages/server/tests/test_schedule.py::test_failed_runs_are_retried_with_backoff` | 3.15 | 32.6 | 0.0 |
| `tests/conformance/test_materialization.py::test_the_table_s_answer_is_always_the_base_s` | 3.15 | 30.8 | 0.0 |
| `tests/conformance/test_materialization.py::test_the_table_s_answer_is_always_the_base_s` | 3.14 | 30.0 | 0.0 |
| `packages/server/tests/test_schedule.py::test_models_are_kept_fresh_without_overlap` | 3.14 | 26.5 | 0.0 |
| `tests/conformance/test_materialization.py::test_a_grain_policy_keeps_the_table_and_never_widens_the_answer` | 3.15 | 26.3 | 0.0 |
| `packages/store/tests/test_file_source_exactness.py::test_every_csv_text_alone_and_in_pairs_comes_back_unchanged` | 3.14 | 26.2 | 0.0 |
| `packages/server/tests/test_schedule.py::test_models_are_kept_fresh_without_overlap` | 3.15 | 25.6 | 0.0 |
| `tests/conformance/test_materialization.py::test_a_grain_policy_keeps_the_table_and_never_widens_the_answer` | 3.14 | 25.3 | 0.0 |
| `tests/conformance/test_estimates.py::test_the_estimate_counts_exactly_the_files_a_filter_can_need` | 3.14 | 25.3 | 0.0 |
| `packages/server/tests/test_schedule.py::test_failed_runs_are_retried_with_backoff` | 3.14 | 23.5 | 0.0 |
| `tests/conformance/test_detail_rows.py::test_pages_concatenate_to_every_authorized_row_exactly_once` | 3.15 | 21.6 | 0.0 |
| `packages/store/tests/test_database_sources.py::test_unreachable_host_fails_within_the_connect_timeout[d5157b4a9eb8]` | 3.14 | 21.1 | 0.0 |
| `packages/store/tests/test_database_sources.py::test_unreachable_host_fails_within_the_connect_timeout[d5157b4a9eb8]` | 3.15 | 21.1 | 0.0 |
| `packages/store/tests/test_database_sources.py::test_unreachable_host_fails_within_the_connect_timeout[03568bd3e289]` | 3.14 | 20.9 | 0.0 |
| `packages/store/tests/test_database_sources.py::test_unreachable_host_fails_within_the_connect_timeout[03568bd3e289]` | 3.15 | 20.8 | 0.0 |
| `tests/conformance/test_estimates.py::test_the_estimate_counts_exactly_the_files_a_filter_can_need` | 3.15 | 20.5 | 0.0 |
| `tests/conformance/test_detail_rows.py::test_pages_sorted_by_a_float_continue_exactly` | 3.14 | 18.9 | 0.0 |
| `tests/conformance/test_detail_rows.py::test_pages_concatenate_to_every_authorized_row_exactly_once` | 3.14 | 18.8 | 0.0 |
| `tests/conformance/test_semantic_corpus.py::test_the_http_api_gives_every_case_the_library_s_outcome` | 3.14 | 18.5 | 0.0 |
| `tests/conformance/test_semantic_corpus.py::test_report_is_deterministic` | 3.15 | 17.8 | 0.0 |
| `tests/conformance/test_semantic_corpus.py::test_report_is_deterministic` | 3.14 | 17.7 | 0.0 |
| `tests/conformance/test_semantic_corpus.py::test_the_http_api_gives_every_case_the_library_s_outcome` | 3.15 | 17.7 | 0.0 |
| `tests/conformance/test_totals.py::test_every_level_equals_its_reference` | 3.14 | 16.8 | 0.0 |
| `packages/store/tests/test_file_source_exactness.py::test_every_pair_of_parquet_types_in_two_files_is_exact_or_refused` | 3.14 | 16.8 | 0.2 |
| `packages/store/tests/test_file_source_exactness.py::test_every_pair_of_parquet_types_in_two_files_is_exact_or_refused` | 3.15 | 16.5 | 0.2 |
| `tests/conformance/test_detail_rows.py::test_no_page_ever_shows_a_row_a_joined_policy_removes` | 3.14 | 16.4 | 0.0 |
| `tests/conformance/test_detail_rows.py::test_pages_sorted_by_a_float_continue_exactly` | 3.15 | 16.3 | 0.0 |
| `tests/conformance/test_totals.py::test_every_level_equals_its_reference` | 3.15 | 16.3 | 0.0 |
| `tests/conformance/test_detail_rows.py::test_no_page_ever_shows_a_row_a_joined_policy_removes` | 3.15 | 16.0 | 0.0 |
| `packages/store/tests/test_admission_edges.py::test_a_read_queries_the_lake_only_once_admitted[d5157b4a9eb8]` | 3.14 | 14.8 | 0.0 |
| `packages/store/tests/test_admission_edges.py::test_a_read_queries_the_lake_only_once_admitted[d5157b4a9eb8]` | 3.15 | 14.3 | 0.0 |
| `packages/server/tests/test_source_models.py::test_an_incremental_source_model_loads_only_new_rows` | 3.14 | 14.1 | 0.0 |
| `tests/tools/test_load_baseline.py::test_the_baseline_runs_end_to_end_on_the_small_dataset` | 3.14 | 13.3 | 0.7 |
| `packages/server/tests/test_source_models.py::test_an_incremental_source_model_loads_only_new_rows` | 3.15 | 13.3 | 0.0 |
| `tests/conformance/test_totals.py::test_period_end_at_every_level_equals_its_reference` | 3.15 | 13.1 | 0.0 |

## Setup time by fixture (each test's whole setup, counted for every fixture it uses)
| Fixture | Tests | Setup minutes |
|---|---|---|
| `request` | 4,030 | 5.8 |
| `tmp_path_factory` | 3,892 | 5.8 |
| `tmp_path` | 3,842 | 5.6 |
| `location` | 1,204 | 5.0 |
| `lake` | 830 | 3.9 |
| `sql` | 960 | 1.5 |
| `expected` | 434 | 0.6 |
| `monkeypatch` | 640 | 0.5 |
| `base` | 88 | 0.5 |
| `kind` | 288 | 0.5 |
| `value` | 226 | 0.5 |
| `served` | 30 | 0.4 |
| `statements` | 68 | 0.4 |
| `name` | 142 | 0.4 |
| `files` | 128 | 0.4 |
| `two_files` | 48 | 0.3 |
| `sourced` | 44 | 0.3 |
| `dataset` | 70 | 0.3 |
| `single` | 44 | 0.3 |
| `sqlite_file` | 70 | 0.2 |
| `sqlite_lake` | 52 | 0.2 |
| `token_check` | 28 | 0.2 |
| `stop` | 60 | 0.2 |
| `parameters` | 42 | 0.2 |
| `admin` | 80 | 0.2 |

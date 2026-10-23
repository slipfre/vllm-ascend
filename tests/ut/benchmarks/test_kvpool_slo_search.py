"""Standard-library tests; no AISBench installation or model service required."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.kvpool import slo_search


def log(ttft=9000, tpot=19, unit="ms"):
    return f"""
| Metric | Average | Median | Std | Min | Max | P90 | P99 |
| TTFT ({unit}) | 1 | 2 | 3 | 4 | 5 | {ttft} | 11000 |
| TPOT ({unit}) | 1 | 2 | 3 | 4 | 5 | {tpot} | 25 |
| Common Metric | Value |
| Total Requests | 128 |
| Failed Requests | 0 |
| Output Token Throughput (token/s) | 1200 |
| Total Token Throughput (token/s) | 2400 |
| Prefill Token Throughput (token/s) | 3000 |
| Request Throughput (req/s) | 12 |
"""


class ParserTests(unittest.TestCase):
    def test_header_selects_percentile_and_converts_seconds(self):
        metrics = slo_search.parse_metrics(log(9, 0.019, "s"), "P90", None)
        self.assertEqual(metrics["ttft_ms"], 9000)
        self.assertEqual(metrics["tpot_ms"], 19)
        self.assertEqual(metrics["ttft_mean_ms"], 1000)
        self.assertEqual(metrics["ttft_p90_ms"], 9000)
        self.assertEqual(metrics["tpot_mean_ms"], 1000)
        self.assertEqual(metrics["tpot_p90_ms"], 19)
        self.assertEqual(metrics["success_rate"], 1)
        self.assertEqual(slo_search.parse_metrics(log(), "P99", None)["tpot_ms"], 25)

    def test_reordered_columns(self):
        text = log().replace(
            "Average | Median | Std | Min | Max | P90 | P99", "P90 | Median | Std | Min | Max | Average | P99"
        )
        self.assertEqual(slo_search.parse_metrics(text, "P90", None)["ttft_ms"], 1)
        self.assertEqual(slo_search.parse_metrics(text, "P90", None)["ttft_mean_ms"], 9000)

    def test_missing_percentile_is_error(self):
        with self.assertRaisesRegex(slo_search.TrialError, "no P95"):
            slo_search.parse_metrics(log(), "P95", None)

    def test_mean_selection_still_requires_and_records_p90(self):
        metrics = slo_search.parse_metrics(log(), "mean", None)
        self.assertEqual(metrics["ttft_ms"], 1)
        self.assertEqual(metrics["ttft_p90_ms"], 9000)
        self.assertEqual(metrics["tpot_ms"], 1)
        self.assertEqual(metrics["tpot_p90_ms"], 19)
        with self.assertRaisesRegex(slo_search.TrialError, "no P90"):
            slo_search.parse_metrics(log().replace("P90", "P95"), "mean", None)

    def test_single_output_token_marks_tpot_not_applicable(self):
        text = "\n".join(line for line in log().splitlines() if "TPOT" not in line)
        metrics = slo_search.parse_metrics(text, "mean", None, tpot_applicable=False)
        self.assertEqual(metrics["ttft_mean_ms"], 1)
        self.assertIsNone(metrics["tpot_ms"])
        self.assertIsNone(metrics["tpot_p90_ms"])
        self.assertEqual(metrics["tpot_status"], "not_applicable_single_token")
        with self.assertRaisesRegex(slo_search.TrialError, "missing summary metrics"):
            slo_search.parse_metrics(text, "mean", None)

    def test_missing_or_inconsistent_summary_is_error(self):
        for text in (
            log().replace("| Failed Requests | 0 |", ""),
            log().replace("| TPOT (ms)", "| Unknown (ms)"),
            log() + "\n| Successful Requests | 12 |",
            log() + "\nERROR: benchmark failed",
        ):
            with self.subTest(text=text), self.assertRaises(slo_search.TrialError):
                slo_search.parse_metrics(text, "P90", None)

    def test_does_not_reuse_warmup_metrics(self):
        with self.assertRaises(slo_search.TrialError):
            slo_search.parse_metrics(log() + "\n[开始] 全量数据集测试\nTotal Requests: 128\n", "P90", None)

    def test_unitless_requires_configuration(self):
        text = log().replace(" (ms)", "")
        with self.assertRaisesRegex(slo_search.TrialError, "no unit"):
            slo_search.parse_metrics(text, "P90", None)
        self.assertEqual(slo_search.parse_metrics(text, "P90", "ms")["ttft_ms"], 9000)

    def test_remote_aisbench_format_cell_units_stage_and_sample_count(self):
        text = """
│ Performance Parameters │ Stage │ Average │ Min │ Max │ Median │ P75 │ P90 │ P99 │ N │
│ TTFT │ total │ 441.0 ms │ 430.6 ms │ 446.7 ms │ 443.3 ms │ 446.1 ms │ 446.4 ms │ 446.6 ms │ 4 │
│ TPOT │ total │ 6.5 ms │ 6.1 ms │ 6.9 ms │ 6.5 ms │ 6.9 ms │ 6.9 ms │ 6.9 ms │ 4 │
│ TTFT │ stable │ 0.1 s │ 0.1 s │ 0.2 s │ 0.1 s │ 0.1 s │ 0.15 s │ 0.2 s │ 3 │
│ TPOT │ stable │ 5 ms │ 5 ms │ 6 ms │ 5 ms │ 5 ms │ 5.9 ms │ 6 ms │ 3 │
│ Common Metric │ Stage │ Value │
│ Total Requests │ total │ 4 │
│ Failed Requests │ total │ 0 │
│ Success Requests │ total │ 4 │
│ Request Throughput │ total │ 1.5551 req/s │
│ Output Token Throughput │ total │ 49.7617 token/s │
│ Prefill Token Throughput │ total │ 3000 token/s │
│ Total Requests │ stable │ 3 │
│ Failed Requests │ stable │ 0 │
│ Request Throughput │ stable │ 2 req/s │
│ Output Token Throughput │ stable │ 60 token/s │
│ Prefill Token Throughput │ stable │ 4000 token/s │
"""
        total = slo_search.parse_metrics(text, "P90", None)
        self.assertEqual(total["ttft_ms"], 446.4)
        self.assertEqual(total["tpot_ms"], 6.9)
        self.assertEqual(total["total_requests"], 4)
        self.assertEqual(total["output_tokens_per_s"], 49.7617)
        self.assertEqual(total["prefill_tokens_per_s"], 3000)
        stable = slo_search.parse_metrics(text, "P90", None, stage="stable")
        self.assertEqual(stable["ttft_ms"], 150)
        self.assertEqual(stable["total_requests"], 3)
        self.assertEqual(stable["prefill_tokens_per_s"], 4000)
        with self.assertRaisesRegex(slo_search.TrialError, "missing summary metrics"):
            slo_search.parse_metrics(text, "P90", None, stage="missing")

    def test_malformed_quantity_is_not_silently_accepted(self):
        for value in ("nan", "inf", "-1 ms", "10 seconds", "10 ms 4"):
            text = log().replace(
                "| TPOT (ms) | 1 | 2 | 3 | 4 | 5 | 19 | 25 |", f"| TPOT (ms) | 1 | 2 | 3 | 4 | 5 | {value} | 25 |"
            )
            with self.subTest(value=value), self.assertRaises(slo_search.TrialError):
                slo_search.parse_metrics(text, "P90", None)

    def test_final_buffered_cache_summary_and_unrounded_rate(self):
        warmup = """
Prefix cache hit summary
scope hbm_hit_rate hbm(hit/query) external_hit_rate external(hit/query)
ALL_PODS 0.00% 0/117984 0.00% 0/0
"""
        full = """
Prefix cache hit summary
scope hbm_hit_rate hbm(hit/query) external_hit_rate external(hit/query)
ALL_PODS 89.63% 470016/524368 0.00% 0/0
"""
        text = log() + "\n[开始] 全量数据集测试\n" + log() + warmup + full
        metrics = slo_search.parse_metrics(text, "P90", None, require_cache=True)
        self.assertEqual(metrics["hbm_hits"], 470016)
        self.assertEqual(metrics["hbm_queries"], 524368)
        self.assertAlmostEqual(metrics["hbm_hit_rate_pct"], 470016 / 524368 * 100)
        self.assertIsNone(metrics["external_hit_rate_pct"])
        self.assertEqual(metrics["external_cache_status"], "no_queries")

    def test_missing_cache_is_explicit_and_required_for_prefix_prewarm(self):
        metrics = slo_search.parse_metrics(log(), "P90", None)
        self.assertIsNone(metrics["hbm_hit_rate_pct"])
        self.assertEqual(metrics["hbm_cache_status"], "not_reported")
        with self.assertRaisesRegex(slo_search.TrialError, "missing full-workload"):
            slo_search.parse_metrics(log(), "P90", None, require_cache=True)

    def test_cache_failures_do_not_reuse_old_summary(self):
        valid = """
Prefix cache hit summary
scope hbm_hit_rate hbm(hit/query) external_hit_rate external(hit/query)
ALL_PODS 0.00% 0/20 50.00% 5/10
"""
        self.assertEqual(slo_search.parse_cache_summary(valid)["hbm_hit_rate_pct"], 0)
        for broken in (
            valid + "\nPrefix cache hit summary\n",
            valid.replace("0/20", "21/20"),
            valid.replace("50.00%", "51.00%"),
            valid + "\nSkipped PODs: missing:8000",
            valid.replace("0.00% 0/20", "0.00% 0/0").replace("5/10", "5/0"),
        ):
            with self.subTest(broken=broken), self.assertRaises(slo_search.TrialError):
                slo_search.parse_cache_summary(broken)


FAKE_CLI = """import argparse
p = argparse.ArgumentParser()
p.add_argument('--concurrency', type=int)
p.add_argument('--data_num')
p.add_argument('--request_rate')
p.add_argument('--repeat')
p.add_argument('--dataset_type')
p.add_argument('--prefix_num', type=int)
p.add_argument('--output_len', type=int, default=1500)
a = p.parse_args()
if a.dataset_type == 'prefix_cache':
    assert a.prefix_num == a.concurrency
print('| Metric | Average | Median | Std | Min | Max | P90 | P99 |')
print('| TTFT (ms) | 1 | 2 | 3 | 4 | 5 |', 10000 if a.concurrency >= 7 else 9000, '| 11000 |')
if a.output_len != 1:
    print('| TPOT (ms) | 1 | 2 | 3 | 4 | 5 | 19 | 25 |')
print('| Common Metric | Value |')
print('| Total Requests |', a.data_num, '|')
print('| Failed Requests | 0 |')
print('| Output Token Throughput (token/s) |', a.concurrency * 100, '|')
print('| Prefill Token Throughput (token/s) |', a.concurrency * 200, '|')
print('| Request Throughput (req/s) |', a.concurrency * 2, '|')
"""


class IntegrationTests(unittest.TestCase):
    def test_summary_always_shows_mean_and_p90(self):
        for statistic in ("mean", "P90", "P99"):
            with self.subTest(statistic=statistic), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "aisbench_test.py").write_text(FAKE_CLI, encoding="utf-8")
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    slo_search.main(
                        [
                            "--project-dir",
                            str(root),
                            "--output-dir",
                            str(root / "results"),
                            "--ttft-limit-ms",
                            "10000",
                            "--percentile",
                            statistic,
                            "--start-concurrency",
                            "1",
                            "--max-concurrency",
                            "1",
                        ]
                    )
                line = next(line for line in captured.getvalue().splitlines() if "(SLO:" in line)
                self.assertIn(f"(SLO: {statistic})", line)
                for value in ("TTFT(mean)=1 ms", "TTFT(P90)=9000 ms", "TPOT(mean)=1 ms", "TPOT(P90)=19 ms"):
                    self.assertIn(value, line)
                if statistic == "P99":
                    self.assertIn("TTFT(P99)=11000 ms", line)
                    self.assertIn("TPOT(P99)=25 ms", line)

    def invoke(self, project, output, extra=(), confirm_runs=2):
        with contextlib.redirect_stdout(io.StringIO()):
            args = [
                "--project-dir",
                str(project),
                "--output-dir",
                str(output),
                "--ttft-limit-ms",
                "10000",
                "--tpot-limit-ms",
                "20",
                "--start-concurrency",
                "2",
                "--max-concurrency",
                "16",
            ]
            if confirm_runs is not None:
                args += ["--confirm-runs", str(confirm_runs)]
            return slo_search.main(args + list(extra))

    def test_full_cli_search_and_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text(FAKE_CLI, encoding="utf-8")
            output = root / "results"
            self.assertEqual(self.invoke(root, output), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["highest_passing_concurrency"], 6)
            self.assertTrue(summary["confirmation_performed"])
            self.assertEqual(summary["result_source"], "confirm")
            self.assertEqual(summary["throughput"]["output_tokens_per_s_mean"], 600)
            self.assertEqual(summary["throughput"]["prefill_tokens_per_s_mean"], 1200)
            rows = [json.loads(line) for line in (output / "history.jsonl").read_text().splitlines()]
            self.assertTrue(all(r["data_num"] == r["concurrency"] * 4 for r in rows))
            self.assertEqual({r["phase"] for r in rows}, {"expand", "bisect", "confirm"})
            self.assertTrue(all(r["concurrency"] < 7 for r in rows if r["passed"]))
            self.assertTrue(all(Path(r["log"]).is_file() for r in rows))
            self.assertIn("Prefill (token/s)", (output / "trial_0001" / "metrics.txt").read_text())
            table = (output / "trial_0001" / "metrics.txt").read_text()
            for column in ("TTFT mean (ms)", "TTFT P90 (ms)", "TPOT mean (ms)", "TPOT P90 (ms)"):
                self.assertIn(column, table)
            self.assertIn("external_hit_rate", (output / "trial_0001" / "console.log").read_text())
            self.assertFalse((root / ".slo_search.lock").exists())

    def test_default_skips_confirm_for_boundary_no_pass_and_limit(self):
        for threshold, winner, code in ((7, 6, 0), (1, None, 1), (1000, 16, 0)):
            with self.subTest(threshold=threshold), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "aisbench_test.py").write_text(FAKE_CLI.replace(">= 7", f">= {threshold}"), encoding="utf-8")
                output = root / "results"
                self.assertEqual(self.invoke(root, output, confirm_runs=None), code)
                summary = json.loads((output / "summary.json").read_text())
                rows = [json.loads(line) for line in (output / "history.jsonl").read_text().splitlines()]
                self.assertEqual(summary["highest_passing_concurrency"], winner)
                self.assertEqual(summary["confirmation_runs"], 0)
                self.assertFalse(summary["confirmation_performed"])
                self.assertEqual(summary["result_source"], "search")
                self.assertEqual(summary["confirmation_trial_ids"], [])
                self.assertNotIn("confirm", {r["phase"] for r in rows})
                if winner is not None:
                    self.assertEqual(summary["throughput"]["output_tokens_per_s_mean"], winner * 100)
                    self.assertEqual(len(summary["result_trial_ids"]), 1)

    def test_mean_slo_changes_search_without_losing_p90(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text(FAKE_CLI, encoding="utf-8")
            output = root / "results"
            self.assertEqual(self.invoke(root, output, ["--percentile", "mean"], confirm_runs=0), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["highest_passing_concurrency"], 16)
            rows = [json.loads(line) for line in (output / "history.jsonl").read_text().splitlines()]
            self.assertTrue(all(r["ttft_ms"] == r["ttft_mean_ms"] == 1 for r in rows))
            self.assertEqual(rows[-1]["ttft_p90_ms"], 10000)
            self.assertEqual(rows[-1]["tpot_p90_ms"], 19)

    def test_ttft_only_single_token_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text(FAKE_CLI, encoding="utf-8")
            output = root / "results"
            with contextlib.redirect_stdout(io.StringIO()):
                code = slo_search.main(
                    [
                        "--project-dir",
                        str(root),
                        "--output-dir",
                        str(output),
                        "--ttft-limit-ms",
                        "10000",
                        "--percentile",
                        "mean",
                        "--start-concurrency",
                        "2",
                        "--max-concurrency",
                        "4",
                        "--",
                        "--output_len",
                        "1",
                    ]
                )
            self.assertEqual(code, 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["active_slo_metrics"], ["ttft"])
            self.assertIsNone(summary["tpot_limit_ms"])
            self.assertIsNone(summary["worst_tpot_ms"])
            self.assertEqual(summary["highest_passing_concurrency"], 4)
            self.assertFalse(summary["confirmation_performed"])
            self.assertIn("N/A", (output / "trial_0001" / "metrics.txt").read_text())

    def test_unbounded_tpot_does_not_affect_ttft_slo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text(
                FAKE_CLI.replace("| 19 | 25 |", "| 9999 | 10000 |"), encoding="utf-8"
            )
            output = root / "results"
            with contextlib.redirect_stdout(io.StringIO()):
                code = slo_search.main(
                    [
                        "--project-dir",
                        str(root),
                        "--output-dir",
                        str(output),
                        "--ttft-limit-ms",
                        "10000",
                        "--start-concurrency",
                        "2",
                        "--max-concurrency",
                        "8",
                    ]
                )
            self.assertEqual(code, 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["highest_passing_concurrency"], 6)
            self.assertEqual(summary["worst_tpot_ms"], 9999)

    def test_slo_bounds_required_and_tpot_rejected_for_one_token(self):
        for args in ([], ["--ttft-limit-ms", "10000", "--tpot-limit-ms", "20", "--", "--output_len=1"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                slo_search.arguments(args)

    def test_no_passing_point_and_capped_search(self):
        for threshold, winner, code in ((1, None, 1), (1000, 16, 0)):
            with self.subTest(threshold=threshold), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "aisbench_test.py").write_text(FAKE_CLI.replace(">= 7", f">= {threshold}"), encoding="utf-8")
                output = root / "results"
                self.assertEqual(self.invoke(root, output), code)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["highest_passing_concurrency"], winner)
                self.assertEqual(summary["at_search_limit"], winner == 16)

    def test_prefix_count_tracks_every_search_and_confirmation_trial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text(FAKE_CLI, encoding="utf-8")
            output = root / "results"
            self.assertEqual(
                self.invoke(root, output, ["--prefix-num-equals-concurrency", "--", "--dataset_type", "prefix_cache"]),
                0,
            )
            rows = [json.loads(line) for line in (output / "history.jsonl").read_text().splitlines()]
            self.assertEqual({r["phase"] for r in rows}, {"expand", "bisect", "confirm"})
            self.assertTrue(all(r["prefix_num"] == r["concurrency"] for r in rows))
            self.assertTrue(all(r["data_num"] == 4 * r["prefix_num"] for r in rows))

    def test_invalid_trial_aborts_instead_of_lowering_concurrency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "aisbench_test.py").write_text("print('incomplete output')", encoding="utf-8")
            output = root / "results"
            self.assertEqual(self.invoke(root, output), 2)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["status"], "aborted")
            self.assertEqual(summary["trials"], 1)

    def test_boundary_instability_moves_down(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "aisbench_test.py").touch()
            args, forwarded = slo_search.arguments(
                [
                    "--project-dir",
                    temporary,
                    "--ttft-limit-ms",
                    "10000",
                    "--tpot-limit-ms",
                    "20",
                    "--start-concurrency",
                    "2",
                    "--max-concurrency",
                    "16",
                    "--confirm-runs",
                    "2",
                ]
            )
        search = slo_search.Search(args, forwarded, Path("."))

        def trial(concurrency, phase):
            passed = concurrency <= (3 if phase == "confirm" else 6)
            row = dict(
                trial=len(search.history) + 1,
                passed=passed,
                output_tokens_per_s=concurrency * 100,
                requests_per_s=1,
                prefill_tokens_per_s=2,
                ttft_ms=1,
                tpot_ms=1,
            )
            search.history.append(row)
            return row

        search.trial = trial
        self.assertEqual(search.run()["highest_passing_concurrency"], 3)


if __name__ == "__main__":
    unittest.main()

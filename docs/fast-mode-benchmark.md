# Fast mode benchmark — 2026-09-27 (legacy report)

Fast is now named **System One**. This historical report is retained for
compatibility with earlier raw files and documents the old end-to-end protocol.
Use [the System One benchmark](system-one-benchmark.md) for the current typed,
calibrated protocol; `/fast` and `/api/v2/fast/diagnostics` remain aliases.

These are live, paired OpenKyrozen Web turns with local Kev-0.8B. Each case ran three times in a fresh session per mode, with the Fast-off and Fast-on order alternating by repetition. The same provider configuration served both modes within each run. Latency includes the full chat request; decision latency is measured separately. The answer check only searches for case keywords, so it is a smoke check rather than a semantic quality score.

Machine: Apple M3, 16 GB RAM, macOS 26.4, Python 3.12.13, Ollama 0.34.4. Kev ran on MLX with `jaredpalmer/kev-0.8b@9a45d25eb2ab761841196625383fa1dff0e56c1e`. The source base was `f79822b` plus this uncommitted Fast mode change.

## Reproducible Kev comparison with paid DeepSeek

This is the current reproducible result for the README. The server used the configured paid DeepSeek provider for both sides; only the per-session Fast setting changed. The benchmark ran the same four informational prompts three times each (12 matched pairs), used Ask mode, created a fresh session for every side, and alternated which side ran first on each repetition. Setup time is excluded from turn latency. The keyword check is a smoke check, not a semantic quality evaluation.

| Metric | Fast off | Fast on: Kev-0.8B |
| --- | ---: | ---: |
| Matched turns | 12 | 12 |
| Median end-to-end latency | 15.996 s | 16.476 s |
| P95 end-to-end latency | 24.557 s | 30.770 s |
| Median decision latency | 0 ms | 342 ms |
| LLM calls | 21 | 23 |
| Recorded LLM tokens | 79,013 | 80,040 |
| Keyword checks | 12/12 | 12/12 |
| Provider errors | 0 | 0 |

Kev activation took 406 ms for the first session and 345 ms median for later sessions; both values are outside the turn-latency measurements.

The aggregate ratio is **0.971x (Kev is 2.9% slower at the median)**. P95 is 25.3% slower with Kev. Per workload, Kev was 1.1% faster on fact questions, 10.9% slower on explanations, 10.1% slower on comparisons, and 5.5% slower on analysis. This is a measured regression for this configuration, not evidence of a speed improvement. Kev made an accepted decision on 3 of 12 turns and recorded a fallback on all 12; it cannot save time when the existing local routing is already cheap and the LLM workload does not change.

Raw data: [fast_mode_2026-09-27_deepseek_kev_repro.json](../benchmarks/results/fast_mode_2026-09-27_deepseek_kev_repro.json).

Repeat this exact protocol against a running localhost server with the same
DeepSeek configuration:

```bash
python benchmarks/fast_mode.py --url http://127.0.0.1:8000 \
  --backend kev --repeats 3 --mode ask \
  > benchmarks/results/fast_mode_$(date +%F)_deepseek_kev_repro.json
```

## Same LLM in both routing slots

Both `KYROZEN_MODEL_SIMPLE` and `KYROZEN_MODEL_COMPLEX` were `qwen2.5:7b`. This run isolates the effect of adding Kev decisions and changing task routing, without a model-switch speed advantage. All 12 pairs completed without a provider error.

| Workload | Fast off median | Kev median | Off ÷ Kev |
| --- | ---: | ---: | ---: |
| Fact | 1.59 s | 0.61 s | 2.61× |
| Explanation | 9.45 s | 8.57 s | 1.10× |
| Comparison | 19.60 s | 20.88 s | 0.94× |
| Analysis | 10.99 s | 20.98 s | 0.52× |

Across all 12 turns per mode, the median was **10.47 s off versus 13.42 s with Kev (0.78×, a 28% latency regression)**. P95 was 20.36 s versus 45.35 s. LLM calls were 18 versus 20; LLM tokens were 46,602 versus 47,923. Keyword checks passed on 9/12 versus 11/12. Kev made a high-confidence choice on 3/12 turns and fell back on at least one routing question on every turn. Its median decision-call latency was 210 ms. These small samples include local model cache effects and agent retries; the difference in keyword checks does not establish a quality improvement.

Raw data: [fast_mode_2026-09-27_kev_fixed_llm.json](../benchmarks/results/fast_mode_2026-09-27_kev_fixed_llm.json).

## Simple and reasoning LLMs configured separately

`KYROZEN_MODEL_SIMPLE=qwen3:0.6b` and `KYROZEN_MODEL_COMPLEX=qwen3:4b`. One Fast-off analysis turn returned an LLM error with zero recorded calls. Its matched Fast-on turn was excluded from summary statistics; both remain in the raw rows.

| Workload | Valid pairs | Fast off median | Kev median | Off ÷ Kev |
| --- | ---: | ---: | ---: | ---: |
| Fact | 3 | 4.13 s | 4.12 s | 1.00× |
| Explanation | 3 | 3.44 s | 4.37 s | 0.79× |
| Comparison | 3 | 20.08 s | 37.99 s | 0.53× |
| Analysis | 2 | 46.07 s | 42.76 s | 1.08× |

Across the 11 valid pairs, the median was 14.37 s off versus 5.55 s with Kev (2.59×); p95 was 56.11 s versus 60.44 s. LLM calls were 22 versus 26 and tokens were 57,976 versus 63,319. Keyword checks passed on all 11 valid turns in each mode. Kev made a high-confidence choice on 3/11 valid turns and fell back on at least one routing question on every turn. Median decision-call latency was 233 ms. The mixed result is not evidence that Kev alone caused the aggregate speedup: task and model behavior varied, and the per-workload results conflict.

Raw data: [fast_mode_2026-09-27_kev.json](../benchmarks/results/fast_mode_2026-09-27_kev.json).

## Live DeepSeek API rerun

The configured paid DeepSeek API served both sides: `deepseek-v4-flash` for simple turns and `deepseek-v4-pro` for reasoning turns. A live smoke request returned `4` for `2 + 2` before measurement. The four informational workloads above were rerun as three matched pairs each, with alternating order and fresh sessions. There were no provider errors or excluded pairs.

| Workload | Fast off median | Kev median | Off ÷ Kev |
| --- | ---: | ---: | ---: |
| Fact | 1.59 s | 1.70 s | 0.94× |
| Explanation | 1.34 s | 1.74 s | 0.77× |
| Comparison | 9.20 s | 8.83 s | 1.04× |
| Analysis | 2.83 s | 3.25 s | 0.87× |

Across the 12 turns per mode, median end-to-end latency was **2.21 s off versus 2.15 s with Kev** (1.03×); p95 was 9.97 s versus 8.85 s. The 2.6% aggregate difference is too small and inconsistent across workloads to establish a speedup. DeepSeek made 21 calls and used 67,357 recorded tokens off, versus 20 calls and 63,368 tokens with Kev. Keyword checks passed on 10/12 versus 11/12; they are not a semantic evaluation. The median Kev decision took 113 ms. It accepted a complexity choice on only the three fact turns and recorded a low-confidence fallback on every turn. The LLM model mix was almost unchanged: 18 Flash/3 Pro calls off and 17 Flash/3 Pro calls with Kev. Warm Fast activation was 397 ms for the first session and 75 ms median thereafter, measured outside turn latency.

Raw data: [fast_mode_2026-09-27_deepseek.json](../benchmarks/results/fast_mode_2026-09-27_deepseek.json).

### Read-only file diagnosis job

The project contained `fast_mode_fixture.py` with `average(values)` implemented as `sum(values) / len(values)`. The job was: inspect that file, name the exception from `average([])`, and suggest the smallest guard without editing files. Both sides used **Ask mode** and the same DeepSeek Flash/Pro configuration. Each of the six trials had a successful `read_file` receipt and answered `ZeroDivisionError` with an empty-input guard. No file was edited.

| Metric | Fast off | Kev |
| --- | ---: | ---: |
| Median end-to-end | 13.89 s | 17.88 s |
| P95 end-to-end | 60.69 s | 24.25 s |
| Correct trials | 3/3 | 3/3 |
| DeepSeek calls | 12 Pro | 8 Pro |
| Recorded LLM tokens | 35,949 | 24,617 |
| Median decision-call latency | 0 | 109 ms |
| Accepted Kev decisions / fallbacks | 0 / 0 | 0 / 3 |

The job's median shows a **22% regression** with Kev (off ÷ Kev = 0.78×). Its p95 is the slowest of only three samples; one Fast-off trial took 60.69 s and six DeepSeek calls. The other off trials took 13.89 s and 13.24 s; Kev trials took 17.88 s, 24.25 s, and 11.32 s. Kev accepted no decision for this job, so differences in DeepSeek retries and tokens cannot be credited to Kev routing. The completed answers and file-read receipts were also checked manually.

Raw data: [fast_mode_2026-09-27_deepseek_ask_job.json](../benchmarks/results/fast_mode_2026-09-27_deepseek_ask_job.json). To repeat, create this fixture in a disposable project as `fast_mode_fixture.py`:

```python
def average(values):
    total = sum(values)
    return total / len(values)
```

Start the Web server with `--project` pointing there, then run:

```bash
python benchmarks/fast_mode.py --url http://127.0.0.1:8000 --backend kev --repeats 3 --mode ask --cases benchmarks/deepseek_job_cases.json
```

An initial Auto-mode job run entered the plan-acceptance gate, so it did not complete the diagnosis; [its raw data](../benchmarks/results/fast_mode_2026-09-27_deepseek_auto_plan_gate.json) is excluded from the job result. An Agent-mode attempt was stopped after repeated DeepSeek tool-call retries, including one incomplete unwrapped tool call and a 25-call trial. It has no completed paired result. Ask mode was used for the valid read-only job measurement.

## Setup and reproduction

The successful Kev setup retry took **499.7 s**, including a live decision. Its isolated Python environment had already been installed by an earlier attempt; most of the successful retry was the Hugging Face checkpoint download. The initial `v0.1.0` tag failed package discovery, and a subsequent Xet download stalled, so 499.7 s is not a clean first-install measurement. The installer now pins the corrected upstream commit and uses standard HTTP for the model download. Warm Web activation took a median 327 ms in the fixed-LLM run. Restarting the stopped Kev service from the cached model and completing a live decision took 10.8 s.

To repeat, start the Web server with a working provider and the desired simple/reasoning model configuration, then run:

```bash
python benchmarks/fast_mode.py --url http://127.0.0.1:8000 --backend kev --repeats 3
```

Jev users can run the same command with `--backend jev` after configuring a TypeSafe API key. This historical report predates the live Jev and expanded calibrated runs; use [the current System One report](system-one-benchmark.md) for those results. OpenKyrozen's baseline route already uses local code, so a decision call by itself does not guarantee a faster turn. Kev also provides local decision processing without a paid decision API; the recorded confidence and fallback reason expose uncertainty without retaining request text.

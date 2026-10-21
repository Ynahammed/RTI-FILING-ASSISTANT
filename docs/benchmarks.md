# Benchmarks

Every score below was produced by a script in `scripts/` and written to `artifacts/`.
Run them all with:

```bash
python -m scripts.bench_all             # benchmarks only
python -m scripts.bench_all --with-eval # plus the 14 end-to-end scenarios (needs both services)
```

`bench_all` enforces nine gates and exits non-zero when any score falls below its floor,
so a regression fails instead of being reported as a pass. All numbers below come from one
run of that command (exit 0).

## Dataset

RTI-Bench (`joyboseroy/rti-bench`, CC-BY-4.0) has **1,516** cases in two sources. This is
the real public dataset in `data/rti-bench/`; only the portal and authority registry in
the product are synthetic.

| Source | Cases | What it contains | Evaluated |
|---|---|---|---|
| A `hf_annotated.csv` | 1,218 | request text + labels | **1,084 scored** (134 `UNKNOWN` excluded) |
| B `cic_combined.jsonl` | 298 | labels + metadata; order text for only 17 | 41 labelled; see section 4 |
| Total | 1,516 | | 71.5% scored, 91.2% examined |

Only `title` and `information_sought` are used as features. `instruction` and `response`
quote the PIO reply and the Commission's order, so a model trained on them learns the
verdict rather than the request. Scores on `instruction` are reported as leakage and must
never be quoted as a result.

## 1. Personal-data keyword screen

The deterministic rule triage uses (`agent.nlu.has_personal_data`), scored on
`information_sought`:

| Precision | Recall | F1 |
|---|---|---|
| 0.5294 | 0.0756 | 0.1324 |

It is precise and very conservative: it fires 17 times on 1,084 rows. That is why it is
safe to block on, and why the classifier below exists.

## 2. Section 8(1)(j) classifier (binary)

TF-IDF word unigrams/bigrams, logistic regression, `C=4`, balanced class weights, 5-fold
CV with folds by `hf_id % 5`.

| Metric | Value | Note |
|---|---|---|
| ROC AUC | **0.7170** | threshold-free |
| Average precision | **0.2545** | base rate 0.1098 |
| Nested CV F1 | **0.2980** | threshold chosen inside each fold |
| Out-of-fold F1 @0.43 | 0.3320 | threshold chosen on the scored rows (optimistic) |
| Keyword rule F1 | 0.1324 | same rows |

**Calibration.** Cross-fitted isotonic regression moves ECE from **0.1259 to 0.0312** and
Brier from 0.1134 to 0.0926. Calibrated scores may be read as probabilities; raw scores may
not.

**Selective precision.** If the screen abstains on the least confident rows, precision
among the rows it keeps is:

| Coverage | Precision | Threshold |
|---|---|---|
| 25% | 0.2177 | 0.2967 |
| 50% | 0.1642 | 0.1826 |
| 80% | 0.1326 | 0.1111 |

All three sit above the 0.11 base rate, so abstention buys real precision, but even at 25%
coverage two of three flags are wrong. The model stays advisory.

Pure-Python inference matches scikit-learn to **3.33e-16**.

## 3. Multi-label exemption classifier

Five sections with at least 25 positives: 8(1)(j) 119, 8(1)(d) 68, 8(1)(h) 67, 8(1)(e) 65,
8(1)(g) 27. One-vs-rest logistic regression; folds by `hf_id % 5`.

| Features | Macro AP | Macro AUC | Micro F1 @0.5 | Brier | ECE |
|---|---|---|---|---|---|
| word (1,2) | 0.1622 | 0.6873 | 0.2500 | 0.0805 | 0.1085 |
| **char_wb (2,6)** | **0.2269** | **0.7245** | **0.2727** | 0.0755 | 0.1046 |
| word + char (union) | 0.2072 | 0.7112 | 0.2508 | **0.0696** | **0.0668** |

- Mean base rate across the five labels: 0.0638, so macro AP is a **3.55× lift** over chance.
- Nested threshold selection gives micro F1 **0.2686** (precision 0.2158, recall 0.3555).
- Baselines: predicting nothing scores 0.000; predicting every label scores 0.120 micro F1.

**Selection bias is resolved.** Choosing the feature variant inside each outer fold picks
`char` in **all five folds**, giving pooled macro AP **0.2269** — identical to the
fixed-choice number, so the selection did not inflate it.

**Calibration is fixed.** Isotonic regression fitted on the other four folds only moves ECE
from **0.1046 to 0.0127** and Brier from 0.0755 to 0.0560.

## 4. Source B (298 CIC orders)

Source B carries labels but almost no text. Verified against the upstream Hugging Face
repository: `cic_combined.jsonl` is the same **650,085 bytes**, so this is how the dataset
is published, not a failed download.

| Measure | Value |
|---|---|
| Documents | 298 |
| Documents with any order text | **17** (5.7%) |
| Documents citing any section | 41 |
| Documents citing 8(1)(j) | 31 |
| 8(1)(j) positives among text-bearing documents | 1 |
| Orders yielding an extractable request span | 2 (0.7%) |
| Transfer / citation detection | **not scoreable** (too few rows with text) |

The earlier version of this benchmark reported a transfer AUC of 0.4984 on the full 298
rows. That number was real but meaningless: it scored mostly empty text. The harness now
refuses to report a text score when fewer than 50 records carry text or fewer than 10
positives exist, and reports `insufficient` instead.

Source B needs its full text fetched (or re-extracted from the PDFs) before any text-based
evaluation is possible. Until then it contributes corpus accounting and label prevalence
only.

## 5. Precedent retrieval

TF-IDF word + char, cosine similarity, self excluded. Four relevance definitions, because
"shared section" is only one proxy for usefulness:

| Relevance | Queries | P@10 | Chance P@10 | Lift | MRR@10 | nDCG@10 | Top-1 |
|---|---|---|---|---|---|---|---|
| shares any section | 315 | **0.1705** | 0.0807 | 2.11× | 0.3452 | 0.1773 | 21.6% |
| shares primary section | 315 | 0.1168 | 0.0549 | 2.13× | 0.2715 | 0.1223 | 15.2% |
| same authority | 358 | 0.0274 | 0.0066 | **4.15×** | 0.0901 | 0.0503 | 5.3% |
| same section and authority | 68 | 0.0103 | 0.0023 | 4.56× | 0.0168 | 0.0262 | 0.0% |

The lift is strongest on authority matching, which suggests the retriever is better at
finding the same institution than at finding a genuinely analogous ruling. Absolute
precision is low everywhere: even the best definition returns a relevant case about once
in six at rank ten.

Recall@10 is 0.0208 by construction — each query has roughly 87 relevant cases, so ten
slots cannot cover them. P@10, MRR and nDCG are the meaningful measures at this depth.

## 6. End-to-end workflow

`scripts/run_eval` drives the agent's HTTP API and the portal across 14 scenarios (TC1–TC4,
X1–X10), including the approval gate, fee ordering, exemption blocking, prompt injection,
wrong authority and deadline expiry. Last recorded run: **14/14 passed**.

## 7. Operations (real requests through the live system)

`scripts/bench_operations.py` samples real RTI-Bench requests and drives each one through
the running agent and portal exactly as a client would: create the run, answer the
follow-ups, approve, complete the operator CAPTCHA handoff, then poll to a terminal state.
Unlike the 14 curated scenarios, nothing here is hand-written.

Last recorded run (**40 requests, concurrency 4**):

| Measure | Value |
|---|---|
| Attempted | 40 |
| Reached a terminal state | 40 (100%) |
| Verified filings | 37 (92.5%) |
| Blocked for exemption review | 1 |
| Redirected as grievance | 1 |
| Harness errors | 0 |
| Duplicate registrations | 0 |
| Payments without approval | 0 |
| Latency p50 / p95 | 13.9 s / 45.4 s |
| Throughput | 12.65 runs/min at concurrency 4 |
| Exemption precision / recall vs dataset | 1.00 / 0.17 |

A sequential run (20 requests, concurrency 1) verified 19 of 20 with p50 4.1 s and p95
34.7 s. The higher latency at concurrency 4 is SQLite write contention, not a model cost.
Latency is wall-clock per run including polling, not server-only time.

**This benchmark found three defects the curated scenarios never touched**, all fixed:

1. A follow-up answer was appended to the request text and re-extracted, so answering the
   department question overwrote the state. Answers now fill the field their question asked
   about.
2. `intake()` rewrote already-answered fields at low confidence each turn, so the agent
   asked for the same information repeatedly. Confirmed values now survive a re-parse.
3. The "confirm authority" and "records or action" questions had no answer path, so a run
   could never terminate. Both now resolve: a confirmation routes the run, and a
   records-or-action reply sets the intent signal.

The exemption screen shows the same shape end to end as in isolation: precise (1.00)
and conservative (0.17 recall), so most 8(1)(j)-labelled requests are still filed rather
than blocked.

## 8. Citizen requests

`scripts/bench_citizen_requests.py` scores a labelled citizen-request file at
`data/citizen-requests.jsonl` when one exists. No such file is present, so it reports
`present: false` and scores nothing. The schema is documented in the script. This is the
only gap that more modelling cannot close.

## Gates

| Gate | Floor | Measured |
|---|---|---|
| 8(1)(j) ROC AUC | ≥ 0.65 | 0.7170 |
| 8(1)(j) nested F1 beats keyword rule | > 0 | +0.1656 |
| Multi-label macro AP over base rate | ≥ 2× | 3.55× |
| Retrieval P@10 over chance | ≥ 1.5× | 2.11× |
| Honest variant selection macro AP | ≥ 0.20 | 0.2269 |
| Multi-label calibrated ECE | ≤ 0.03 | 0.0127 |
| Binary calibrated ECE | ≤ 0.05 | 0.0312 |
| Retrieval authority P@10 over chance | ≥ 2× | 4.15× |
| Operations completion | ≥ 90% | 100% |
| Operations duplicate registrations | 0 | 0 |
| Operations payments without approval | 0 | 0 |
| End-to-end scenarios | all pass | 14/14 |

## What these numbers do not support

- **No claim of production accuracy on citizen text.** Every label here comes from an
  appellate decision. The citizen harness exists but has no data.
- **No claim that the classifier can block a filing.** At 0.28 nested precision it is a
  screening aid; the deterministic rule remains the only thing that blocks.
- **No Source B coverage claim.** Its text is absent for 281 of 298 records, so no text
  task can be scored on it.
- **Retrieval relevance is still a proxy.** Authority matching lifts precision over chance,
  but sharing an authority is not the same as being a useful precedent.
- **Calibration is fitted on 1,084 rows.** The ECE figures are measured, not guaranteed to
  hold out of domain.

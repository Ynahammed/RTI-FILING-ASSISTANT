# Benchmarks

Every score below was produced by a script in `scripts/` and written to
`artifacts/`. Run them all with:

```bash
python -m scripts.bench_all            # benchmarks only
python -m scripts.bench_all --with-eval # plus the 14 end-to-end scenarios (needs both services)
```

`bench_all` enforces gates and exits non-zero if any score falls below its floor, so a
regression fails instead of being reported as a pass. All numbers below come from one
run of that command (exit 0).

## Dataset

RTI-Bench (`joyboseroy/rti-bench`, CC-BY-4.0) has **1,516** cases in two sources. This
is the real public dataset in `data/rti-bench/`; only the portal and authority registry
in the product are synthetic.

| Source | Cases | What it contains | Evaluated |
|---|---|---|---|
| A `hf_annotated.csv` | 1,218 | request text + labels | **1,084 scored** (134 `UNKNOWN` excluded) |
| B `cic_combined.jsonl` | 298 | full CIC orders + labels, no request text | 41 labelled; see section 4 |
| Total | 1,516 | | 71.5% scored, 91.2% examined |

- 280 scored rows cite at least one exemption section.
- 65 rows cite more than one, so the multi-label task is genuine.
- Labels come from appellate decisions, **not** from citizen requests.

Only `title` and `information_sought` are used as features. `instruction` and `response`
quote the PIO reply and the Commission's order, so a model trained on them learns the
verdict rather than the request. Scores on `instruction` are reported as leakage and
must never be quoted as a result.

## 1. Personal-data keyword screen

The deterministic rule that triage uses (`agent.nlu.has_personal_data`), scored on
`information_sought`:

| Precision | Recall | F1 |
|---|---|---|
| 0.5294 | 0.0756 | 0.1324 |

It is precise and very conservative: it fires 17 times on 1,084 rows. That is why it is
safe to block on, and why the classifier below exists.

## 2. Section 8(1)(j) classifier (binary)

TF-IDF word unigrams/bigrams, logistic regression, `C=4`, balanced class weights,
5-fold CV with folds by `hf_id % 5`.

| Metric | Value | Note |
|---|---|---|
| ROC AUC | **0.7170** | threshold-free |
| Average precision | **0.2545** | base rate 0.1098 |
| Nested CV F1 | **0.2980** | threshold chosen inside each fold |
| Out-of-fold F1 @0.43 | 0.3320 | threshold chosen on the scored rows (optimistic) |
| Keyword rule F1 | 0.1324 | same rows |

The honest figure is the nested one. The model beats the rule it replaced by
**+0.166 F1**, and ships advisory-only: it never gates a filing. Pure-Python inference
matches scikit-learn to **3.33e-16**.

## 3. Multi-label exemption classifier

Five sections with at least 25 positives: 8(1)(j) 119, 8(1)(d) 68, 8(1)(h) 67,
8(1)(e) 65, 8(1)(g) 27. One-vs-rest logistic regression; folds by `hf_id % 5`.

| Features | Macro AP | Macro AUC | Micro F1 @0.5 | Brier | ECE |
|---|---|---|---|---|---|
| word (1,2) | 0.1622 | 0.6873 | 0.2500 | 0.0805 | 0.1085 |
| **char_wb (2,6)** | **0.2269** | **0.7245** | **0.2727** | 0.0755 | 0.1046 |
| word + char (union) | 0.2072 | 0.7112 | 0.2508 | **0.0696** | **0.0668** |

- Mean base rate across the five labels: 0.0638, so macro AP is a **3.55× lift** over chance.
- Nested threshold selection gives micro F1 **0.2686** (precision 0.2158, recall 0.3555).
- Baselines: predicting nothing scores 0.000; predicting every label scores 0.120 micro F1.

**Selection bias is resolved.** Comparing three variants on the same folds that are
reported would favour whichever variant happened to fit them best. Running the choice
inside each outer fold picks `char` in **all five folds** and gives a pooled macro AP of
**0.2269** — identical to the fixed-choice number, so the selection did not inflate it.

**Calibration is fixed.** Isotonic regression fitted on the other four folds only moves
the multi-label ECE from **0.1046 to 0.0127** and the Brier score from 0.0755 to 0.0560.
Calibrated scores may be shown as probabilities; raw scores may not.

## 4. Source B (298 CIC orders)

Source B has labelled sections but **no request text**. It cannot be scored as a
screening model without inventing the input, so this benchmark measures what is real:

| Measure | Value |
|---|---|
| Documents | 298 |
| Documents citing any section | 41 |
| Documents citing 8(1)(j) | 31 |
| Orders yielding an extractable request span | 2 (0.7%) |
| Transfer ROC AUC (trained on A, scored on B) | **0.4984** |
| Transfer average precision | 0.1052 (base rate 0.1040) |
| Keyword screen on orders | fires 0 times; recall 0.0 |

The transfer test is the useful negative result: a model trained on request text scores
at **chance** on order text, and the deterministic screen never fires. That is evidence
the task is request-shaped, not that either component is broken. Scoring Source B as if
it were screening would produce a number that means nothing.

## 5. Precedent retrieval

TF-IDF word + char, cosine similarity, self excluded. A retrieved case is relevant when
it shares an exemption section with the query.

| Relevance | P@10 | Chance P@10 | MRR@10 | nDCG@10 | Top-1 hit |
|---|---|---|---|---|---|
| shares any section | **0.1705** | 0.0807 | **0.3452** | 0.1773 | 21.6% |
| shares primary section | 0.1168 | 0.0549 | 0.2715 | 0.1223 | 15.2% |

315 cases have labels to retrieve against. Precision@10 is **2.11×** chance, and the
first result is relevant about one time in five versus one in thirteen by chance.

Recall@10 is low (0.0208) by construction: each query has roughly 87 relevant cases in
the corpus, so ten slots cannot cover them. P@10, MRR and nDCG are the meaningful
measures at this depth.

## 6. End-to-end workflow

Separate from the statistical benchmarks: `scripts/run_eval` drives the agent's HTTP API
and the portal across 14 scenarios (TC1–TC4, X1–X10), including the approval gate, fee
ordering, exemption blocking, prompt injection, wrong authority and deadline expiry.
It requires both services and is not part of `bench_all` unless `--with-eval` is passed.
Last recorded run: **14/14 passed**, with `bench_all` gating on it when
`artifacts/eval_results.json` is present.

## Gates

| Gate | Floor | Measured |
|---|---|---|
| 8(1)(j) ROC AUC | ≥ 0.65 | 0.7170 |
| 8(1)(j) nested F1 beats keyword rule | > 0 | +0.1656 |
| Multi-label macro AP over base rate | ≥ 2× | 3.55× |
| Retrieval P@10 over chance | ≥ 1.5× | 2.11× |
| Honest variant selection macro AP | ≥ 0.20 | 0.2269 |
| Calibrated ECE | ≤ 0.03 | 0.0127 |
| End-to-end scenarios | all pass | 14/14 |

Every score above, plus the gate results, is written to `artifacts/benchmarks.json` in one
record, so a reviewer can read the whole picture without running anything.

## What these numbers do not support

- **No claim of production accuracy.** The labels are appellate outcomes, not citizen
  requests. A model trained on citizen text would need citizen labels. This is the one
  gap that cannot be closed by more modelling, only by data acquisition.
- **No claim that the classifier can block a filing.** At 0.28 nested precision, it is a
  screening aid. The deterministic rule remains the only thing that blocks.
- **The binary 8(1)(j) model is uncalibrated.** Calibration was measured and fixed for the
  multi-label model only; do not show the binary score as a probability.
- **Retrieval relevance is a proxy.** Two cases can share a section without sharing an
  authority, a remedy, or a useful precedent.
- **Source B cannot validate screening.** It has labels but no request text, 0.7% of
  orders yield a request span, and transfer scores at chance. Its 298 cases are examined
  but not scored, which caps coverage at 71.5% of the corpus.

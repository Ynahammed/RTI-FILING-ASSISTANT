# Benchmarks

Every score below was produced by a script in `scripts/` and written to `artifacts/`.
Run them all with:

```bash
python -m scripts.bench_all             # benchmarks only
python -m scripts.bench_all --with-eval # plus the 14 end-to-end scenarios (needs both services)
```

`bench_all` enforces fifteen gates, plus the 16-scenario gate when its artifact is
present and three more for the live operations run with `--with-ops` (nineteen in total).
It exits non-zero when any score falls below its floor, so a regression fails instead of
being reported as a pass. All numbers below come from one run of that command (exit 0).

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
| 0.5263 | 0.0840 | 0.1449 |

It is precise and very conservative: it fires 19 times on 1,084 rows. That is why it is
safe to block on, and why the classifier below exists.

The rule was rebuilt for citizen phrasing after the development fixtures in section 9
showed it missing colloquial requests. It now matches records and subjects on word
boundaries with English plurals ("beneficiaries", "names and addresses"), recognises a
named or honorific subject ("Dr Kulkarni"), and treats an identity-only request about a
public servant — "names of the officers who approved" — as a request about a post rather
than a person. On appellate text that trades a little recall for precision; on the
citizen fixtures it was worth roughly 0.8 recall. Those fixtures are in-sample, so the
appellate numbers above remain the only out-of-sample measurement of the rule.

## 2. Section 8(1)(j) classifier (binary)

TF-IDF word unigrams/bigrams, logistic regression, `C=4`, balanced class weights, 5-fold
CV with folds by `hf_id % 5`.

| Metric | Value | Note |
|---|---|---|
| ROC AUC | **0.7170** | threshold-free |
| Average precision | **0.2545** | base rate 0.1098 |
| Nested CV F1 | **0.2980** | threshold chosen inside each fold |
| Out-of-fold F1 @0.43 | 0.3320 | threshold chosen on the scored rows (optimistic) |
| Keyword rule F1 | 0.1449 | same rows |

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

`scripts/run_eval` drives the agent's HTTP API and the portal across 16 scenarios (TC1–TC4,
X1–X12), including the approval gate, fee ordering, exemption blocking, prompt injection,
wrong authority, deadline expiry, and both review decisions with their audit trail. Last
recorded run: **16/16 passed**.

## 7. Exemption review routing

Blocking a filing on a classifier with 0.30 precision would refuse valid requests. The
screen therefore **routes to a human review queue** and never files a flagged request on
its own; it does not reject it. `scripts/bench_review_routing.py` scores that policy on
the same 1,084 rows (119 positives):

| Policy | TP | FP | FN | Precision | Recall | Review load |
|---|---|---|---|---|---|---|
| Keyword rule blocks | 10 | 9 | 109 | 0.5263 | 0.0840 | 1.75% |
| Rule OR classifier ≥ 0.42 | 47 | 106 | 72 | 0.3072 | **0.3950** | 14.1% |

That is **4.7× the rule's recall** for one review in seven requests. The threshold is not
hand-picked: the selection rule is *maximise recall subject to precision ≥ 0.28 and review
load ≤ 16%*, measured on out-of-fold scores. Re-choosing the threshold inside each outer
fold with no knowledge of the held-out rows gives recall **0.3697** at precision 0.3165,
so the number is not an artifact of fitting the threshold on the rows it is scored on.
Those re-selections land at 0.42, 0.42, 0.50, 0.43 and 0.42: one fold moves to 0.50 because
its inner rows miss the 0.28 precision floor by 0.008 at 0.42. The pooled nested recall is
the number the gate checks; the spread is reported rather than smoothed away.

What this means in the product: `agent.supervisor` queues a request for review when the
calibrated classifier score reaches the threshold **or** the deterministic rule fires. The
gate is unchanged — review is entered before approval, so nothing is filed while a flag is
open. A flag is work with an owner: claim, due time, release or uphold, and a per-operator
audit row; scenarios X11 and X12 exercise both decisions through the HTTP API.
On the 40 live requests in section 8 the combined screen caught **6 of 6**
8(1)(j)-labelled requests with one false positive (precision 0.86), where the rule alone
caught 1. That run predates the rule rebuild in section 1: the rebuilt rule is narrower on
identity-only requests, so a rerun could move a row or two, not the conclusion.

## 8. Operations (real requests through the live system)

`scripts/bench_operations.py` samples real RTI-Bench requests and drives each one through
the running agent and portal exactly as a client would: create the run, answer the
follow-ups, approve, complete the operator CAPTCHA handoff, then poll to a terminal state.
Unlike the 14 curated scenarios, nothing here is hand-written.

Last recorded run (**40 requests, concurrency 4**):

| Measure | Value |
|---|---|
| Attempted | 40 |
| Reached a terminal state | 40 (100%) |
| Verified filings | 32 (80%) |
| Queued for exemption review | 7 |
| Redirected as grievance | 1 |
| Harness errors | 0 |
| Duplicate registrations | 0 |
| Payments without approval | 0 |
| Latency p50 / p95 | 3.15 s / 3.92 s |
| Throughput | 65.8 runs/min at concurrency 4 |
| Exemption screen precision / recall vs dataset | 0.86 / 1.00 |

Latency and throughput at three concurrency levels, same 40-request mix (20 at C=1):

| Concurrency | Verified | Review | p50 | p95 | Throughput |
|---|---|---|---|---|---|
| 1 (20 requests) | 15 | 4 | 1.40 s | 1.79 s | 47.9 runs/min |
| 4 (40 requests) | 32 | 7 | 3.15 s | 3.92 s | 65.8 runs/min |
| 8 (40 requests) | 32 | 7 | 7.31 s | 10.27 s | 56.5 runs/min |

Repeat runs vary by about 20% (C=4 p95 has landed between 2.7 s and 3.9 s), which is
noise next to the pre-fix 45 s. Concurrency 4 remains the sweet spot; at 8 the SQLite
writer serialises and per-run latency triples for less throughput than C=4. Before the
fix below, the same C=4 run took **45.4 s p95** and 12.65 runs/min, and C=1 took 34.7 s
p95 — the queue was waiting on the portal's SQLite writer, not on the model.

**The contention fix.** The portal now keeps one connection per request with a bounded
`busy_timeout`, WAL mode, and short write transactions; the filing path stopped holding a
transaction open across the CAPTCHA handoff. The verifier lease is claimed once per run
rather than polled. Latency is wall-clock per run including polling, not server-only time.

**This benchmark found three defects the curated scenarios never touched**, all fixed:

1. A follow-up answer was appended to the request text and re-extracted, so answering the
   department question overwrote the state. Answers now fill the field their question asked
   about.
2. `intake()` rewrote already-answered fields at low confidence each turn, so the agent
   asked for the same information repeatedly. Confirmed values now survive a re-parse.
3. The "confirm authority" and "records or action" questions had no answer path, so a run
   could never terminate. Both now resolve: a confirmation routes the run, and a
   records-or-action reply sets the intent signal.

The exemption screen shows a different shape end to end than the rule alone did: with the
classifier in the routing path it caught all 6 8(1)(j)-labelled requests in this sample
and queued them for review instead of filing them, at one false positive in 40
(precision 0.86). Six positives is a small sample; section 7's 119-positive measurement
is the number to trust.

## 9. Citizen requests

`scripts/bench_citizen_requests.py` scores a labelled citizen-request file at
`data/citizen-requests.jsonl`. No such file exists yet, so the artifact reports
`present: false`, the reason, and how far the set is from the 100-row target. The schema is
in the script; collection, labelling and adjudication run through
`python -m scripts.annotate_server`, and the full protocol (recruitment, consent, codebook,
sample-size reasoning, timeline) is [docs/citizen-data.md](citizen-data.md).

The benchmark refuses an unfrozen file, a frozen file whose hash no longer matches, fewer
than 100 adjudicated rows, fewer than 25 positives or 25 negatives, and kappa below 0.6.
When it does score, it reports Wilson intervals beside precision and recall, because at
n = 100 a point estimate alone invites over-reading.

**Development-only checks, not evidence.** `scripts/bench_citizen_dev.py` scores two
synthetic citizen-style files written and labelled by the author, marked `evidence: false`
and blocked from freezing:

| File | Rows | Rule precision / recall | State |
|---|---|---|---|
| `data/dev/synthetic-citizen.jsonl` | 100 (35 positive) | 1.00 / 1.00 | in-sample |
| `data/dev/paraphrase-holdout.jsonl` | 40 (20 positive) | 1.00 / 1.00 | in-sample |

The history matters more than the numbers. The first file, written before the screen was
changed, scored the old rule at **0.14 recall at 1.00 precision** (5 of 35): it found
eleven personal-data requests in plain English and missed "the residential address of the
PIO", "the Aadhaar numbers of beneficiaries" and "the fee defaulters list with student
names". The rule was extended against those misses. A second file was then written in
different phrasings and scored once, before any repair: **0.55 recall at 0.79 precision**,
with three false positives on public-office text — the municipal commissioner's *office*
address, the *number* of patients treated in the OPD, and a post's salary *scale*. Those
three fixed the rule's shape rather than its vocabulary: identity-only requests about
public servants stopped counting, and "address of", "patient" and bare "salary" were
replaced by terms that mean a private fact.

Both files are now saturated because both were used to tune. They are regression
fixtures — they catch a future edit that reopens one of these gaps — and they cannot
support a citizen-accuracy claim. Only the frozen 100-row human set can; that remains
`present: false`.

## Gates

| Gate | Floor | Measured |
|---|---|---|
| 8(1)(j) ROC AUC | ≥ 0.65 | 0.7170 |
| 8(1)(j) nested F1 beats keyword rule | > 0 | +0.1531 |
| Multi-label macro AP over base rate | ≥ 2× | 3.55× |
| Retrieval P@10 over chance | ≥ 1.5× | 2.11× |
| Honest variant selection macro AP | ≥ 0.20 | 0.2269 |
| Multi-label calibrated ECE | ≤ 0.03 | 0.0127 |
| Binary calibrated ECE | ≤ 0.05 | 0.0312 |
| Retrieval authority P@10 over chance | ≥ 2× | 4.15× |
| Review routing recall | ≥ 0.35 | 0.3950 |
| Review routing precision | ≥ 0.28 | 0.3072 |
| Review routing load | ≤ 16% | 14.11% |
| Every required rule is cited | 0 uncited | 0 |
| Every jurisdiction has an authority | 0 uncovered | 0 (36/36) |
| Registry entries carry provenance | 0 missing | 0 |
| No rule claims verification without a source | 0 | 0 |
| Operations completion | ≥ 90% | 100% |
| Operations duplicate registrations | 0 | 0 |
| Operations payments without approval | 0 | 0 |
| End-to-end scenarios | all pass | 16/16 |

## What these numbers do not support

- **No claim of production accuracy on citizen text.** Every label here comes from an
  appellate decision. The citizen harness exists but has no data.
- **The classifier cannot reject a request.** At 0.31 routing precision it queues for a
  human, which is why review load is capped at 16%. A reviewer still decides.
- **The review queue has no owner yet.** Flags route to a queue the dashboard shows, but
  there is no per-operator assignment or SLA.
- **No Source B coverage claim.** Its text is absent for 281 of 298 records, so no text
  task can be scored on it.
- **Retrieval relevance is still a proxy.** Authority matching lifts precision over chance,
  but sharing an authority is not the same as being a useful precedent.
- **Calibration is fitted on 1,084 rows.** The ECE figures are measured, not guaranteed to
  hold out of domain.

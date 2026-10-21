# Getting 100 adjudicated citizen requests

This is the plan and the tooling for the one number the project cannot produce from
public data: **how well the screening works on text a citizen actually wrote**, rather
than on a Commission order. It is deliberately small — 100 rows, two annotators, one
adjudicator, three weeks — because a defensible 100 is worth more than an impressive
1,000 nobody trusts.

The pipeline refuses to score anything else. `bench_citizen_requests` reports
`present: false` until a frozen file exists with at least 100 adjudicated rows, at least
25 positives and 25 negatives, two annotators, and Cohen's kappa ≥ 0.6.

## Why 100, and what it can support

At n = 100 with the expected mix (about 30–40 positives), a 95% Wilson interval on a
recall or precision near 0.5 is roughly **±0.17**. Cohen's kappa around 0.7 gets a
bootstrap interval of roughly **±0.12**.

That is enough to answer the question that matters: *does the screen work on citizen
language at all, and is it in the same range as on appellate text?* It is **not** enough
to separate 0.55 from 0.65, or to certify a threshold. Report intervals, never a single
percentage. If someone asks for tighter numbers, the answer is more rows, not a better
statistic.

## Who labels

| Role | Who | Why them | Time |
|---|---|---|---|
| Annotator A | Someone with RTI experience (law student, activist, paralegal) | Knows what 8(1)(j) means without a lecture | 3–4 h |
| Annotator B | A second person with similar background, **not** the builder | Independence is the whole point | 3–4 h |
| Adjudicator | The project owner or a senior reviewer | Resolves disagreements with a written reason | 2–3 h |

Rules:

- **Neither annotator may be the author of the system or of most requests.**
- Annotators work **independently** and do not discuss rows before both have labelled.
- Recruit through a law-school clinic, an RTI activists' network, or a civic-tech group.
  A paid 4-hour engagement at a fair hourly rate is faster and cleaner than favours;
  budget roughly ₹6,000–8,000 total at ₹500/hour.
- Run a **10-row calibration batch** first. If kappa on that batch is below 0.6, fix the
  codebook and brief again; do not proceed to the full set hoping it improves.

## What to collect

Ask contributors for a real request they wrote and filed, with consent. Do not
commission requests to fit the label — that produces a set that measures the brief.

- Target pool: **150 candidates** to net 100 after duplicates, short texts, withdrawals
  and skips.
- Aim for **at least 30% involving third-party personal information**, but recruit across
  topics rather than cherry-picking; if the natural mix is 15% positive, report that.
- Cover the languages the product claims: English, Hindi, Marathi, Telugu, Kannada, Tamil.
  Record `language` on every row and report the mix; a 95%-English set cannot support a
  multilingual claim.
- Obvious identifiers are removed automatically (`redact()` in
  `scripts/annotate_citizen.py`: email, phone, Aadhaar, PAN). The consent form still tells
  contributors not to include names or addresses, because patterns cannot catch those.

### Consent text (shown in the app)

> I wrote this request about a real matter, or I have the author's permission to share it.
> I understand it will be redacted, labelled by two people, and may be published as part of
> an evaluation set. I have not included anyone's name, address, phone number or identity
> document in the text.

Keep submissions in `data/citizen-annotations.jsonl` (gitignored if you prefer; the frozen
file plus manifest is the publishable artifact). Contributors can withdraw a row before
the freeze; after the freeze, the hash is the record and the file is not edited.

## The codebook

One question, asked identically of every row:

> Does this request seek another person's private information — their address, phone
> number, identity, health, family or financial details — rather than information about how
> a public body works or spends money?

**Yes (personal data, 8(1)(j)-shaped):**

- the home address or phone number of a named officer
- medical records or test results of named patients
- a named person's salary, bank account, loan or tax details
- family details, caste certificate copies, or identity documents of a third party

**No (records about public bodies):**

- how much a department spent, on what, under which contract
- attendance, minutes, file notings, inspection reports, work orders
- a public official's **official** duties and decisions (their designation and office are
  public; their home address is not)
- a company's balance sheet or tender documents (that is 8(1)(d), a different label — the
  set scores only 8(1)(j))
- a request for the contributor's **own** records

When a row mixes both, label **yes** if a reasonable PIO would need to consider 8(1)(j);
write the reason in the adjudication note.

## Protocol

1. **Week 1 — collect and calibrate.** Stand up the app, circulate the contribution link,
   collect 150 candidates, run the 10-row calibration batch with both annotators, agree the
   codebook wording, then start.
2. **Week 2 — label independently.** Each annotator labels in 20–30 minute batches. The app
   shows one request at a time and never shows the other annotator's label. Target 100
   rows each, roughly 90 seconds per row.
3. **Week 3 — adjudicate and freeze.** The adjudicator resolves every disagreement with a
   one-line reason. Run `freeze`, which re-checks kappa, the row count and the class mix,
   writes the SHA-256 manifest, then run the benchmark.

Do not skip in this order: labelling after seeing the model's score, filtering rows after
seeing labels, or hand-editing the frozen file all invalidate the set.

## Commands

```bash
# 1. Local UI: contribution form, labelling, adjudication
python -m scripts.annotate_server --port 8010
#    contributor:  http://127.0.0.1:8010/contribute
#    annotator A:  http://127.0.0.1:8010/label?annotator=A
#    annotator B:  http://127.0.0.1:8010/label?annotator=B
#    adjudicator:  http://127.0.0.1:8010/adjudicate?name=C
#    status:       http://127.0.0.1:8010/

# 2. Or import candidates collected elsewhere
python -m scripts.annotate_citizen import --input candidates.jsonl

# 3. Inspect progress and agreement at any time
python -m scripts.annotate_citizen stats

# 4. Resolve an open disagreement from the CLI instead of the page
python -m scripts.annotate_citizen review
python -m scripts.annotate_citizen adjudicate --id CIT-0007 --value yes --adjudicator C

# 5. Freeze (refuses below 100 rows, below 25/25 per class, or kappa < 0.6)
python -m scripts.annotate_citizen freeze

# 6. Score the screening against the frozen set
python -m scripts.bench_citizen_requests
```

The app writes to `data/citizen-annotations.jsonl`; `freeze` writes
`data/citizen-requests.jsonl` and its manifest. Point `RTI_CITIZEN_ANNOTATIONS` elsewhere
to keep experiments apart.

## Why not just write the requests yourself?

You can — but you cannot call them evidence. If one person writes the requests and labels
them, then both "annotators" are the same opinion, kappa is 1.0 by construction, and the
result measures how well the screen handles that person's phrasing. That is the circularity
the two-annotator rule exists to break.

What is legitimate is a **development set**: synthetic rows, author labels, used to test the
pipeline and the codebook before paying annotators. `data/dev/synthetic-citizen.jsonl` holds
100 such rows (35 personal-data, 65 public-records, including the codebook's edge cases).
Every row is marked `synthetic: true`, `freeze` refuses them, and the scorer writes
`evidence: false` with the warning attached.

```bash
python -m scripts.bench_citizen_dev
```

What it found, on that set: the keyword screen reached **0.14 recall** at 1.00 precision
(5 of 35), and adding the calibrated classifier at its routing threshold raised recall to
0.17 at 1.00 precision and 6% review load. On appellate text the same screen reaches 0.39
recall at 0.31 precision. The direction is the point: **screening tuned on Commission orders
appears to miss most colloquial personal-data requests**, which is exactly what the real set
is needed to confirm or refute. The dev set cannot tell you whether the true citizen recall
is 0.14 or 0.35; it can tell you the codebook is usable and that the screen probably needs
citizen text in training, not just in evaluation.

## What that finding changed

The screen was rebuilt against those misses, and the rebuild is the useful part of the
story.

A second author-written file, `data/dev/paraphrase-holdout.jsonl` (40 rows, 20 positive,
different phrasings, one Marathi row), was written from the codebook after the first fix and
scored **once, before any repair**: **0.55 recall at 0.79 precision**. It missed dealers and
accountants, passbooks and roll numbers, a named informant's address, and a Marathi address
request; it falsely flagged the municipal commissioner's *office* address, the *number* of
patients treated in the OPD, and a post's salary *scale*.

Those three false positives were the most useful output. They said the rule was matching
words rather than meaning:

- an **identity-only** request ("names of the officers who approved") is about a post, not a
  person — the codebook already says a designation is public and a home address is not;
- "address of", "patient" and bare "salary" are not private facts. A residence address is;
  a headcount of patients is not; a pay *scale* is not.

Both files now score 1.00, because both were used to tune. That is memorisation. They are
kept as regression fixtures: `scripts/bench_citizen_dev.py` reports them with
`evidence: false` and an explicit note that the screen was repaired against them, and an
edit that reopens one of these gaps fails a test. The appellate benchmark still shows the
rule's out-of-sample behaviour — 0.53 precision at 0.08 recall, deliberately conservative —
and nothing here says how it will do on the next hundred real requests.

## Honest limits of this design

- **Volunteer bias.** People who answer an RTI-community call are more literate and more
  English-speaking than the median applicant. Report the language and channel mix; do not
  claim representativeness.
- **Positives are rarer than the screen's error rate.** At 30 positives, one false negative
  moves recall by 3 points. That is why intervals are mandatory.
- **Two annotators is the floor, not the ideal.** It buys a kappa, not a gold standard; the
  adjudicator's reasons are the audit trail.
- **The label is one section, not the whole law.** This set measures 8(1)(j) routing. It says
  nothing about 8(1)(d), 8(1)(h), fee correctness or routing accuracy.

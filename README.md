# dataassay

Audit a tabular dataset — locally.

An assay characterizes a sample before it makes any claim about it. This tool
works in the same order:

1. **Characterize.** Establish what kind of thing each column is — its support,
   cadence, panel shape, whether a standard deviation can be established at all.
2. **Test.** Run only the checks that those properties make *valid*. A 3σ rule
   on a heavy-tailed series is not a weak check; it is an invalid one.
3. **Grade.** Rank what came back, and separate real defects from source-side
   bookkeeping that merely looks like one.

A check that could not be run is a deliverable. "Seasonality undetermined — only
1.2 cycles of history" is something you need to know, and it appears in the
report next to what did run.

## Status

**v0.7.0.** `assay audit --report out.html` writes a self-contained report and
a flagged-items CSV. `assay profile` characterizes; `assay catalog` prints what
the checks are; `assay init` writes a manifest and `assay declare` answers it;
`assay interview` optionally asks a model to propose answers, sending metadata
only.

Validated against a corpus of known defects (see `VALIDATION.md`) and swept
against 104 real CSVs from the pipeline it was built for, where the metric that
matters is defects per file rather than findings per file.

Thirteen checks, each earned by a real defect found in a production pipeline:
constant/all-zero columns, mojibake, duplicate rows, duplicate grain, future
dates, cadence gaps, file order, stale tails, flatline tails, saturation at a
bound, negligible residue, level shifts, and schema drift.

The observation date does not have to be a date column. Where it is spread
across `year`, `month` and `dekad`, or across `year` and a label like
`"END OF APR"`, it is assembled — otherwise five of the thirteen checks are
withheld on a whole class of agricultural and government files.

Precision is measured but never filed as a fault. A column where most values
carry 15+ significant digits has been computed, not reported — that is
provenance, and it belongs beside the column rather than in the findings.

Every check declares the properties it needs and is refused when the profile
has not established them. What was withheld and why travels beside the
findings, because zero findings at 30% coverage and zero at 95% are different
objects and one number cannot tell them apart.

Findings carry a disposition — likely defect, worth a look, or the source doing
its own bookkeeping — because most anomalies in real data are the source's own
calendar, and a tool that cannot say so gets switched off within a week. They
also carry the predicate that produced them, so any claim can be re-run without
this code.

Confidence is never an opaque score. Each input is named, and agreement between
two independent checks on one column promotes it.

## Install

```
pip install dataassay
```

## Use

```
assay audit data.parquet          # characterize, then check
assay profile data.csv            # characterize only
assay init data.csv               # write a manifest to answer its questions
assay declare data.csv --time-axis d   # record an answer in it
assay catalog                     # what the checks are and why
assay audit data.csv --json       # the machine contract
assay interview data.csv          # optional: ask a model (metadata only)
```

## Validation

It was tested against 82 findings from a real pipeline audit, on the files as
they stood *before* those findings were fixed, recovered from git — and on the
same files afterwards. See [VALIDATION.md](VALIDATION.md), including the
denominator (only ~30 of the 82 are visible in a data file at all) and the four
families still out of reach.

## The report

One HTML file. No CDN, no external request, no dependency — charts are inline
SVG drawn by hand, and the machine-readable findings are embedded in the page
rather than written beside it, so the human artifact and the machine contract
are the same bytes and cannot drift.

Ordered the way it has to be read: provenance, then **coverage before the
findings** (an empty findings list means nothing until you know how many checks
ran), then the findings — each with its governing property, one chart, the
composition of its confidence, and the predicate to re-run it without this code
— then what each column was taken to be, where a wrong assumption of ours is
visible, then every check including the ones that passed.

Charts are theme-aware and never lean on colour alone: a flagged point carries
a rule and a direct label, a flagged range is a band with one label, and any
chart with two categories carries a legend.

## The manifest

`assay init` writes `<file>.assay.json`: what the tool detected, the questions
it could not answer, and an empty `declared` block for you to fill in.

The two blocks are kept apart on purpose. `detected` is regenerated every time
and is only there for reference; `declared` is what a person says is true, and
it always wins. So a value's provenance is never ambiguous — there is no
guessing later whether a grain was inferred or confirmed, and the report can be
honest about which checks rest on an assumption and which rest on an answer.

Answering once is what makes the tool usable more than once. The next audit of
the same dataset asks nothing, and it can run where there is nobody to ask at
all — a pipeline, a server, CI. The conversation is just the most convenient
way to author the file the first time.

`assay declare` is how you answer without opening the file:

```
assay declare data.csv --time-axis observed_at
assay declare data.csv --grain observed_at,site
assay declare data.csv --skip high_null        # seen it, not answering
assay declare data.csv --accept-proposed       # the model was right
```

Running a command is a person deciding, so `declared` keeps meaning what it
says. Two things it refuses: a declaration naming a column the file does not
have (that would gate checks on a fiction), and promoting anything out of
`proposed` unless you pass `--accept-proposed` — the model never gets a quiet
path into `declared`.

A skipped question is not a hidden one. It moves out of the open list and into
a section of its own, because a question nobody has looked at and a question
someone has judged not worth answering are different states, and showing a
decision as outstanding work is how a report teaches people to stop reading
it.

## The interview (optional)

`assay interview` sends the **profile** — column names, types, counts,
quantiles, and the questions the profiler could not settle — to a model, and
writes what it proposes into the manifest.

Three things make it safe to use on data you cannot upload.

**It sends metadata, never rows.** You see exactly what would go, before it
goes: a plain-language summary, then a confirmation. `--show-payload` prints the
literal bytes and sends nothing. Two disclosures are admitted rather than
hidden — column *names* have to go, and low-cardinality columns include their
distinct values because a categorical column cannot be characterized without
them. `--redact-values` strips the second.

**Proposals never become answers.** They land in the manifest's `proposed`
block, beside `declared`, and are never applied. You move across what you agree
with. `declared` records that a person decided, and that is only worth
something if it stays true.

**The model does not choose which checks run.** It proposes property
declarations; the engine gates the checks itself, deterministically, exactly as
it does for a declaration you typed. Handing check selection to a language model
would throw away the discipline the whole tool is built on.

Credentials come from the environment only — never a file, never an argument,
so a key cannot end up in your shell history or a commit.

## Privacy

Nothing is uploaded. The engine runs entirely in your process against your
files, and has exactly one runtime dependency (`duckdb`, which has no
transitive dependencies of its own). The LLM adapter is an optional extra
(`pip install 'dataassay[llm]'`) and the only module in the package that opens a
network connection — a test walks every other module's imports and fails if a
network library appears in one.

## Where this came from

dataassay was extracted from the data pipeline behind
[SoftSignal](https://getsoftsignal.com), a small independent commodities
research operation. These are the checks we run against our own feeds — USDA,
CONAB, CFTC, NOAA and others — before anything built on them is published.

That is the whole reason it behaves the way it does. It refuses checks whose
assumptions do not hold because we needed to know which of our own numbers were
actually supported. It reports what it could not check because a silent gap in
our data becomes a confident sentence in a report. Most of its checks exist because
something got past us first, and each one records what earned it — run
`assay catalog` and read the "earned by" line under any check.

## Support

MIT, and offered as-is. This is tooling we maintain because we depend on it, not
a product with a support commitment behind it. Issues and pull requests are
welcome and will be answered when time allows; there is no SLA, and no promise
of backward compatibility before 1.0 — the version numbers are deliberately low
because the check catalog is still moving.

If you are relying on it for something that matters, pin the version.

## License

MIT © SoftSignal LLC

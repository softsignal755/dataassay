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

**v0.2.0 — the check catalog.** `assay profile` characterizes; `assay audit`
characterizes and then checks; `assay catalog` prints what it knows how to look
for. The HTML report is not here yet.

Nine checks, each earned by a real defect found in a production pipeline:
constant/all-zero columns, mojibake, duplicate rows, duplicate grain, future
dates, cadence gaps, flatline tails, saturation at a bound, and level shifts.

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
assay catalog                     # what the checks are and why
assay audit data.csv --json       # the machine contract
```

## Privacy

Nothing is uploaded. The engine runs entirely in your process against your
files, and has exactly one runtime dependency (`duckdb`, which has no
transitive dependencies of its own). When an LLM adapter arrives it will be an
optional extra, it will receive *metadata only* — dtypes, counts, quantiles,
histogram bins — never rows, and it will show you that payload before sending
it.

## License

MIT © SoftSignal LLC

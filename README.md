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

**v0.0.1 — Phase 0.** Skeleton, packaging, and a provenance slice. Property
detection, the check catalog, and the HTML report are not here yet.

## Install

```
pip install dataassay
```

## Use

```
assay profile data.parquet
assay profile data.csv --json
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

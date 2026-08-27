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

**v0.1.0 — the profiler.** Characterization only. The check catalog and the
HTML report are not here yet.

`assay profile` reports:

- **Provenance** — content hash, shape, declared types.
- **Raw-text evidence**, gathered *before* parsing, because the two worst
  defects in tabular data do not survive it. `1.234` is 1234 under one decimal
  convention and 1.234 under the other; `03/04/2026` is valid as both DD/MM and
  MM/DD and they disagree. Both produce perfectly valid values, so nothing
  downstream ever errors. The tool gathers evidence and refuses to guess when
  the evidence does not decide.
- **Measurements** — nulls, cardinality, quantiles, dispersion, sentinel codes
  masquerading as numbers, and blocks of columns that go missing on the same
  rows.
- **Observed properties**, each with its evidence, including whether a standard
  deviation can be established at all. When the tail inflates σ past the robust
  scale, a 3σ rule is not weak — it is invalid, and the gate closes so that
  later checks cannot silently use it.

Questions are rationed. The tool asks only where an answer changes which checks
are valid, resolves what it can from file-wide evidence, and states the
assumptions it made instead of asking about them. Across the 103 CSVs it was
developed against, 73 raise no question at all and none raises more than four.

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

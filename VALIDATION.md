# Validation against a known-answer corpus

Most data-quality tools are demonstrated on data whose defects the author put
there. This one was tested against 82 findings from a real pipeline audit, on
the actual files as they stood before those findings were fixed — recovered
from git — and on the same files afterwards.

That gives both halves of a backtest: does it find what was there, and does it
go quiet once the defect is gone.

## What the corpus is

`audit/AUDIT_REPORT.md` in the parent repository: 6 critical, 19 high, 33
medium, 24 low, all confirmed against live data in June 2026, all since fixed.

Twenty of the affected CSVs are tracked in git, so their pre-fix state is
recoverable at `ac80af0^`.

## The honest denominator

The corpus is a **code** audit, and most of it is out of reach of any tool that
reads a data file:

| Class | Count | Why |
|---|---|---|
| Code-only | ~30 | The bug is in a generator or report; the file it read was correct. A single- vs double-underscore column name, a substring match pulling in the wrong contract, a hard-coded "neutral". |
| Infrastructure | ~15 | Serving, refresh, registry, alerting. Not a data file at all. |
| **Data-visible** | **~30** | The defect is in a file. This is the only part that can be scored. |

Counting a hit rate against 82 would be dishonest. The denominator is ~30.

## Results on the pre-fix files

Three defects, on the twenty recoverable files:

| File | What was found | Corpus finding |
|---|---|---|
| `livestock_storage.csv` | Rows not in time order, 32 inversions; file ends 2026-03-01 while the latest observation is 2026-04-01 | **H7** — "cold-storage CSV sorted alphabetically by month — surfaces serve March as latest while April exists" |
| `livestock_cof.csv` | Rows not in time order, 26 inversions | H7 class |
| `energy_weather_history.csv` | 1,080 exact duplicate rows | not in the corpus — an independent find |

H7 is the clean result: reproduced from the pre-fix file, in the tool's own
words, and **silent on the fixed file**. A true positive and a true negative on
the same check.

Two defects survive into today's data and are live:

- `livestock_cof.csv` carries an exact duplicate of the 2020 cattle-on-feed
  inventory row, and ends on July placements while August inventory sits in the
  file.
- `_history_*.csv` snow-water columns hold 3,683 values of `-7.35e-22` where
  zero belongs.

## What the misses taught

The gaps clustered, and the clusters were worth more than the hit rate.

**The tool was blind to a whole file shape.** `coffee_weather.csv` keys on
`year, month, dekad`; `livestock_storage.csv` on `year` plus `"END OF APR"`.
Neither has a date column, so five of eleven checks were withheld on each —
and that is *why* H7 survived: there was no axis to notice alphabetical order
against. Composite time axes are now assembled from year + month (+ day or
dekad), including month-name labels. With an axis established, `coffee_weather`
immediately began flagging `crop_water_stress_index_deviation` — the column
behind C4 and H15.

**Order is not a value.** Every number in a file can be right while the row
order is wrong, and a great deal of code depends on order without saying so.
`file_order` was earned by H7 directly.

**"Behind" needs a reference that travels.** H14 (a fetcher never scheduled,
file frozen at 2026-05-07) is invisible to a freshness check reading the file's
timestamp. `stale_tail` compares the last observation against the file's own
load stamp where one exists, so it still works on an archived copy months
later.

**Retrospective checks need retrospective references.** M19 — rows stamped with
a future Friday — cannot be caught today by comparing against today, because
those dates are now in the past. The same load-stamp idea fixes it; that
comparison is not built yet.

## Still out of reach

Four families, named rather than quietly omitted:

- **Derived-column correctness** (H5, H9, L13, L14): a five-year average
  computed over two years, or with future leakage. Detecting it means
  recomputing it, which means knowing the definition.
- **Cross-column semantics** (C4, H13): a deviation whose sign inverts with the
  sign of its own baseline; two columns swapped so "standardized" holds the raw
  anomaly. Row-by-row correct, jointly wrong.
- **Vintage comparison** (C3, M7, M17, L8, L12): a column silently dropped, a
  history truncated, a preliminary row overwriting an official one. All need a
  previous version. The manifest is the beginning of this and does not finish it.
- **Semantic geography** (M3): a point-of-interest that is an ocean cell.

## Reproducing this

```
git archive ac80af0^ data/ | tar -x -C /tmp/prefix
assay audit /tmp/prefix/data/livestock_storage.csv
```

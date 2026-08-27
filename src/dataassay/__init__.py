"""dataassay — audit a tabular dataset.

An assay characterizes a sample before it makes any claim about it. This tool
works in that order too: establish what kind of thing each column is, run only
the checks that property makes valid, and report what could NOT be checked as
plainly as what failed.
"""

__version__ = "0.4.1"

__all__ = ["__version__"]

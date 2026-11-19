## Metric
- Signal: promql: sum(increase(nightly_job_failures_total[7d]))
- Baseline: 42 on 2026-11-02
- Target: 10
- Channel: C0123456789
- Parallel: 2

## Context

The nightly import job fails about six times a day, and each failure pages whoever is
on call. Most failures are timeouts against the upstream API and retries that start over
from the beginning. This goal brings weekly failures down to 10.

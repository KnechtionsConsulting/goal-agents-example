Retry upstream API timeouts in the nightly import from the last completed page instead of
from the first page.

**Why:** most failures are a timeout late in the run, and the retry repeats every earlier
page, which makes another timeout likely.

**Change:** keep the last completed page in the job's checkpoint and resume from it in
`ImportJob.retry`.

## Success signal
- Signal: promql: sum(increase(nightly_job_failures_total[7d]))
- Expect: < 30
- Check: 7d

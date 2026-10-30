# Running the scheduler unattended

`orvex schedule <project>` keeps a project's models as fresh as they declare. It is meant to run
for days under a service manager (systemd, Docker, Kubernetes). This page says how it stops, how
long to let it stop, and how to see what it is doing.

## How it stops

`SIGTERM` is what service managers send:

- **While waiting between runs**, the scheduler stops within a second. While it is checking the
  project or reading the run records, it stops once that step ends (connecting to a slow source
  and validating against it can take tens of seconds). No run or lake maintenance is decided
  after a stop. One short window remains: a stop that arrives after the scheduler has decided a
  run, but before the run is admitted to the lake, still lets that run start; it then gets the
  grace period, like any run in progress.
- **During a run**, the run may finish for the stop grace period (`--stop-grace`, 60 seconds by
  default, from 0 to 86400). After that it is cancelled: nothing it wrote is kept, and the run records say it was
  cancelled. The next start runs it again.
- **During lake maintenance**, maintenance finishes; it is not cancelled.

Ctrl+C (`SIGINT`) does the same.

When it has stopped, it prints `stopped (SIGTERM)` (or `SIGINT`) and exits with code 0.

If the process is killed (`SIGKILL`, a crash, a power cut), nothing half-written is kept either:
the lake keeps only committed transactions, and the operating system releases the scheduler's
lock, so a new scheduler can start at once.

## Give the service manager a longer stop timeout

A service manager waits a fixed time after `SIGTERM`, then kills the process. **That wait must be
longer than the stop grace period plus the time to cancel and roll back** — allow at least
`--stop-grace` + 60 seconds. With a shorter wait, a run in progress is killed instead of
finishing or being cancelled cleanly: nothing is corrupted, but the run's work is lost without
a cancellation being recorded.

| Service manager | Setting | Default | With the default grace (60 s) set |
|---|---|---|---|
| systemd | `TimeoutStopSec=` in the unit | 90 s | `TimeoutStopSec=120` |
| Docker | `docker stop -t <seconds>`, `stop_grace_period` in Compose | **10 s** | `stop_grace_period: 2m` |
| Kubernetes | `terminationGracePeriodSeconds` in the pod | 30 s | `terminationGracePeriodSeconds: 120` |

**Docker's 10-second default kills a run in progress**: always set `stop_grace_period` (or
`docker stop -t`). Start the command in exec form (`CMD ["orvex", "schedule", "/project"]`), not
through a shell, so the signal reaches the scheduler.

A systemd unit, for example:

```ini
[Service]
ExecStart=/opt/orvex/bin/orvex schedule /srv/projects/sales
TimeoutStopSec=120
Restart=on-failure
```

To let long runs finish instead, raise both together: `--stop-grace 600` and
`TimeoutStopSec=660`.

## One scheduler per project

A second scheduler on the same project exits at once with `SCHEDULER_ALREADY_RUNNING` and the
process id of the one already running. Other commands (`run`, `maintain`) can still be used
between the scheduler's runs; while a run holds the project they exit with `PROJECT_LOCKED`, and
the scheduler waits while another command holds it.

## See what it is doing

`orvex runs <project>` starts with one line about the scheduler, read from its heartbeat
(`.orvex/scheduler.json`, renewed every minute, during runs too):

| Line | Meaning |
|---|---|
| `scheduler: running since …, last decision at … (…), next at …` | Alive; what it last decided and when it looks again |
| `scheduler: stopped at … (SIGTERM)` | Stopped cleanly, and why |
| `scheduler: not responding (process N, last heard at …)` | No heartbeat for 3 minutes: the process was killed or is frozen. A run or check that hangs inside the process still shows `running`, since the heartbeat is renewed during it; look at the last decision's time |
| `scheduler: has not run` | No scheduler has run on this project |
| `scheduler: unknown (…)` | The heartbeat file exists but cannot be read |

The last decision is the last line the scheduler printed about a run, maintenance or a stop, or
the first line of the problems that keep it from running models (`waiting: another process holds
the project`, a project that does not check, run records it cannot read), at the time they were
first printed. The other problem lines are in the scheduler's output.

The heartbeat holds times and lines the scheduler also prints, nothing more: no secret reaches it
that would not reach the output.

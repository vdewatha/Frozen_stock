# Memory freezes: 2026-11-11

**Question.** The deployed app froze three times on 2026-11-11. Why, and what stops it?

**Answer: the VM ran out of memory each time.** The readiness snapshot was being built in two processes at once, and the larger build was running at all three freezes. This change builds it once, hands memory back after long tasks, and makes the memory guard act in time. It does not yet make the build itself cheaper, because the day's log does not say which check inside it is the expensive one. The build now records that.

This corrects my conclusion earlier the same day that the Replit platform was the likely cause. I had read memory after a reboot, when it was low, not before the freeze.

Code: `app/services/readiness.py`, `app/services/deployment_monitor.py`, `app/api/routes.py`, `app/core/memory.py`, `app/tasks/celery_app.py`, `app/services/cost_probes.py`, `artifacts/api-server/src/resource-guard.ts`, `artifacts/api-server/src/production-server.ts`.

## What the log shows

Source: the deployment log for 2026-11-11, 00:00 to 16:47 ET, 47,091 lines.

| Froze (ET) | Memory in the last reading before | Largest process | Back up | Down |
|---|---|---|---|---|
| 06:10:04 | 7,414 of 7,943 MiB used, 529 free | paper-core 3,101 MiB | 09:21, a publish | 3 h 12 min |
| 11:40:48 | 7,625 used ten minutes earlier, 318 free | paper-core 3,147 MiB | 12:01, by itself | 20 min |
| 14:35:01 | 7,759 used, 184 free | paper-core 3,008 MiB | 15:20, by itself | 45 min |

* **Each freeze ended the same way.** The gateway logged `event loop stalled for 1461s` (then 744s, 2662s), killed its children, and the process exited with code 137. After the first, the platform logged a crash loop and waited for a publish.
* **The VM has no swap, and the app rests at 5.4 to 5.6 GiB of 7.9.** paper-core held 1.6 to 1.9 GiB at rest and paper-fast 1.2 to 1.5 GiB. Both start near 0.35 GiB after a boot.
* **The readiness snapshot is the spike, in two places.**
  * The API rebuilt it about every five minutes: 148 builds, 109 seconds each on average, growing the API process by 0.4 to 1.3 GiB.
  * The worker's `deployment_monitor_job` rebuilt it every fifteen minutes: about 130 seconds, growing paper-core by about 1.4 GiB. That job had started 47, 102 and 99 seconds before the three freezes and never finished.
* **The memory guard never acted.** It needs two low readings in a row, 10 seconds apart. It logged 13 first readings that day and never a second: the memory either recovered or the VM stalled first.

Trading decisions never read the readiness snapshot. Each order validates its own symbol's data when it is placed. The snapshot feeds the dashboard, the deployment monitor's alerts and a few operator actions.

## What changed

* **One build.** The API publishes each snapshot it builds to Redis. The deployment monitor reuses it when it is under 8 minutes old, and records `readiness_source` (`shared` or `computed`).
  * **One case still builds fresh:** the shared snapshot is blocked while the regular session is open. A block then holds orders and raises a critical alert, so it has to be current evidence.
  * **If Redis or the API is down,** the monitor builds its own, as before.
* **Each check is timed.** The snapshot carries `timing`: seconds and memory growth per check. The refresh log line names the slowest check and the one that grew the process most.
* **Memory goes back to the kernel.** After any task longer than 5 seconds, and after each readiness build, the process trims its freed heap (glibc `malloc_trim`, at most every 30 seconds). The Python processes also run with two malloc arenas instead of one per thread.
* **The memory guard acts on the first reading below an emergency floor** of 7% available (about 560 MiB), and samples every 5 seconds instead of 10. The two-reading rule stays for the 8% line. It still stops only the research worker, which its supervisor restarts.
* **The cost probe can sell again.** The QQQ probe of 11-10 was refused 51 times with "Sell would create a short position": the exit re-arm had put an OCO on the probe's share before it learned to skip probes. When the whole position is the probe's share, the probe now replaces those legs the way the trading cycle does. A larger position is left alone, because the extra shares and their stops belong to a strategy.

## Not changed, and why

* **The build itself is not cheaper yet.** I expected it to load full price histories. It loads 260 daily rows for each of 55 symbols, which is small, and nothing else in it looks large on reading. The new timing will name the expensive check after the next publish, and that is the next fix.
* **The overnight minute-bar import still runs every minute.** It is the post-close repair pass, and it also keeps the feed's "verified in the last five minutes" evidence fresh. It runs in its own 224 MiB process and was not involved in any freeze. Slowing it risks the feed status flapping overnight.
* **The API still rebuilds every five minutes.** A slower cadence would make the dashboard's readiness older. With the worker's copy gone, the API's build is the only spike left.

## What I could not verify

* **The trim and the arena limit.** `malloc_trim` and `MALLOC_ARENA_MAX` are glibc behaviour, and my machine is not Linux. The tests cover that they are called and never raise. Whether resting memory falls on the VM will show in the `[resources]` lines after the next publish.
* **How often the guard's emergency floor will fire.** On 11-11 it would have fired about ten times. With the worker's build gone it should be rare.

## How to check after a publish

* `[resources]` lines: paper-core should no longer jump by 1.4 GiB every fifteen minutes, and resting memory should stay well under 5 GiB.
* `readiness refresh:` lines now end with `slowest <check> Ns, largest growth <check> +N MiB`.
* The deployment monitor's snapshot shows `readiness_source: shared`.
* The cost probe's next tick sells QQQ, and a round trip is recorded.

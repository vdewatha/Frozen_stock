# Continuous candidate training

The user requested 24/7 operation with one daily agent review. The previous
September 5 11 a.m. stop instruction is superseded: the daily review does not
stop the experiment.

## Separate responsibilities

- `trading-paper-kraken`: simulated execution against public Kraken prices.
- `trading-paper-research`: collects completed hourly candles, evaluates the
  frozen approved model, and handles bounded paper execution every 60 seconds.
- `trading-model-training`: checks for a new completed hourly candle every 60
  seconds and trains logistic-regression and random-forest candidates once per
  new cutoff. It waits between candles, rather than repeatedly training on the
  same data. It has no network or provider credentials and only a read-only
  single-file mount of the research database.

Training uses the existing chronological, purged train/holdout pipeline and
records comparison with a training-prevalence baseline. Candidate files are
immutable and versioned under the private ignored `.paper-training/` directory.
The trainer's own SQLite status database records attempts and completed cutoffs;
a process lock prevents concurrent workers sharing this output directory.
Failed candidate training has a bounded retry count. Data validation failures
prevent training until valid fresh history is available.

These are research candidates, not automatic replacements for binding 1.
Overlapping rolling holdouts are not independent evidence, and inspecting many
candidates can produce selection bias. No candidate is registered, approved,
promoted, or used for orders by the trainer. Candidate training does not erase
the active paper model's results. New trading-model approval remains separate.
The existing legacy feature-scaling limitation also remains; automated training
does not fix it or imply improved performance.

## Operations

Build and launch only when no trainer container already exists:

```sh
docker build -f backend/Dockerfile.paper -t trading-model-training:20260905 backend
python backend/scripts/start_continuous_training.py --database .paper-venue/research.sqlite --output .paper-training
```

The launcher refuses duplicate containers and non-private output directories.
It mounts only the source database, never the directory containing credentials.
The source currently uses SQLite DELETE journal mode. A WAL migration requires
revisiting the mount design; the launcher refuses WAL rather than changing it.

Read-only checks:

```sh
docker logs --tail 5 trading-model-training
docker exec trading-paper-research python scripts/report_paper_research.py --approval-id 1 --binding-id 1
```

All services use restart-unless-stopped. The trainer is limited to one CPU and
1 GB RAM, with a read-only root filesystem and bounded Docker logs. Candidate
artifacts are retained, so disk usage must be checked during daily reviews.
No automatic deletion of training evidence is configured.

The daily agent review runs at 11 a.m. America/New_York and summarizes data,
training health, candidate metrics, paper wins and losses, and operational
issues. It does not change models, clear kill switches, or enable live trading.
The computer must stay awake with Docker running for continuous processing;
Codex must be available for its daily scheduled review. This is local operation,
not an always-on cloud deployment or a guarantee of uninterrupted availability.

To stop intentionally, preserve all containers and state:

```sh
docker stop trading-model-training
docker stop trading-paper-research
docker stop trading-paper-kraken
```

Also pause the daily review if stopping the experiment permanently. Never reset
balances, delete losses, or automatically restart an operator-halted experiment.

## Deployment verification — September 5, 2026

All three services were confirmed running. The trainer published candidate run
`07cffa734156cebaf85dd7447c65febbc42f9bd94633ae8e65ac2f7817777818`
at 03:25 UTC from the latest completed 02:00 UTC candle. Logistic Brier was
0.252055 and random forest 0.262574 versus baseline 0.249513; neither improved
on the baseline. The active binding, paper account, and risk limits were unchanged.
Docker inspection confirmed no network, read-only source, read-only root, and
restart-unless-stopped. Runtime publication verified output permissions.

207 backend tests passed, including ten focused trainer/launcher tests. An
independent reviewer checked isolation, bounded retries, crash recovery, and
artifact integrity; the launcher argument mismatch and completed-artifact
verification findings were fixed before deployment. A preexisting test fixture
was corrected to use UTC, matching the production timestamp validator.

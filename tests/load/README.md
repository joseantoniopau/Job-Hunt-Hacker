# Load tests

Locust-driven load test scaffold for the Job Hunt Hacker API.

## Install

```bash
pip install locust
```

## Run

Start the API locally first (`./run.sh` or `uvicorn backend.app.main:app --port 8731`),
then in a separate terminal:

```bash
locust -f tests/load/locustfile.py \
    --host=http://127.0.0.1:8731 \
    -u 50 \
    -r 5 \
    -t 2m
```

Flags:

- `-u 50` — peak concurrent users.
- `-r 5` — ramp-up rate (users spawned per second).
- `-t 2m` — total test duration.

Open <http://127.0.0.1:8089> for the live Locust web UI, or add `--headless`
to print final stats to stdout.

## User mix

| Class           | Weight | Pattern                                                                                  |
|-----------------|-------:|------------------------------------------------------------------------------------------|
| `BrowsingUser`  | 4      | Read-mostly: `/api/health`, `/api/stats`, `/api/jobs`, `/api/applications/board`, etc.   |
| `SearchUser`    | 2      | POST `/api/search` every 30s with realistic queries.                                     |
| `TailorUser`    | 1      | POST `/api/resume/tailor` every 60s (LLM-bound path).                                    |

## Tips

- Run with `--headless --html report.html` in CI to capture a self-contained report.
- Add `--stop-timeout 10` so in-flight requests get a chance to complete.
- Use `LOCUST_USERS`, `LOCUST_SPAWN_RATE`, `LOCUST_RUN_TIME` env vars if you'd
  rather avoid CLI flags.

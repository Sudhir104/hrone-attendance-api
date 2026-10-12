# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and analytics built on MongoDB aggregation pipelines. All code is in `app/main.py`.

## Run

Needs Python 3.11+ and MongoDB 6.0+ (`$densify` and `$setWindowFields` need 5.1+).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows (Git Bash): source .venv/Scripts/activate
python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
python -m uvicorn app.main:app --port 8000
```

Indexes are created at startup (idempotent). Interactive docs: `http://localhost:8000/docs`.

## Endpoints

- `GET /health`, `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}` (correction + history entry)
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## How it was checked

- Rules R1-R5 (late, overtime, work hours rounding, half day, overnight shift) were checked with small unit examples.
- All endpoints were run by hand against a local MongoDB with the 9 sample documents. Five parallel punch-ins for the same day gave one 201 and four 409.

## Not verified / known gaps

- Not tested on a large dataset: ties on the leaderboard, weekend records, mid-month joiners and employees with no logs are covered by the code but not by real test data.
- `moving_avg_7d` in the trend is the mean of the unrounded daily rates, rounded half-up at the end.
- Present records dated before an employee's `joined_on` are still counted.
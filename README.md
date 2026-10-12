# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+; `$setWindowFields` and `$densify` need 5.1+).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Indexes are created at startup. All code is in `app/main.py`.

# REVIEW.md

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Naive datetimes come back; `datetime.now()` / `fromtimestamp()` use the server's local zone, not UTC/IST | Different results on a UTC server vs a laptop; naive vs aware `TypeError` | `tz_aware=True`, everything in UTC, convert to IST only for dates and shifts |
| 2 | `compute_late_minutes` | `int(minutes) > 10` drops seconds, so 09:40:01 gives 0 instead of 10; start is built from the punch-in's own (naive) day; overnight shifts ignored | punch-in 09:40:01 on a 09:30 shift | Work in whole seconds, late only if `> 600 s`, floor to minutes since shift start, shift start taken on the attendance date (R1/R2) |
| 3 | `compute_work_hours` | `round()` is banker's rounding on a float; seconds are not truncated | 0.005 h rounds to 0.0 | `Decimal` with `ROUND_HALF_UP` (R4) |
| 4 | `compute_overtime` | No 30-minute minimum; always same-day shift end so overnight is wrong; aware minus naive datetime raises `TypeError` | punch-out 18:40 gives 10 instead of 0 | Shift end on the right day (+1 day if overnight), >= 30 rule (R3) |
| 5 | `/health` | Always "ok", never talks to MongoDB | Stop Mongo, still 200 | `ping`, 503 on failure |
| 6 | `create_employee` | check-then-insert race, no unique index; no validation (emp_code format, shift_start == shift_end, bad joined_on); `created_at` is a naive datetime, not epoch ms | Two parallel creates both get 201 | Unique index on `emp_code`, `DuplicateKeyError` -> 409, pydantic validation, ms output |
| 7 | `list_employees` | `skip = page * page_size` skips the whole first page; `total` ignores the filter; no sort; page / page_size unbounded | `page=1` returns page 2 | `(page-1)*page_size`, `count_documents(filter)`, sort by `emp_code`, `ge/le` limits |
| 8 | `punch_in` | Unknown employee -> `emp[...]` on `None` = 500; any `status` accepted; `punched_at` treated as local time, `0` treated as missing | `emp_code` that doesn't exist | 404, `Literal` status, strict epoch-ms validation, `is not None` |
| 9 | `punch_in` | check-then-insert race, no unique index on `(emp_code, date)` | Parallel punch-ins both succeed | Unique index, `DuplicateKeyError` -> 409 |
| 10 | `punch_in` | Date taken from local date, no overnight rule (R1), microseconds kept | 03:00 punch-in on a 22:00-06:00 shift gets today's date | `attendance_date()` in IST with the overnight rule, truncate to seconds |
| 11 | `punch_in` response | Leaks `_id` as `id`, returns datetimes instead of epoch ms | Compare against the OpenAPI schema | `record_out()` |
| 12 | `list_attendance` | Loads the whole collection and sorts in Python; no tie-break on `emp_code`; no validation | 100k records: slow and memory heavy | Query + `sort/skip/limit` in MongoDB, indexes, validation (`date_from > date_to` -> 422) |
| 13 | startup | No indexes at all | `explain` shows COLLSCAN | `ensure_indexes()` at startup (idempotent) |
| 14 | config | Hard-coded default is fine, but the client had no timeout, so a dead DB hangs requests | Stop Mongo and call anything | `serverSelectionTimeoutMS`, `PyMongoError` -> 503 |

Looked at and left alone: `load_dotenv()` does not override real environment variables, so it already matches the contract. `doc.pop("_id")` after insert is fine.

# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` on logs: it stops duplicate punch-ins and serves per-employee and join lookups. `(date, emp_code)` serves the sorted list and month ranges. `(status, date, emp_code)` serves status filters. `(emp_code, punch_in)` finds the open record for punch-out. On employees: unique `emp_code`, plus `(department, joined_on)` for headcount. I rejected an index on `late_minutes`; month ranges are already narrow.
2. **Punch-in race.** Both requests build the document and call `insert_one`. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-write step.
3. **Ties.** The leaderboard uses `$rank`, so tied employees share a rank. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined by month end) and `$lookup`s the logs, so people with zero logs still count.
5. **100x data.** I would pre-aggregate monthly numbers per employee instead of scanning daily logs on every call.

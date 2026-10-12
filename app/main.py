"""Employee Attendance & Analytics API (FastAPI + MongoDB). Run: uvicorn app.main:app --port 8000"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override real environment variables

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRES = ["PRESENT", "WFH", "ON_DUTY"]
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: Sunday = 1
DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
SORT = {"date": -1, "emp_code": 1}

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Ms = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_RE)]

_indexed = False


def ensure_indexes():
    global _indexed
    if _indexed:
        return
    e, a = db.employees, db.attendance_logs
    e.create_index("emp_code", unique=True)
    e.create_index([("department", 1), ("emp_code", 1)])
    e.create_index([("department", 1), ("joined_on", 1)])
    e.create_index("joined_on")
    a.create_index([("emp_code", 1), ("date", 1)], unique=True)
    a.create_index([("date", -1), ("emp_code", 1)])
    a.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    a.create_index([("emp_code", 1), ("punch_in", -1)])
    _indexed = True


@asynccontextmanager
async def lifespan(_):
    try:
        ensure_indexes()
    except PyMongoError as err:  # /health retries and reports 503 until Mongo is back
        print("index creation failed:", err)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error(_: Request, err: PyMongoError):
    return JSONResponse({"detail": "database error"}, status_code=503)


# ------------------------------------------------------------------ helpers
def to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms):  # whole seconds only (R1)
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def now():
    return datetime.now(UTC).replace(microsecond=0)


def now_ms():
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def half_up(x, nd):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-nd), ROUND_HALF_UP))


def parse_date(s):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        raise HTTPException(422, f"invalid date: {s}")
    try:
        return Date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid date: {s}")


def at_ist(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def overnight(emp):
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in, emp):  # R1
    local = punch_in.astimezone(IST)
    if overnight(emp) and local < at_ist(local.date(), emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def calc_late(punch_in, emp, d):  # R2
    secs = int((punch_in - at_ist(d, emp["shift_start"])).total_seconds())
    return secs // 60 if secs > 600 else 0


def calc_overtime(punch_out, emp, d):  # R3
    end = at_ist(d, emp["shift_end"]) + (timedelta(days=1) if overnight(emp) else timedelta())
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def calc_hours(punch_in, punch_out):  # R4
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / 3600).quantize(Decimal("0.01"), ROUND_HALF_UP))


def derived(status, pi, po, emp, d):
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in PRES and pi is not None:
        out["late_minutes"] = calc_late(pi, emp, d)
        if po is not None:
            out["work_hours"] = calc_hours(pi, po)
            out["overtime_minutes"] = calc_overtime(po, emp, d)
            out["half_day"] = out["work_hours"] < 4.5  # R5, on the rounded value
    return out


def emp_out(e):
    keys = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{k: e[k] for k in keys}, "created_at": to_ms(e.get("created_at"))}


def record_out(r):
    def conv(v):
        return to_ms(v) if isinstance(v, datetime) else v

    history = [
        {"at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"],
         "changes": {k: {"from": conv(c["from"]), "to": conv(c["to"])} for k, c in h["changes"].items()}}
        for h in r.get("history") or []
    ]
    return {
        "emp_code": r["emp_code"], "date": r["date"], "status": r["status"],
        "punch_in": to_ms(r.get("punch_in")), "punch_out": to_ms(r.get("punch_out")),
        "work_hours": r.get("work_hours"), "late_minutes": r.get("late_minutes") or 0,
        "overtime_minutes": r.get("overtime_minutes") or 0, "half_day": bool(r.get("half_day")),
        "history": history,
    }


def get_emp(code):
    emp = db.employees.find_one({"emp_code": code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def month_bounds(month):
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def count_weekdays(first, last):
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(i)).weekday() < 5)


# ------------------------------------------------------------------ models
def parse_date_value(v):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
        raise ValueError("expected YYYY-MM-DD")
    Date.fromisoformat(v)
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_is_date(cls, v):
        return parse_date_value(v)

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[Ms] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[Ms] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[Ms] = None
    punch_out: Optional[Ms] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def no_nulls(cls, data):
        if isinstance(data, dict):
            for k in ("status", "punch_in", "punch_out"):
                if k in data and data[k] is None:
                    raise ValueError(f"{k} must not be null")
        return data


# ------------------------------------------------------------------ system / employees
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": now_ms()}
    try:
        db.employees.insert_one(doc)  # unique index on emp_code decides races
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    cur = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(e) for e in cur], "total": db.employees.count_documents(q),
            "page": page, "page_size": page_size}


# ------------------------------------------------------------------ attendance
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_emp(body.emp_code)
    t = from_ms(body.punched_at) if body.punched_at is not None else now()
    d = attendance_date(t, emp)
    doc = {"emp_code": body.emp_code, "date": d.isoformat(), "status": body.status, "punch_in": t,
           "punch_out": None, **derived(body.status, t, None, emp, d), "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index decides races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_emp(body.emp_code)
    t = from_ms(body.punched_at) if body.punched_at is not None else now()
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": t}}, sort=[("punch_in", -1)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pi = rec["punch_in"]
    if t <= pi or t - pi > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    fields = {"punch_out": t, **derived(rec["status"], pi, t, emp, Date.fromisoformat(rec["date"]))}
    # only matches while the record is still open, so one of two parallel punch-outs wins
    upd = db.attendance_logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None},
        {"$set": fields}, return_document=ReturnDocument.AFTER)
    if not upd:
        raise HTTPException(409, "already punched out")
    return record_out(upd)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Path(pattern=DATE_RE)], body: RegularizeIn):
    parse_date(date)
    emp = get_emp(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "no record for that date")

    status = body.status or old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{status} cannot have punch times")
        pi = po = None
    else:
        pi = from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        po = from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if pi is None:
            raise HTTPException(422, "punch_in is required for this status")
        if attendance_date(pi, emp).isoformat() != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if po is not None and not (pi < po <= pi + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": status, "punch_in": pi, "punch_out": po,
           **derived(status, pi, po, emp, Date.fromisoformat(date))}
    prev = {"status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
            "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes") or 0,
            "overtime_minutes": old.get("overtime_minutes") or 0, "half_day": bool(old.get("half_day"))}
    changes = {k: {"from": prev[k], "to": v} for k, v in new.items() if prev[k] != v}
    if not changes:
        raise HTTPException(422, "nothing to change")

    entry = {"at": now_ms(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # optimistic lock: the write only applies if the record is exactly as we read it
    flt = {"emp_code": emp_code, "date": date, "status": prev["status"],
           "punch_in": prev["punch_in"], "punch_out": prev["punch_out"],
           "history": {"$size": len(old["history"])} if old.get("history") is not None else {"$exists": False}}
    upd = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not upd:
        raise HTTPException(409, "record changed meanwhile, please retry")
    return record_out(upd)


def attendance_query(emp_code, date_from, date_to, status):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    lo = parse_date(date_from) if date_from else None
    hi = parse_date(date_to) if date_to else None
    if lo and hi and lo > hi:
        raise HTTPException(422, "date_from is after date_to")
    rng = {**({"$gte": date_from} if date_from else {}), **({"$lte": date_to} if date_to else {})}
    if rng:
        q["date"] = rng
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[Status] = None, page: Page = 1, page_size: PageSize = 20):
    q = attendance_query(emp_code, date_from, date_to, status)
    cur = db.attendance_logs.find(q).sort(list(SORT.items())).skip((page - 1) * page_size).limit(page_size)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    return {"items": [record_out(r) for r in cur], "total": total, "page": page, "page_size": page_size}


# ------------------------------------------------------------------ analytics
def month_group():
    """One $group over a set of attendance docs; used per employee and per department."""
    present = {"$in": ["$status", PRES]}
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, WEEKDAYS]}
    credit = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}
    has_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}
    count = lambda cond: {"$sum": {"$cond": [cond, 1, 0]}}
    return {"$group": {
        "_id": None,
        "present": {"$sum": {"$cond": [{"$and": [present, weekday]}, credit, 0]}},
        "late_count": count({"$gt": ["$late_minutes", 0]}),
        "late_min": {"$sum": "$late_minutes"},
        "ot_min": {"$sum": "$overtime_minutes"},
        "leave": count({"$eq": ["$status", "LEAVE"]}),
        "on_duty": count({"$eq": ["$status", "ON_DUTY"]}),
        # work_hours summed as integer 1/10000ths so the average can be rounded exactly in Python
        "wh_sum": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 10000]}, 0]}}, 0]}},
        "wh_n": count(has_hours),
    }}


def monthly_pipeline(emp_code, month):
    s, e = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": s, "$lte": e}}}, month_group()]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_emp(emp_code)
    g = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month))), {})
    s, e = month_bounds(month)
    working = count_weekdays(max(Date.fromisoformat(s), Date.fromisoformat(emp["joined_on"])), Date.fromisoformat(e))
    present = float(g.get("present", 0))
    pct = half_up(Decimal(str(present)) / working * 100, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
            "leave_days": g.get("leave", 0), "late_count": g.get("late_count", 0),
            "total_late_minutes": int(g.get("late_min", 0)), "total_overtime_minutes": int(g.get("ot_min", 0)),
            "attendance_pct": pct}


def summary_pipeline(month, department):
    s, e = month_bounds(month)
    match = {"joined_on": {"$lte": e}}  # R9: joined by month end, with or without logs
    if department is not None:
        match["department"] = department
    pick = lambda name: {"$sum": {"$ifNull": [f"$g.{name}", 0]}}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": s, "$lte": e}}}, month_group()], "as": "g"}},
        {"$unwind": {"path": "$g", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{
            k: pick(k) for k in ("present", "late_count", "late_min", "leave", "on_duty", "wh_sum", "wh_n")}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(summary_pipeline(month, department)):
        avg = None
        if r["wh_n"]:
            avg = float((Decimal(r["wh_sum"]) / r["wh_n"] / 10000).quantize(Decimal("0.01"), ROUND_HALF_UP))
        items.append({"department": r["_id"], "headcount": r["headcount"], "present_days": float(r["present"]),
                      "avg_work_hours": avg, "late_count": r["late_count"], "total_late_minutes": int(r["late_min"]),
                      "leave_count": r["leave"], "on_duty_count": r["on_duty"]})
    return {"month": month, "items": items}


def leaderboard_pipeline(month, limit, department):
    s, e = month_bounds(month)
    pipe = [
        {"$match": {"date": {"$gte": s, "$lte": e}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "cnt": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees drop out here
    ]
    if department is not None:
        pipe.append({"$match": {"e.department": department}})
    pipe += [
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},  # 1, 2, 2, 4
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total": -1, "_id": 1}},
    ]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": r["rank"], "emp_code": r["_id"], "name": r["e"]["name"], "department": r["e"]["department"],
         "total_late_minutes": int(r["total"]), "late_count": r["cnt"]} for r in rows]}


def check_range(frm, to):
    a, b = parse_date(frm), parse_date(to)
    if b < a or (b - a).days + 1 > 92:
        raise HTTPException(422, "to must be on/after from, and the range at most 92 days")
    return a, b


def trend_pipeline(department, frm, to):
    a, b = check_range(frm, to)
    lo = datetime(a.year, a.month, a.day, tzinfo=UTC)
    hi = datetime(b.year, b.month, b.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": frm, "$lte": to}}},
                                  {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}],
                     "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},  # keeps the pipeline non-empty
        {"$group": {"_id": "$logs.date",
                    "present": {"$sum": {"$cond": [{"$in": ["$logs.status", PRES]},
                                                   {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                    "late": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}}}},
        {"$set": {"d": {"$ifNull": [{"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}, lo]}}},
        {"$group": {"_id": "$d", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$set": {"d": "$_id"}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {"date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                  "work": {"$in": [{"$dayOfWeek": "$d"}, WEEKDAYS]},
                  "present": {"$ifNull": ["$present", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"},
                     "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                                  {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"rate": {"$cond": [{"$and": ["$work", {"$gt": ["$headcount", 0]}]},
                                     {"$divide": ["$present", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {"avg7": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: Annotated[str, Query(alias="from", pattern=DATE_RE)],
                     to: Annotated[str, Query(pattern=DATE_RE)]):
    pipe = trend_pipeline(department, frm, to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    r4 = lambda x: None if x is None else half_up(x, 4)
    return {"department": department, "items": [
        {"date": r["date"], "is_working_day": r["work"], "headcount": r["headcount"], "present_count": r["present"],
         "late_count": r["late"], "attendance_rate": r4(r["rate"]), "moving_avg_7d": r4(r["avg7"])}
        for r in db.employees.aggregate(pipe)]}


# ------------------------------------------------------------------ explain
@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
            emp_code: Optional[str] = None, month: Optional[Month] = None, department: Optional[str] = None,
            limit: Annotated[int, Query(ge=1, le=50)] = 10, date_from: Optional[str] = None,
            date_to: Optional[str] = None, status: Optional[Status] = None,
            frm: Annotated[Optional[str], Query(alias="from", pattern=DATE_RE)] = None,
            to: Annotated[Optional[str], Query(pattern=DATE_RE)] = None, page: Page = 1, page_size: PageSize = 20):
    def need(**kw):
        missing = [k for k, v in kw.items() if v is None]
        if missing:
            raise HTTPException(422, f"missing parameters: {', '.join(missing)}")

    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": attendance_query(emp_code, date_from, date_to, status), "sort": SORT,
               "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            coll, pipe = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month=month)
            coll, pipe = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            coll, pipe = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            need(department=department, **{"from": frm, "to": to})
            coll, pipe = "employees", trend_pipeline(department, frm, to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}

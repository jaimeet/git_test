import base64
import csv
import io
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db
from .parser import ParsedSheet, ParseError, get_parser

APP_DIR = Path(__file__).parent
log = logging.getLogger("hisab")
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Hisab", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


def money(paise) -> str:
    value = db.from_paise(paise)
    if value is None:
        return ""
    return f"{config.CURRENCY}{value:,.2f}"


templates.env.filters["money"] = money
templates.env.globals["currency"] = config.CURRENCY
templates.env.filters["rupees"] = lambda p: "" if p is None else f"{db.from_paise(p):.2f}"


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not config.APP_PASSWORD or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user, pw = "", ""
        if secrets.compare_digest(user, config.APP_USERNAME) and secrets.compare_digest(pw, config.APP_PASSWORD):
            return await call_next(request)
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="hisab"'})


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- pages ----------


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.session() as conn:
        sheets = db.list_sheets(conn)
    return templates.TemplateResponse(request, "index.html", {"sheets": sheets})


@app.post("/upload")
async def upload(photo: UploadFile = File(...)):
    if photo.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, f"Unsupported file type {photo.content_type}. Upload a JPEG, PNG or WebP photo.")
    content = await photo.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Photo is larger than {config.MAX_UPLOAD_MB} MB.")

    suffix = Path(photo.filename or "").suffix.lower() or ".jpg"
    name = f"{date.today().isoformat()}-{uuid.uuid4().hex[:10]}{suffix}"
    (config.IMAGES_DIR / name).write_bytes(content)

    with db.session() as conn:
        sheet_id = db.create_sheet(conn, name)

    await run_in_threadpool(_parse_into_sheet, sheet_id, content)
    return RedirectResponse(f"/sheets/{sheet_id}", status_code=303)


def _parse_into_sheet(sheet_id: int, content: bytes) -> None:
    try:
        parsed = get_parser().parse(content)
    except Exception as exc:
        if not isinstance(exc, ParseError):
            log.exception("Unexpected error parsing sheet %s", sheet_id)
        message = str(exc) if isinstance(exc, ParseError) else f"Unexpected error: {exc}"
        with db.session() as conn:
            db.update_sheet_meta(conn, sheet_id, parse_error=message)
        return
    _store_parsed(sheet_id, parsed)


def _store_parsed(sheet_id: int, parsed: ParsedSheet) -> None:
    entries = [
        {
            "entry_date": e.entry_date or parsed.sheet_date,
            "description": e.description.strip() or (e.original_text or "").strip() or "(unreadable)",
            "party": e.party,
            "kind": e.kind,
            "amount_paise": db.to_paise(abs(e.amount)),
            "category": e.category,
            "confidence": e.confidence,
        }
        for e in parsed.entries
    ]
    with db.session() as conn:
        db.update_sheet_meta(
            conn,
            sheet_id,
            sheet_date=parsed.sheet_date,
            parse_error=None,
            raw_json=parsed.model_dump_json(),
            opening_balance_paise=db.to_paise(parsed.opening_balance),
            closing_balance_paise=db.to_paise(parsed.closing_balance),
            written_total_in_paise=db.to_paise(parsed.written_total_in),
            written_total_out_paise=db.to_paise(parsed.written_total_out),
            notes=parsed.notes,
        )
        db.replace_entries(conn, sheet_id, entries)


def _sheet_or_404(conn, sheet_id: int):
    sheet = db.get_sheet(conn, sheet_id)
    if sheet is None:
        raise HTTPException(404, "Sheet not found")
    return sheet


def _checks(sheet, entries) -> list[str]:
    """Cross-check parsed lines against totals written on the sheet."""
    warnings = []
    total_in = sum(e["amount_paise"] for e in entries if e["kind"] == "in")
    total_out = sum(e["amount_paise"] for e in entries if e["kind"] == "out")
    if sheet["written_total_in_paise"] is not None and sheet["written_total_in_paise"] != total_in:
        warnings.append(f"Money-in lines add up to {money(total_in)} but the sheet says {money(sheet['written_total_in_paise'])}.")
    if sheet["written_total_out_paise"] is not None and sheet["written_total_out_paise"] != total_out:
        warnings.append(f"Money-out lines add up to {money(total_out)} but the sheet says {money(sheet['written_total_out_paise'])}.")
    if sheet["opening_balance_paise"] is not None and sheet["closing_balance_paise"] is not None:
        expected = sheet["opening_balance_paise"] + total_in - total_out
        if expected != sheet["closing_balance_paise"]:
            warnings.append(
                f"Opening {money(sheet['opening_balance_paise'])} + in − out = {money(expected)}, "
                f"but the closing balance written is {money(sheet['closing_balance_paise'])}."
            )
    low = sum(1 for e in entries if e["confidence"] == "low")
    if low:
        warnings.append(f"{low} line(s) were hard to read — highlighted below.")
    return warnings


@app.get("/sheets/{sheet_id}", response_class=HTMLResponse)
def review(request: Request, sheet_id: int):
    with db.session() as conn:
        sheet = _sheet_or_404(conn, sheet_id)
        entries = db.get_entries(conn, sheet_id)
    total_in = sum(e["amount_paise"] for e in entries if e["kind"] == "in")
    total_out = sum(e["amount_paise"] for e in entries if e["kind"] == "out")
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "sheet": sheet,
            "entries": entries,
            "total_in": total_in,
            "total_out": total_out,
            "warnings": _checks(sheet, entries),
        },
    )


@app.post("/sheets/{sheet_id}")
async def save_sheet(request: Request, sheet_id: int):
    form = await request.form()
    descriptions = form.getlist("description")
    parties = form.getlist("party")
    kinds = form.getlist("kind")
    amounts = form.getlist("amount")
    dates = form.getlist("entry_date")
    categories = form.getlist("category")
    confidences = form.getlist("confidence")
    sheet_date = (form.get("sheet_date") or "").strip() or None

    entries, errors = [], []
    for i, desc in enumerate(descriptions):
        desc = desc.strip()
        amount_raw = amounts[i].strip() if i < len(amounts) else ""
        if not desc and not amount_raw:
            continue  # blank row
        try:
            amount = db.to_paise(amount_raw)
        except Exception:
            amount = None
        if amount is None or amount < 0:
            errors.append(f"Row {i + 1}: '{amount_raw}' is not a valid amount.")
            continue
        kind = kinds[i] if i < len(kinds) and kinds[i] in ("in", "out") else "out"
        entries.append(
            {
                "entry_date": (dates[i].strip() if i < len(dates) else "") or sheet_date,
                "description": desc or "(no description)",
                "party": (parties[i].strip() if i < len(parties) else "") or None,
                "kind": kind,
                "amount_paise": amount,
                "category": (categories[i].strip() if i < len(categories) else "") or None,
                "confidence": confidences[i] if i < len(confidences) and confidences[i] else None,
            }
        )
    if errors:
        raise HTTPException(400, " ".join(errors))

    action = form.get("action", "confirm")
    with db.session() as conn:
        _sheet_or_404(conn, sheet_id)
        meta = {
            "sheet_date": sheet_date,
            "notes": (form.get("notes") or "").strip() or None,
        }
        if action == "confirm":
            meta.update(status="confirmed", confirmed_at=db.now_iso())
        db.update_sheet_meta(conn, sheet_id, **meta)
        db.replace_entries(conn, sheet_id, entries)
    return RedirectResponse("/" if action == "confirm" else f"/sheets/{sheet_id}", status_code=303)


@app.post("/sheets/{sheet_id}/reparse")
async def reparse(sheet_id: int):
    with db.session() as conn:
        sheet = _sheet_or_404(conn, sheet_id)
        db.update_sheet_meta(conn, sheet_id, status="draft", confirmed_at=None)
    content = (config.IMAGES_DIR / sheet["image_path"]).read_bytes()
    await run_in_threadpool(_parse_into_sheet, sheet_id, content)
    return RedirectResponse(f"/sheets/{sheet_id}", status_code=303)


@app.post("/sheets/{sheet_id}/delete")
def delete(sheet_id: int):
    with db.session() as conn:
        sheet = db.delete_sheet(conn, sheet_id)
    if sheet is not None:
        (config.IMAGES_DIR / sheet["image_path"]).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)


@app.get("/images/{name}")
def image(name: str):
    path = (config.IMAGES_DIR / name).resolve()
    if path.parent != config.IMAGES_DIR.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# ---------- ledger ----------


def _ledger(date_from, date_to, q):
    with db.session() as conn:
        rows = db.query_entries(conn, date_from or None, date_to or None, q or None)
    total_in = sum(r["amount_paise"] for r in rows if r["kind"] == "in")
    total_out = sum(r["amount_paise"] for r in rows if r["kind"] == "out")
    return rows, total_in, total_out


@app.get("/entries", response_class=HTMLResponse)
def entries_page(request: Request, date_from: str = "", date_to: str = "", q: str = ""):
    rows, total_in, total_out = _ledger(date_from, date_to, q)
    by_category: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "out":
            key = r["category"] or "Uncategorised"
            by_category[key] = by_category.get(key, 0) + r["amount_paise"]
    return templates.TemplateResponse(
        request,
        "entries.html",
        {
            "rows": rows,
            "total_in": total_in,
            "total_out": total_out,
            "by_category": sorted(by_category.items(), key=lambda kv: -kv[1]),
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        },
    )


@app.get("/entries.csv")
def entries_csv(date_from: str = "", date_to: str = "", q: str = ""):
    rows, _, _ = _ledger(date_from, date_to, q)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "description", "party", "category", "in", "out", "sheet_id"])
    for r in rows:
        amt = f"{db.from_paise(r['amount_paise']):.2f}"
        w.writerow([
            r["entry_date"] or "", r["description"], r["party"] or "", r["category"] or "",
            amt if r["kind"] == "in" else "", amt if r["kind"] == "out" else "", r["sheet_id"],
        ])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="hisab.csv"'},
    )


@app.get("/api/entries")
def entries_json(date_from: str = "", date_to: str = "", q: str = ""):
    rows, total_in, total_out = _ledger(date_from, date_to, q)
    return JSONResponse({
        "total_in": str(db.from_paise(total_in)),
        "total_out": str(db.from_paise(total_out)),
        "entries": [
            {
                "id": r["id"], "sheet_id": r["sheet_id"], "date": r["entry_date"],
                "description": r["description"], "party": r["party"], "category": r["category"],
                "kind": r["kind"], "amount": str(db.from_paise(r["amount_paise"])),
            }
            for r in rows
        ],
    })

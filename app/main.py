import base64
import csv
import io
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, whatsapp
from .ingest import checks, mark_reading, money, parse_into_sheet, save_image
from .parser import describe_provider

APP_DIR = Path(__file__).parent
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Hisab", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["money"] = money
templates.env.globals["currency"] = config.CURRENCY
templates.env.filters["rupees"] = lambda p: "" if p is None else f"{db.from_paise(p):.2f}"


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not config.APP_PASSWORD or request.url.path in ("/healthz", "/whatsapp/webhook"):  # webhook has its own HMAC check
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
    return templates.TemplateResponse(request, "index.html", {"sheets": sheets, "provider": describe_provider()})


@app.post("/upload")
async def upload(background: BackgroundTasks, photo: UploadFile = File(...)):
    if photo.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, f"Unsupported file type {photo.content_type}. Upload a JPEG, PNG or WebP photo.")
    content = await photo.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Photo is larger than {config.MAX_UPLOAD_MB} MB.")

    name = save_image(content, Path(photo.filename or "").suffix.lower() or ".jpg")

    with db.session() as conn:
        sheet_id = db.create_sheet(conn, name)
    mark_reading(sheet_id)
    background.add_task(parse_into_sheet, sheet_id, content)  # the sheet page shows "Reading…" meanwhile
    return RedirectResponse(f"/sheets/{sheet_id}", status_code=303)


def _sheet_or_404(conn, sheet_id: int):
    sheet = db.get_sheet(conn, sheet_id)
    if sheet is None:
        raise HTTPException(404, "Sheet not found")
    return sheet


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
            "warnings": checks(sheet, entries),
            "provider": describe_provider(),
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
        if _sheet_or_404(conn, sheet_id)["reading"]:
            raise HTTPException(409, "This sheet is still being read. Wait for it to finish, then save.")
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
def reparse(sheet_id: int, background: BackgroundTasks):
    with db.session() as conn:
        sheet = _sheet_or_404(conn, sheet_id)
    if not sheet["reading"]:
        content = (config.IMAGES_DIR / sheet["image_path"]).read_bytes()
        mark_reading(sheet_id)
        background.add_task(parse_into_sheet, sheet_id, content, sheet["caption"])
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


# ---------- WhatsApp ----------


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request, background: BackgroundTasks):
    if not config.whatsapp_enabled() or not config.WHATSAPP_WEBHOOK_SECRET:
        raise HTTPException(503, "WhatsApp is not configured (set WAHA_URL and WHATSAPP_WEBHOOK_SECRET).")
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("x-webhook-hmac")):
        raise HTTPException(401, "Bad webhook signature")
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(400, "Body is not JSON")
    job = whatsapp.handle_event(event)
    if job is not None:
        background.add_task(job)  # parsing takes a while; answer WAHA right away
    return {"ok": True}


@app.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_page(request: Request):
    ctx = {"enabled": config.whatsapp_enabled(), "session": None, "error": None,
           "chat": None, "fixed_chat": bool(config.WHATSAPP_CHAT_ID), "sheets": []}
    if ctx["enabled"]:
        try:
            ctx["session"] = whatsapp.get_client().session_info()
        except whatsapp.WahaError as exc:
            ctx["error"] = str(exc)
        except Exception as exc:
            ctx["error"] = f"WhatsApp bridge error: {exc}"
        ctx["chat"] = whatsapp.active_chat()
        with db.session() as conn:
            ctx["sheets"] = [s for s in db.list_sheets(conn, limit=200) if s["source"] == "whatsapp"][:10]
    ctx["secret_missing"] = ctx["enabled"] and not config.WHATSAPP_WEBHOOK_SECRET
    return templates.TemplateResponse(request, "whatsapp.html", ctx)


@app.get("/whatsapp/qr")
def whatsapp_qr():
    try:
        png = whatsapp.get_client().qr_png()
    except whatsapp.WahaError as exc:
        raise HTTPException(502, str(exc))
    if png is None:
        raise HTTPException(404, "No QR code right now")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/whatsapp/start")
def whatsapp_start():
    try:
        whatsapp.get_client().start_session()
    except whatsapp.WahaError as exc:
        raise HTTPException(502, str(exc))
    return RedirectResponse("/whatsapp", status_code=303)


@app.post("/whatsapp/logout")
def whatsapp_logout():
    try:
        whatsapp.get_client().logout()
    except whatsapp.WahaError as exc:
        raise HTTPException(502, str(exc))
    return RedirectResponse("/whatsapp", status_code=303)


@app.post("/whatsapp/forget-chat")
def whatsapp_forget_chat():
    with db.session() as conn:
        db.set_setting(conn, whatsapp.CHAT_SETTING, None)
    return RedirectResponse("/whatsapp", status_code=303)

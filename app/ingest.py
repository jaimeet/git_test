"""Shared pipeline for turning a photo into a draft sheet, used by web uploads and WhatsApp."""

import logging
import uuid
from datetime import date

from . import config, db
from .parser import ParsedSheet, ParseError, get_parser

log = logging.getLogger("hisab")


def money(paise) -> str:
    value = db.from_paise(paise)
    if value is None:
        return ""
    return f"{config.CURRENCY}{value:,.2f}"


def save_image(content: bytes, suffix: str = ".jpg") -> str:
    name = f"{date.today().isoformat()}-{uuid.uuid4().hex[:10]}{suffix}"
    (config.IMAGES_DIR / name).write_bytes(content)
    return name


def parse_into_sheet(sheet_id: int, content: bytes, hint: str | None = None) -> None:
    try:
        parsed = get_parser().parse(content, hint=hint)
    except Exception as exc:
        if not isinstance(exc, ParseError):
            log.exception("Unexpected error parsing sheet %s", sheet_id)
        message = str(exc) if isinstance(exc, ParseError) else f"Unexpected error: {exc}"
        with db.session() as conn:
            db.update_sheet_meta(conn, sheet_id, parse_error=message)
        return
    store_parsed(sheet_id, parsed)


def store_parsed(sheet_id: int, parsed: ParsedSheet) -> None:
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


def checks(sheet, entries) -> list[str]:
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
        warnings.append(f"{low} line(s) were hard to read — please check them.")
    return warnings

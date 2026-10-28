"""WhatsApp intake through WAHA (https://github.com/devlikeapro/waha).

WAHA runs as a linked device on your WhatsApp number and POSTs every message to
/whatsapp/webhook. Photos posted in the one chat you chose become draft sheets,
and Hisab replies in that chat with what it read.

Pick the chat by sending "hisab here" in it from your own phone; "hisab stop" turns it off.
"""

import hashlib
import hmac
import logging
import mimetypes
import re
import sqlite3
from urllib.parse import urlsplit

import httpx

from . import config, db
from .ingest import checks, mark_reading, money, parse_into_sheet, save_image

log = logging.getLogger("hisab.whatsapp")

CHAT_SETTING = "whatsapp_chat_id"
HELP_TEXT = (
    "Hisab commands (send from your own phone):\n"
    "• hisab here — read hisab photos posted in this chat\n"
    "• hisab stop — stop reading photos\n"
    "• hisab status — show which chat is being read"
)


class WahaError(Exception):
    pass


class WahaClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, session: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url if base_url is not None else config.WAHA_URL).rstrip("/")
        self.session = session or config.WAHA_SESSION
        key = api_key if api_key is not None else config.WAHA_API_KEY
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={"X-Api-Key": key} if key else {},
            timeout=httpx.Timeout(30.0, connect=5.0),
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WahaError(f"Could not reach WhatsApp bridge at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise WahaError("WhatsApp bridge rejected the API key. Check WAHA_API_KEY matches on both containers.")
        return resp

    def session_info(self) -> dict | None:
        resp = self._request("GET", f"/api/sessions/{self.session}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def start_session(self) -> None:
        """Create, start, or (if it failed) restart the session."""
        info = self.session_info()
        if info is None:
            resp = self._request("POST", "/api/sessions", json={"name": self.session, "start": True})
        elif info.get("status") == "STOPPED":
            resp = self._request("POST", f"/api/sessions/{self.session}/start")
        else:
            resp = self._request("POST", f"/api/sessions/{self.session}/restart")
        if resp.status_code >= 400:
            raise WahaError(f"Could not start WhatsApp session: {resp.status_code} {resp.text[:200]}")

    def logout(self) -> None:
        self._request("POST", f"/api/sessions/{self.session}/logout")

    def qr_png(self) -> bytes | None:
        resp = self._request("GET", f"/api/{self.session}/auth/qr", headers={"Accept": "image/png"})
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            return None
        return resp.content

    def download(self, media_url: str) -> bytes:
        # WAHA builds media URLs from its own idea of its address; fetch the same path from the address we know.
        parts = urlsplit(media_url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        resp = self._request("GET", path)
        if resp.status_code != 200:
            raise WahaError(f"Could not download the photo from WhatsApp bridge ({resp.status_code}).")
        return resp.content

    def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        body = {"session": self.session, "chatId": chat_id, "text": text, "linkPreview": False}
        if reply_to:
            body["reply_to"] = reply_to
        resp = self._request("POST", "/api/sendText", json=body)
        if resp.status_code >= 400:
            log.warning("sendText failed: %s %s", resp.status_code, resp.text[:200])


_client: WahaClient | None = None


def get_client() -> WahaClient:
    global _client
    if _client is None:
        _client = WahaClient()
    return _client


def set_client(client: WahaClient | None) -> None:
    """Swap the WAHA client (used by tests)."""
    global _client
    _client = client


def verify_signature(body: bytes, signature: str | None) -> bool:
    """WAHA signs the raw body with HMAC-SHA512 using WHATSAPP_HOOK_HMAC_KEY."""
    if not config.WHATSAPP_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(config.WHATSAPP_WEBHOOK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def active_chat() -> str | None:
    if config.WHATSAPP_CHAT_ID:
        return config.WHATSAPP_CHAT_ID
    with db.session() as conn:
        return db.get_setting(conn, CHAT_SETTING)


def chat_of(payload: dict) -> str | None:
    # For your own messages WAHA puts the chat in "to"; for everyone else's (including groups) it's "from".
    return payload.get("to") if payload.get("fromMe") else payload.get("from")


def _command(text: str | None) -> str | None:
    words = re.sub(r"\s+", " ", (text or "").strip().lower()).split(" ")
    if len(words) == 2 and words[0] in ("hisab", "/hisab", "#hisab"):
        return words[1]
    return None


def handle_event(event: dict):
    """Decide what to do with one webhook event. Returns a callable to run in the background, or None."""
    if event.get("event") != "message.any":
        return None
    if event.get("session") and event["session"] != config.WAHA_SESSION:
        return None
    payload = event.get("payload") or {}
    if payload.get("source") == "api":
        return None  # our own replies echoing back
    chat = chat_of(payload)
    if not chat or chat == "status@broadcast":
        return None

    command = _command(payload.get("body")) if payload.get("fromMe") else None
    if command:
        return lambda: run_command(command, chat, payload.get("id"))

    if chat != active_chat():
        return None
    media = payload.get("media") or {}
    if not payload.get("hasMedia") or not (media.get("mimetype") or "").startswith("image/"):
        return None
    with db.session() as conn:
        if db.sheet_by_source_ref(conn, payload["id"]):
            return None  # WAHA retried a webhook we already handled
    return lambda: ingest_photo(payload, chat)


def run_command(command: str, chat: str, message_id: str | None) -> None:
    client = get_client()
    if command in ("here", "start"):
        if config.WHATSAPP_CHAT_ID and config.WHATSAPP_CHAT_ID != chat:
            reply = "Hisab is fixed to another chat by WHATSAPP_CHAT_ID in its settings; remove that to choose here."
        else:
            with db.session() as conn:
                db.set_setting(conn, CHAT_SETTING, chat)
            reply = "✅ Hisab will read hisab photos posted in this chat. Send “hisab stop” to turn it off."
    elif command == "stop":
        with db.session() as conn:
            if db.get_setting(conn, CHAT_SETTING) == chat:
                db.set_setting(conn, CHAT_SETTING, None)
                reply = "Hisab stopped reading photos here."
            else:
                reply = "Hisab wasn't reading this chat."
    elif command == "status":
        current = active_chat()
        reply = ("Hisab is reading photos in this chat." if current == chat
                 else "Hisab is reading a different chat." if current
                 else "Hisab isn't reading any chat. Send “hisab here” in the chat to use.")
    else:
        reply = HELP_TEXT
    client.send_text(chat, reply, reply_to=message_id)


def _sheet_link(sheet_id: int) -> str:
    if config.PUBLIC_URL:
        return f"{config.PUBLIC_URL}/sheets/{sheet_id}"
    return f"sheet #{sheet_id} in Hisab"


def ingest_photo(payload: dict, chat: str) -> int | None:
    client = get_client()
    message_id = payload["id"]
    media = payload.get("media") or {}
    caption = (payload.get("body") or "").strip() or None

    if not media.get("url"):
        detail = f" ({media['error']})" if media.get("error") else ""
        client.send_text(chat, f"⚠️ Hisab couldn't get this photo from WhatsApp{detail}. Try sending it again.", message_id)
        return None
    try:
        content = client.download(media["url"])
    except WahaError as exc:
        log.warning("%s", exc)
        client.send_text(chat, f"⚠️ Hisab couldn't download this photo: {exc}", message_id)
        return None

    suffix = mimetypes.guess_extension(media["mimetype"]) or ".jpg"
    name = save_image(content, ".jpg" if suffix in (".jpe", ".jpeg") else suffix)
    try:
        with db.session() as conn:
            sheet_id = db.create_sheet(conn, name, source="whatsapp", source_ref=message_id,
                                       source_chat=chat, caption=caption)
    except sqlite3.IntegrityError:
        (config.IMAGES_DIR / name).unlink(missing_ok=True)
        return None  # a concurrent retry of the same message got there first

    mark_reading(sheet_id)
    parse_into_sheet(sheet_id, content, hint=caption)

    with db.session() as conn:
        sheet = db.get_sheet(conn, sheet_id)
        entries = db.get_entries(conn, sheet_id)
        warnings = checks(sheet, entries)
        saved = bool(config.WHATSAPP_AUTO_SAVE and entries and not warnings and not sheet["parse_error"])
        if saved:
            db.update_sheet_meta(conn, sheet_id, status="confirmed", confirmed_at=db.now_iso())

    if config.WHATSAPP_REPLY:
        client.send_text(chat, summary_text(sheet, entries, warnings, saved), message_id)
    return sheet_id


def summary_text(sheet, entries, warnings: list[str], saved: bool) -> str:
    link = _sheet_link(sheet["id"])
    if sheet["parse_error"]:
        return f"⚠️ Hisab couldn't read this photo: {sheet['parse_error']}\nYou can type the lines in: {link}"
    total_in = sum(e["amount_paise"] for e in entries if e["kind"] == "in")
    total_out = sum(e["amount_paise"] for e in entries if e["kind"] == "out")
    lines = [
        f"📒 Hisab read {len(entries)} line{'s' if len(entries) != 1 else ''}"
        + (f" for {sheet['sheet_date']}" if sheet["sheet_date"] else ""),
        f"In: {money(total_in)}   Out: {money(total_out)}",
    ]
    lines += [f"• {w}" for w in warnings]
    lines.append(f"✅ Saved to ledger. Details: {link}" if saved else f"Please check and save: {link}")
    return "\n".join(lines)

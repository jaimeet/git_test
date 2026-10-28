import hashlib
import hmac
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, whatsapp
from app.parser import set_parser
from tests.test_app import SAMPLE, FakeParser, jpeg_bytes

SECRET = "hook-secret"
ME = "919800000000@c.us"
GROUP = "120363000000000000@g.us"


class FakeWaha:
    """Stands in for the WAHA container."""

    def __init__(self):
        self.sent = []
        self.downloads = []
        self.photo = jpeg_bytes()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "waha-key"
        if request.url.path == "/api/sendText":
            self.sent.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "sent"})
        if request.url.path.startswith("/api/files/"):
            self.downloads.append(str(request.url))
            return httpx.Response(200, content=self.photo, headers={"content-type": "image/jpeg"})
        if request.url.path == "/api/sessions/default":
            return httpx.Response(200, json={"name": "default", "status": "WORKING",
                                             "me": {"id": ME, "pushName": "Jaimeet"}})
        return httpx.Response(404)


@pytest.fixture
def wa(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hisab.db")
    monkeypatch.setattr(config, "APP_PASSWORD", "web-password")  # webhook must still get through
    monkeypatch.setattr(config, "WAHA_URL", "http://waha:3000")
    monkeypatch.setattr(config, "WHATSAPP_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(config, "WHATSAPP_CHAT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", "http://pi.local:8092")
    monkeypatch.setattr(config, "WHATSAPP_AUTO_SAVE", False)
    fake = FakeWaha()
    whatsapp.set_client(whatsapp.WahaClient("http://waha:3000", "waha-key", "default",
                                            transport=httpx.MockTransport(fake.handler)))
    parser = FakeParser()
    set_parser(parser)
    from app.main import app

    with TestClient(app) as c:
        c.waha, c.parser = fake, parser
        yield c
    whatsapp.set_client(None)
    set_parser(None)


def post_event(client, payload, event="message.any", secret=SECRET):
    body = json.dumps({"event": event, "session": "default", "payload": payload}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return client.post("/whatsapp/webhook", content=body,
                       headers={"content-type": "application/json", "X-Webhook-Hmac": sig})


def text_msg(body, chat=GROUP, from_me=True, msg_id="m-text"):
    return {"id": msg_id, "fromMe": from_me, "from": ME if from_me else chat, "to": chat if from_me else ME,
            "body": body, "hasMedia": False, "source": "app"}


def photo_msg(chat=GROUP, from_me=False, msg_id="m-photo", caption=""):
    return {"id": msg_id, "fromMe": from_me, "from": ME if from_me else chat, "to": chat if from_me else ME,
            "participant": "919811111111@c.us", "body": caption, "hasMedia": True,
            "media": {"url": f"http://localhost:3000/api/files/default/{msg_id}.jpeg", "mimetype": "image/jpeg"}}


def sheets(client):
    with db.session() as conn:
        return db.list_sheets(conn)


def test_rejects_bad_signature(wa):
    assert post_event(wa, text_msg("hisab here"), secret="wrong").status_code == 401
    r = wa.post("/whatsapp/webhook", json={"event": "message.any", "payload": {}})
    assert r.status_code == 401


def test_full_flow(wa):
    # Photos are ignored until a chat is chosen.
    post_event(wa, photo_msg(msg_id="early"))
    assert sheets(wa) == []

    # Someone else can't choose the chat; only the account owner can.
    post_event(wa, text_msg("hisab here", from_me=False))
    assert whatsapp.active_chat() is None

    assert post_event(wa, text_msg("Hisab  HERE")).status_code == 200
    assert whatsapp.active_chat() == GROUP
    assert "will read hisab photos" in wa.waha.sent[-1]["text"]
    assert wa.waha.sent[-1]["chatId"] == GROUP

    # A photo in the chosen chat becomes a draft sheet, using the caption as a hint.
    post_event(wa, photo_msg(caption="27 Oct"))
    [sheet] = sheets(wa)
    assert sheet["source"] == "whatsapp" and sheet["status"] == "draft" and sheet["caption"] == "27 Oct"
    assert wa.parser.last_hint == "27 Oct"
    assert wa.waha.downloads == ["http://waha:3000/api/files/default/m-photo.jpeg"]  # host rewritten
    reply = wa.waha.sent[-1]
    assert reply["reply_to"] == "m-photo"
    assert "read 3 lines for 2026-10-27" in reply["text"]
    assert "Please check and save: http://pi.local:8092/sheets/" in reply["text"]
    assert "sheet says" in reply["text"]  # written total mismatch is reported

    # WAHA retrying the same webhook doesn't create a second sheet.
    post_event(wa, photo_msg(caption="27 Oct"))
    assert len(sheets(wa)) == 1

    # Photos elsewhere, our own API echoes, and non-images are ignored.
    post_event(wa, photo_msg(chat="919822222222@c.us", msg_id="other-chat"))
    post_event(wa, {**photo_msg(msg_id="echo", from_me=True), "source": "api"})
    post_event(wa, {**photo_msg(msg_id="pdf"), "media": {"url": "http://x/api/files/a.pdf", "mimetype": "application/pdf"}})
    post_event(wa, photo_msg(msg_id="plain-message-event"), event="message")
    assert len(sheets(wa)) == 1

    # The owner's own photo in the chat counts too.
    post_event(wa, photo_msg(from_me=True, msg_id="mine"))
    assert len(sheets(wa)) == 2

    # Status page renders with basic auth.
    page = wa.get("/whatsapp", auth=("hisab", "web-password")).text
    assert "connected" in page and "Jaimeet" in page and GROUP in page

    post_event(wa, text_msg("hisab stop", msg_id="m-stop"))
    assert whatsapp.active_chat() is None
    post_event(wa, photo_msg(msg_id="after-stop"))
    assert len(sheets(wa)) == 2


def test_auto_save_only_when_clean(wa, monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_AUTO_SAVE", True)
    monkeypatch.setattr(config, "WHATSAPP_CHAT_ID", GROUP)

    post_event(wa, photo_msg(msg_id="messy"))  # SAMPLE has a total mismatch and a low-confidence line
    assert sheets(wa)[0]["status"] == "draft"

    clean = SAMPLE.model_copy(update={
        "written_total_out": 560, "closing_balance": 1690.5,
        "entries": [e.model_copy(update={"confidence": "high"}) for e in SAMPLE.entries],
    })
    wa.parser.result = clean
    post_event(wa, photo_msg(msg_id="clean"))
    statuses = {s["source_ref"]: s["status"] for s in sheets(wa)}
    assert statuses == {"messy": "draft", "clean": "confirmed"}
    assert "Saved to ledger" in wa.waha.sent[-1]["text"]


def test_parse_failure_reply(wa, monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_CHAT_ID", GROUP)
    wa.parser.error = "No Anthropic API key configured. Set ANTHROPIC_API_KEY."
    post_event(wa, photo_msg())
    assert "couldn't read this photo" in wa.waha.sent[-1]["text"]
    assert "type the lines in" in wa.waha.sent[-1]["text"]


def test_existing_database_is_migrated(tmp_path, monkeypatch):
    """Databases created by the first release gain the new columns without losing data."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sheets (id INTEGER PRIMARY KEY AUTOINCREMENT, sheet_date TEXT, image_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', parse_error TEXT, raw_json TEXT, opening_balance_paise INTEGER,
            closing_balance_paise INTEGER, written_total_in_paise INTEGER, written_total_out_paise INTEGER,
            notes TEXT, created_at TEXT NOT NULL, confirmed_at TEXT);
        INSERT INTO sheets (image_path, created_at, status) VALUES ('old.jpg', '2026-08-01', 'confirmed');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DB_PATH", path)
    db.init_db()
    db.init_db()  # idempotent
    with db.session() as conn:
        [row] = db.list_sheets(conn)
    assert row["image_path"] == "old.jpg" and row["source"] == "upload" and row["status"] == "confirmed"


def test_start_session_picks_the_right_call():
    calls = []
    status = {"value": None}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404) if status["value"] is None else httpx.Response(200, json={"status": status["value"]})
        return httpx.Response(201, json={})

    client = whatsapp.WahaClient("http://waha:3000", "k", "default", transport=httpx.MockTransport(handler))
    for value, expected in [(None, "/api/sessions"), ("STOPPED", "/api/sessions/default/start"),
                            ("FAILED", "/api/sessions/default/restart")]:
        status["value"] = value
        calls.clear()
        client.start_session()
        assert calls[-1] == ("POST", expected)


def test_wrong_api_key_gives_clear_error():
    client = whatsapp.WahaClient("http://waha:3000", "bad", "default",
                                 transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(whatsapp.WahaError, match="WAHA_API_KEY"):
        client.session_info()

import base64
import io
import json

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, db
from app.parser import ClaudeSheetParser, ParsedEntry, ParsedSheet, ParseError, set_parser


def jpeg_bytes(size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG")
    return buf.getvalue()


SAMPLE = ParsedSheet(
    sheet_date="2026-10-27",
    entries=[
        ParsedEntry(description="Milk", original_text="दूध", party=None, kind="out", amount=60,
                    entry_date=None, category="Milk", confidence="high"),
        ParsedEntry(description="Shop sales", original_text=None, party=None, kind="in", amount=1250.5,
                    entry_date=None, category="Sales", confidence="high"),
        ParsedEntry(description="Ramesh udhar", original_text=None, party="Ramesh", kind="out", amount=500,
                    entry_date=None, category="Loan", confidence="low"),
    ],
    opening_balance=1000,
    closing_balance=1690.5,
    written_total_in=1250.5,
    written_total_out=600,  # deliberately wrong: lines add to 560
    notes="Last line smudged",
)


class FakeParser:
    def __init__(self, result=SAMPLE, error=None):
        self.result, self.error, self.calls = result, error, 0

    def parse(self, image_bytes, hint=None):
        self.calls += 1
        self.last_hint = hint
        if self.error:
            raise ParseError(self.error)
        return self.result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hisab.db")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    fake = FakeParser()
    set_parser(fake)
    from app.main import app

    with TestClient(app) as c:
        c.fake = fake
        yield c
    set_parser(None)


def upload(client):
    return client.post("/upload", files={"photo": ("sheet.jpg", jpeg_bytes(), "image/jpeg")}, follow_redirects=False)


def test_paise_roundtrip():
    assert db.to_paise("1,25,000.50") == 12500050
    assert db.to_paise(0.1 + 0.2) == 30
    assert db.to_paise("") is None
    assert str(db.from_paise(12500050)) == "125000.50"


def test_upload_parse_review_confirm_ledger(client):
    r = upload(client)
    assert r.status_code == 303
    sheet_url = r.headers["location"]

    page = client.get(sheet_url).text
    assert "Milk" in page and "Ramesh" in page
    assert "sheet says" in page            # written total mismatch is flagged
    assert "hard to read" in page          # low confidence flagged
    assert 'class="low"' in page

    # Not in the ledger until confirmed.
    assert client.get("/api/entries").json()["entries"] == []

    # Reviewer fixes the smudged line to 440 and confirms.
    form = {
        "sheet_date": "2026-10-27",
        "entry_date": ["2026-10-27"] * 3,
        "description": ["Milk", "Shop sales", "Ramesh udhar"],
        "party": ["", "", "Ramesh"],
        "category": ["Milk", "Sales", "Loan"],
        "kind": ["out", "in", "out"],
        "amount": ["60", "1,250.50", "540"],
        "confidence": ["high", "high", "low"],
        "notes": "checked",
        "action": "confirm",
    }
    r = client.post(sheet_url, data=form, follow_redirects=False)
    assert r.status_code == 303

    data = client.get("/api/entries").json()
    assert data["total_in"] == "1250.50"
    assert data["total_out"] == "600.00"
    assert len(data["entries"]) == 3

    page = client.get(sheet_url).text
    assert "sheet says" not in page  # totals now match

    csv_text = client.get("/entries.csv").text
    assert "Ramesh udhar" in csv_text and "540.00" in csv_text

    assert client.get("/entries?q=Ramesh").status_code == 200
    assert len(client.get("/api/entries?q=Ramesh").json()["entries"]) == 1
    assert client.get("/api/entries?date_from=2026-10-28").json()["entries"] == []


def test_parse_error_still_allows_manual_entry(client):
    client.fake.error = "Anthropic API key is missing or invalid."
    r = upload(client)
    sheet_url = r.headers["location"]
    page = client.get(sheet_url).text
    assert "Couldn&#39;t read automatically" in page or "Couldn't read automatically" in page

    r = client.post(sheet_url, data={
        "sheet_date": "2026-10-20", "entry_date": [""], "description": ["Tea"], "party": [""],
        "category": [""], "kind": ["out"], "amount": ["20"], "confidence": [""], "action": "confirm",
    }, follow_redirects=False)
    assert r.status_code == 303
    rows = client.get("/api/entries").json()["entries"]
    assert rows == [{**rows[0], "date": "2026-10-20", "amount": "20.00", "kind": "out"}]


def test_invalid_amount_rejected(client):
    sheet_url = upload(client).headers["location"]
    r = client.post(sheet_url, data={
        "description": ["Tea"], "amount": ["abc"], "kind": ["out"], "action": "confirm",
    })
    assert r.status_code == 400


def test_reparse_and_delete(client):
    sheet_url = upload(client).headers["location"]
    client.post(sheet_url + "/reparse")
    assert client.fake.calls == 2
    images = list(config.IMAGES_DIR.iterdir())
    assert len(images) == 1
    client.post(sheet_url + "/delete")
    assert client.get(sheet_url).status_code == 404
    assert list(config.IMAGES_DIR.iterdir()) == []


def test_rejects_non_image(client):
    r = client.post("/upload", files={"photo": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400


def test_image_path_traversal(client):
    assert client.get("/images/..%2Fhisab.db").status_code == 404


def test_basic_auth(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    token = base64.b64encode(b"hisab:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_claude_parser_request_shape():
    """Runs the real SDK against a mocked transport to check what we send and how we read the reply."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "content": [{"type": "text", "text": SAMPLE.model_dump_json()}],
        })

    sdk = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    parsed = ClaudeSheetParser(sdk).parse(jpeg_bytes((3000, 1000)))

    assert parsed.entries[1].amount == 1250.5
    body = seen["body"]
    assert body["model"] == config.ANTHROPIC_MODEL
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    image = body["messages"][0]["content"][0]
    assert image["source"]["media_type"] == "image/jpeg"
    resized = Image.open(io.BytesIO(base64.b64decode(image["source"]["data"])))
    assert max(resized.size) == 2000


def test_claude_parser_refusal():
    def handler(request):
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": "refusal", "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": None, "explanation": None},
            "usage": {"input_tokens": 10, "output_tokens": 0}, "content": [],
        })

    sdk = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(ParseError):
        ClaudeSheetParser(sdk).parse(jpeg_bytes())


def test_claude_parser_without_api_key(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no saved `ant auth login` profile
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    sdk = anthropic.Anthropic(http_client=httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(500))))
    with pytest.raises(ParseError, match="ANTHROPIC_API_KEY"):
        ClaudeSheetParser(sdk).parse(jpeg_bytes())


def test_unexpected_parser_crash_is_recorded(client):
    class Boom:
        def parse(self, image_bytes, hint=None):
            raise RuntimeError("kaboom")

    set_parser(Boom())
    r = upload(client)
    assert r.status_code == 303
    assert "kaboom" in client.get(r.headers["location"]).text

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app import config, db, parser
from app.parser import OllamaSheetParser, ParseError
from tests.test_app import SAMPLE, client, jpeg_bytes, upload  # noqa: F401 (fixture)


def ollama(handler):
    return OllamaSheetParser("http://laptop:11434", "qwen3-vl:8b", transport=httpx.MockTransport(handler))


def test_request_and_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "qwen3-vl:8b", "done": True, "done_reason": "stop",
                                         "message": {"role": "assistant", "content": SAMPLE.model_dump_json()}})

    assert ollama(handler).parse(jpeg_bytes((3000, 1000)), hint="27 Oct") == SAMPLE
    body = seen["body"]
    assert seen["url"] == "http://laptop:11434/api/chat"
    assert body["model"] == "qwen3-vl:8b" and body["stream"] is False
    assert "$ref" not in json.dumps(body["format"]) and "entries" in body["format"]["properties"]
    assert body["options"]["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    user = body["messages"][1]
    assert "27 Oct" in user["content"]
    assert max(Image.open(io.BytesIO(base64.b64decode(user["images"][0]))).size) == 2000


@pytest.mark.parametrize("response,needle", [
    (httpx.Response(404, json={"error": "model 'qwen3-vl:8b' not found"}), "ollama pull qwen3-vl:8b"),
    (httpx.Response(500, json={"error": "out of memory"}), "Ollama error 500: out of memory"),
    (httpx.Response(200, json={"done_reason": "length", "message": {"content": "{\"entries\": ["}}), "OLLAMA_NUM_CTX"),
    (httpx.Response(200, json={"done_reason": "stop", "message": {"content": "sorry, I can't"}}), "wasn't valid sheet data"),
])
def test_errors(response, needle):
    with pytest.raises(ParseError, match=needle):
        ollama(lambda r: response).parse(jpeg_bytes())


def test_laptop_off():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ParseError, match="OLLAMA_HOST"):
        ollama(handler).parse(jpeg_bytes())


def test_provider_choice(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-x")
    monkeypatch.setattr(config, "OLLAMA_URL", "http://laptop:11434")
    assert parser.choose_provider() == "ollama"  # local wins in auto mode
    assert "local model" in parser.describe_provider()
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    assert parser.choose_provider() == "gemini"


def test_upload_returns_before_reading_and_restart_recovery(client):
    """The sheet page works while reading, saving is blocked until it finishes, and restarts don't leave it stuck."""
    sheet_url = upload(client).headers["location"]
    sheet_id = int(sheet_url.rsplit("/", 1)[1])
    with db.session() as conn:
        db.update_sheet_meta(conn, sheet_id, reading=1)  # pretend the AI is still working

    page = client.get(sheet_url).text
    assert "Reading the sheet" in page and 'id="sheet-form"' not in page
    assert client.post(sheet_url, data={"description": ["x"], "amount": ["1"], "kind": ["out"]}).status_code == 409
    assert "reading…" in client.get("/").text

    db.init_db()  # what happens on the next app start
    with db.session() as conn:
        sheet = db.get_sheet(conn, sheet_id)
    assert sheet["reading"] == 0 and "interrupted" in sheet["parse_error"]
    assert 'id="sheet-form"' in client.get(sheet_url).text

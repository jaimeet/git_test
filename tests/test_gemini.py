import base64
import io
import json

import httpx
import pytest
from google import genai
from google.genai import types
from PIL import Image

from app import config, parser
from app.parser import GeminiSheetParser, ParseError
from tests.test_app import SAMPLE, jpeg_bytes


def gemini_client(handler):
    return genai.Client(api_key="test-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry_options=types.HttpRetryOptions(attempts=1),
    ))


def reply(text, finish="STOP"):
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10},
    })


def test_request_and_response(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-flash-latest")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return reply(SAMPLE.model_dump_json())

    parsed = GeminiSheetParser(gemini_client(handler)).parse(jpeg_bytes((3000, 1000)), hint="27 Oct")

    assert parsed == SAMPLE
    assert "models/gemini-flash-latest:generateContent" in seen["url"]
    assert seen["key"] == "test-key"
    body = seen["body"]
    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert "entries" in json.dumps(gen.get("responseSchema") or gen.get("responseJsonSchema"))
    assert "hisab" in body["systemInstruction"]["parts"][0]["text"]
    parts = body["contents"][0]["parts"]
    b64 = parts[0]["inlineData"]["data"]
    image = Image.open(io.BytesIO(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))))
    assert max(image.size) == 2000
    assert "27 Oct" in parts[1]["text"]


@pytest.mark.parametrize("status,needle", [
    (400, "API key is missing or invalid"),
    (429, "free daily or per-minute limit"),
    (404, "was not found"),
    (500, "Gemini API error 500"),
])
def test_api_errors(status, needle):
    def handler(request):
        message = "API key not valid. Please pass a valid API key." if status == 400 else "nope"
        reason = "API_KEY_INVALID" if status == 400 else "OTHER"
        return httpx.Response(status, json={"error": {"code": status, "message": message, "status": reason}})

    with pytest.raises(ParseError, match=needle):
        GeminiSheetParser(gemini_client(handler)).parse(jpeg_bytes())


def test_blocked_and_truncated():
    with pytest.raises(ParseError, match="declined"):
        GeminiSheetParser(gemini_client(lambda r: reply("", finish="SAFETY"))).parse(jpeg_bytes())
    with pytest.raises(ParseError, match="too long"):
        GeminiSheetParser(gemini_client(lambda r: reply('{"entries": [', finish="MAX_TOKENS"))).parse(jpeg_bytes())


def test_provider_choice(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(config, "AI_PROVIDER", "auto")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert parser.choose_provider() is None
    parser.set_parser(None)
    with pytest.raises(ParseError, match="GEMINI_API_KEY"):
        parser.get_parser().parse(jpeg_bytes())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert parser.choose_provider() == "claude"

    monkeypatch.setattr(config, "GEMINI_API_KEY", "AIza-x")
    assert parser.choose_provider() == "gemini"  # Gemini wins when both are set

    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    assert parser.choose_provider() == "claude"

    monkeypatch.setattr(config, "AI_PROVIDER", "auto")
    parser.set_parser(None)
    assert isinstance(parser.get_parser(), GeminiSheetParser)
    parser.set_parser(None)

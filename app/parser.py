"""Turn a photo of a hisab sheet into structured entries using a local Ollama model, Gemini, or Claude."""

import base64
import io
import os
from typing import Literal, Optional, Protocol

import anthropic
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from . import config

# Claude reads detail best when the long edge is near this size; bigger just costs more tokens.
MAX_EDGE_PX = int(os.environ.get("PARSE_MAX_EDGE_PX", "2000"))


class ParsedEntry(BaseModel):
    description: str = Field(description="What the line is for, transliterated/translated to English if needed, e.g. 'Milk', 'Rent'.")
    original_text: Optional[str] = Field(description="The line as written on the sheet, in its original script.")
    party: Optional[str] = Field(description="Person or shop the money went to / came from, if written.")
    kind: Literal["in", "out"] = Field(description="'in' = money received (jama/aavak/credit). 'out' = money paid or spent (naam/udhar diya/kharch/debit).")
    amount: float = Field(description="Amount in rupees as a plain number, e.g. 1250.5.")
    entry_date: Optional[str] = Field(description="YYYY-MM-DD if this line has its own date, else null.")
    category: Optional[str] = Field(description="Short category such as Grocery, Salary, Rent, Transport, Sales, Loan.")
    confidence: Literal["high", "medium", "low"] = Field(description="How sure you are about the amount and the in/out direction.")


class ParsedSheet(BaseModel):
    sheet_date: Optional[str] = Field(description="Date of the sheet as YYYY-MM-DD, or null if not written.")
    entries: list[ParsedEntry]
    opening_balance: Optional[float] = Field(description="Opening balance / aagal baki if written.")
    closing_balance: Optional[float] = Field(description="Closing balance / baki if written.")
    written_total_in: Optional[float] = Field(description="Total of the 'in' column exactly as written on the sheet, if any.")
    written_total_out: Optional[float] = Field(description="Total of the 'out' column exactly as written on the sheet, if any.")
    notes: Optional[str] = Field(description="Anything ambiguous the reviewer should check: smudged digits, crossed-out lines, unclear direction.")


SYSTEM_PROMPT = """You read photos of handwritten or printed daily account sheets (hisab / rojmel / khata) \
from small Indian households and businesses and transcribe every money line into structured data.

How these sheets are usually written:
- Text may be English, Hindi, Gujarati, Marathi, or another Indian language, often mixed. Digits may be \
Latin (0-9) or in Devanagari/Gujarati script; convert them to ordinary numbers.
- Two-column layouts are common: the left side is often jama (receipts, money in) and the right side \
naam/udhar (payments, money out). Follow the headings actually written on the sheet when present. \
A single list of expenses with no income heading is all 'out'.
- Amounts may use Indian grouping (1,25,000) and a '/-' suffix; ignore those marks. A trailing '.50' or \
'-50' after rupees may be paise.
- Totals, sub-totals, carried-forward and balance lines are not entries: report them in the balance and \
total fields instead of in entries.
- Lines that are fully struck through were cancelled: skip them and mention them in notes.

Transcribe faithfully. Do not invent lines or fix the writer's arithmetic. When a digit or direction is \
unclear, give your best reading, mark confidence 'low', and say what is unclear in notes. The person \
reviewing your output will correct mistakes before anything is saved."""


class SheetParser(Protocol):
    def parse(self, image_bytes: bytes, hint: str | None = None) -> ParsedSheet: ...


class ParseError(Exception):
    pass


def prepare_image(image_bytes: bytes) -> tuple[bytes, str]:
    """Fix phone-camera rotation, shrink to a sensible size, and re-encode as JPEG."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        raise ParseError(f"Could not read image: {exc}") from exc
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue(), "image/jpeg"


def build_instruction(hint: str | None) -> str:
    instruction = "Transcribe this hisab sheet."
    if hint:
        instruction += f"\n\nThe person who sent the photo added this caption (it may give the date or context):\n{hint}"
    return instruction


class ClaudeSheetParser:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def parse(self, image_bytes: bytes, hint: str | None = None) -> ParsedSheet:
        data, media_type = prepare_image(image_bytes)
        instruction = build_instruction(hint)
        try:
            response = self.client.beta.messages.parse(
                model=config.ANTHROPIC_MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                output_config={"effort": config.PARSE_EFFORT},
                output_format=ParsedSheet,
                # If a safety classifier declines, let the API retry on its recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.standard_b64encode(data).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": instruction},
                        ],
                    }
                ],
            )
        except TypeError as exc:
            # The SDK raises this before sending anything when no credentials are configured.
            if "authentication" not in str(exc).lower():
                raise
            raise ParseError("No Anthropic API key configured. Set ANTHROPIC_API_KEY.") from exc
        except anthropic.CredentialsError as exc:
            raise ParseError(f"Anthropic credentials problem: {exc}") from exc
        except anthropic.AuthenticationError as exc:
            raise ParseError("Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise ParseError("Rate limited by the Anthropic API. Try again in a minute.") from exc
        except anthropic.APIStatusError as exc:
            raise ParseError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ParseError("Could not reach the Anthropic API. Check the Pi's internet connection.") from exc

        if response.stop_reason == "refusal":
            raise ParseError("The model declined to read this image.")
        if response.stop_reason == "max_tokens":
            raise ParseError("The sheet was too long to transcribe in one go. Try photographing it in halves.")
        if response.parsed_output is None:
            raise ParseError("The model did not return structured entries.")
        return response.parsed_output


class GeminiSheetParser:
    """Google Gemini. A free API key from https://aistudio.google.com covers personal use of the Flash models."""

    def __init__(self, client: genai.Client | None = None):
        self.client = client or genai.Client(api_key=config.GEMINI_API_KEY)

    def parse(self, image_bytes: bytes, hint: str | None = None) -> ParsedSheet:
        data, media_type = prepare_image(image_bytes)
        try:
            response = self.client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=data, mime_type=media_type),
                    build_instruction(hint),
                ],
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=ParsedSheet,
                ),
            )
        except genai_errors.APIError as exc:
            raise ParseError(_gemini_error_message(exc)) from exc
        except httpx.HTTPError as exc:
            raise ParseError("Could not reach the Gemini API. Check the Pi's internet connection.") from exc

        candidate = response.candidates[0] if response.candidates else None
        finish = candidate.finish_reason if candidate else None
        if finish == genai_types.FinishReason.MAX_TOKENS:
            raise ParseError("The sheet was too long to transcribe in one go. Try photographing it in halves.")
        if response.parsed is None:
            if finish not in (None, genai_types.FinishReason.STOP):
                raise ParseError(f"Gemini declined to read this image ({finish.name}).")
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                raise ParseError(f"Gemini declined to read this image ({response.prompt_feedback.block_reason.name}).")
            raise ParseError("Gemini did not return structured entries.")
        return response.parsed


def _gemini_error_message(exc: genai_errors.APIError) -> str:
    text = f"{exc.status or ''} {exc.message or ''}"
    if exc.code in (401, 403) or "API_KEY_INVALID" in text or "API key not valid" in text:
        return "Gemini API key is missing or invalid. Set GEMINI_API_KEY (get one free at aistudio.google.com)."
    if exc.code == 429:
        return "Gemini's free daily or per-minute limit was reached. Wait a bit, then use “Read photo again”."
    if exc.code == 404:
        return f"Gemini model '{config.GEMINI_MODEL}' was not found. Check GEMINI_MODEL."
    return f"Gemini API error {exc.code}: {exc.message}"


def _inline_refs(schema: dict) -> dict:
    """Ollama turns the schema into a grammar; give it one self-contained object without $ref/$defs."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


SHEET_SCHEMA = _inline_refs(ParsedSheet.model_json_schema())


class OllamaSheetParser:
    """A free vision model running locally under Ollama, e.g. on a laptop on the same network."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or config.OLLAMA_URL).rstrip("/")
        self.model = model or config.OLLAMA_MODEL
        self.http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(config.OLLAMA_TIMEOUT, connect=10.0),
            transport=transport,
        )

    def parse(self, image_bytes: bytes, hint: str | None = None) -> ParsedSheet:
        data, _ = prepare_image(image_bytes)
        body = {
            "model": self.model,
            "stream": False,
            "format": SHEET_SCHEMA,
            "options": {"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_instruction(hint) + "\n\nAnswer with JSON matching the given schema.",
                    "images": [base64.standard_b64encode(data).decode("ascii")],
                },
            ],
        }
        try:
            resp = self.http.post("/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise ParseError(
                f"The local model took longer than {int(config.OLLAMA_TIMEOUT)} seconds. "
                "Raise OLLAMA_TIMEOUT or use a smaller model."
            ) from exc
        except httpx.HTTPError as exc:
            raise ParseError(
                f"Could not reach Ollama at {self.base_url}. Is the laptop on, Ollama running, "
                "and OLLAMA_HOST set to 0.0.0.0 so other devices can connect?"
            ) from exc

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", "")
            except ValueError:
                detail = resp.text[:200]
            if resp.status_code == 404 or "not found" in detail.lower():
                raise ParseError(f"Model '{self.model}' isn't downloaded on the laptop. Run: ollama pull {self.model}")
            raise ParseError(f"Ollama error {resp.status_code}: {detail}")

        result = resp.json()
        if result.get("done_reason") == "length":
            raise ParseError("The local model ran out of room. Raise OLLAMA_NUM_CTX or photograph the sheet in halves.")
        content = (result.get("message") or {}).get("content", "")
        try:
            return ParsedSheet.model_validate_json(content)
        except ValueError as exc:
            raise ParseError("The local model's answer wasn't valid sheet data. Try “Read photo again” or another model.") from exc


class MissingKeyParser:
    def parse(self, image_bytes: bytes, hint: str | None = None) -> ParsedSheet:
        raise ParseError(
            "No AI configured. Set OLLAMA_URL (free local model), GEMINI_API_KEY (free from aistudio.google.com) "
            "or ANTHROPIC_API_KEY."
        )


def choose_provider() -> str | None:
    provider = config.AI_PROVIDER
    if provider in ("ollama", "gemini", "claude"):
        return provider
    if config.OLLAMA_URL:
        return "ollama"
    if config.GEMINI_API_KEY:
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "claude"
    return None


def describe_provider() -> str:
    provider = choose_provider()
    if provider == "ollama":
        return f"local model {config.OLLAMA_MODEL} via Ollama at {config.OLLAMA_URL or '(OLLAMA_URL not set)'}"
    if provider == "gemini":
        return f"Google Gemini ({config.GEMINI_MODEL})"
    if provider == "claude":
        return f"Anthropic Claude ({config.ANTHROPIC_MODEL})"
    return "no AI configured"


_parser: SheetParser | None = None


def get_parser() -> SheetParser:
    global _parser
    if _parser is None:
        provider = choose_provider()
        if provider == "ollama":
            _parser = OllamaSheetParser() if config.OLLAMA_URL else MissingKeyParser()
        elif provider == "gemini":
            _parser = GeminiSheetParser() if config.GEMINI_API_KEY else MissingKeyParser()
        elif provider == "claude":
            _parser = ClaudeSheetParser()
        else:
            return MissingKeyParser()  # not cached, so adding a key only needs a container restart
    return _parser


def set_parser(parser: SheetParser | None) -> None:
    """Swap the parser (used by tests)."""
    global _parser
    _parser = parser

"""Turn a photo of a hisab sheet into structured entries using Claude's vision."""

import base64
import io
import os
from typing import Literal, Optional, Protocol

import anthropic
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
    def parse(self, image_bytes: bytes) -> ParsedSheet: ...


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


class ClaudeSheetParser:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def parse(self, image_bytes: bytes) -> ParsedSheet:
        data, media_type = prepare_image(image_bytes)
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
                            {"type": "text", "text": "Transcribe this hisab sheet."},
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


_parser: SheetParser | None = None


def get_parser() -> SheetParser:
    global _parser
    if _parser is None:
        _parser = ClaudeSheetParser()
    return _parser


def set_parser(parser: SheetParser | None) -> None:
    """Swap the parser (used by tests)."""
    global _parser
    _parser = parser

# Hisab

A small self-hosted web app for your daily hisab (account) sheet. Photograph the page with your phone, the app reads every line, you check and correct it, and it is saved to a ledger you can search, total, and export to CSV. It runs in Docker on a Raspberry Pi 5.

## How it works

1. **Upload.** Open the app on your phone and tap *Take photo*. The photo is kept on the Pi under `data/images/`.
2. **Read.** The photo is straightened (EXIF rotation), resized, and sent to Claude's vision model (Anthropic API). Claude returns structured lines: description, original text, party, in (jama) or out (naam/kharch), amount, category, and a confidence level. It also returns any opening/closing balances and column totals written on the sheet. Hindi, Gujarati, Marathi and English sheets work, including Indian-script digits and `1,25,000/-` style amounts.
3. **Review.** The photo appears next to an editable table. Lines the model was unsure about are highlighted. If the lines don't add up to the totals written on the sheet, or opening + in − out doesn't equal the closing balance, you get a warning. You can fix, add or remove lines.
4. **Save.** Only sheets you have reviewed and saved appear in the **Ledger**. The ledger has date and text filters, in/out/net totals, spending by category, and CSV export.

If the photo can't be read (no API key, no internet, unreadable image), the sheet is still created and you can type the lines in by hand.

## Run on a Raspberry Pi 5

You need Raspberry Pi OS (64-bit) or another arm64 Linux, plus an Anthropic API key from <https://console.anthropic.com>.

Install Docker (skip if it's already installed), then log out and back in:

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER
```

Get the code and configure it:

```bash
git clone https://github.com/jaimeet/git_test.git hisab && cd hisab
```

```bash
cp .env.example .env && nano .env
```

Set `ANTHROPIC_API_KEY` in `.env`. Set `APP_PASSWORD` too if the Pi can be reached from outside your home network.

Create the data folder. The container runs as uid 1000, which is the default `pi` user, so the folder must be writable by that user:

```bash
mkdir -p data
```

Build and start:

```bash
docker compose up -d --build
```

Open `http://<pi-hostname>.local:8000` (or `http://<pi-ip>:8000`) on your phone. Add it to your home screen so it works like an app.

The image builds natively on the Pi: all dependencies have arm64 wheels, so nothing is compiled. The first build takes a few minutes.

### Update

```bash
git pull && docker compose up -d --build
```

### Backup

Everything lives in `./data`: `hisab.db` (SQLite) and `images/`. Copy that folder to back it up. For a consistent copy of the database while the app is running:

```bash
docker compose exec hisab python -c "import sqlite3; s=sqlite3.connect('/data/hisab.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
```

## Configuration

All settings go in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for automatic reading. |
| `APP_USERNAME` / `APP_PASSWORD` | `hisab` / empty | HTTP basic auth for the whole UI. Off when the password is empty. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model used to read sheets. |
| `PARSE_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max`. Higher effort reads messy handwriting more carefully but costs more and takes longer. |
| `PARSE_MAX_EDGE_PX` | `2000` | Photos are shrunk to this long edge before sending. |
| `CURRENCY` | `₹` | Symbol shown in the UI. |
| `MAX_UPLOAD_MB` | `25` | Upload size limit. |

## Privacy and cost

Sheet photos are sent to the Anthropic API to be read. The photos, the database and the ledger stay on your Pi. Each sheet is one API request, and you pay for its tokens at your API account's rates. The model's safety fallback (`fallbacks: "default"`) is turned on, so in the rare case the model declines an image, the API retries on its recommended fallback model.

## Tips for good photos

- Lay the page flat in good light and avoid shadows across the numbers.
- Fill the frame with the page; one page per photo.
- For very long sheets, photograph the top and bottom halves as two sheets.

## API

Read-only JSON for scripts and spreadsheets:

- `GET /api/entries?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&q=text` returns saved entries and totals.
- `GET /entries.csv` takes the same filters and returns CSV.
- `GET /healthz` is the health check (no auth).

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
DATA_DIR=./data uvicorn app.main:app --reload
```

```bash
python -m pytest -q
```

The tests use a fake parser for the web flow and drive the real Anthropic SDK against a mocked HTTP transport, so they need no API key or network.

Layout:

```
app/
  main.py      FastAPI routes: upload, review/save, ledger, CSV/JSON
  parser.py    Image prep + Claude vision call with a structured-output schema
  db.py        SQLite schema and queries (amounts stored as integer paise)
  config.py    Environment settings
  templates/   Jinja pages (mobile-first)
  static/      CSS
```

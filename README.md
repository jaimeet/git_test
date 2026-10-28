# Hisab

A small self-hosted web app for your daily hisab (account) sheet. Photograph the page with your phone, the app reads every line, you check and correct it, and it is saved to a ledger you can search, total, and export to CSV. It runs in Docker on a Raspberry Pi 5.

Photos can also arrive through WhatsApp: post the sheet photo in a chosen chat and Hisab reads it and replies with what it found (see [Connect WhatsApp](#connect-whatsapp)).

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

Create the data folder. The app runs as uid 1000 inside the container and takes ownership of `data/` on startup, so it doesn't matter who creates it:

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

Everything lives in `./data`: `hisab.db` (SQLite) and `images/`. If you use WhatsApp, also back up `./waha/sessions` to avoid scanning the QR code again. Copy that folder to back it up. For a consistent copy of the database while the app is running:

```bash
docker compose exec hisab python -c "import sqlite3; s=sqlite3.connect('/data/hisab.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
```

## Connect WhatsApp

Hisab can link to your own WhatsApp number as a linked device, the same way WhatsApp Web does. Photos posted in **one chat you choose** (a "Hisab" group, or your "Message yourself" chat) are read automatically. Hisab replies in that chat with the totals, anything that looks off, and a link to review the sheet. Photos in all your other chats are ignored.

The link uses [WAHA](https://github.com/devlikeapro/waha), an open-source WhatsApp bridge that runs as a second container next to Hisab.

> **Heads-up:** WAHA is unofficial. Linking an automated client to a personal number is against WhatsApp's terms of service. For one person reading their own photos the risk is low, but WhatsApp can ban numbers it flags. Hisab never sends anything except short replies in the chat you chose.

### Set it up

1. Generate two random secrets:

   ```bash
   openssl rand -hex 24
   ```

   Run it twice, then put the two values in `.env` as `WAHA_API_KEY` and `WHATSAPP_WEBHOOK_SECRET`. Also set `PUBLIC_URL` to the address you open Hisab at (for example `http://192.168.1.20:8000`), so reply links work.

2. Create the bridge's data folders and start both containers:

   ```bash
   mkdir -p waha/sessions waha/media && docker compose up -d --build
   ```

3. Open Hisab → **WhatsApp**. Once the bridge shows *waiting for scan*, open WhatsApp on your phone → **Settings → Linked devices → Link a device** and scan the QR code. The status changes to *connected*.

4. On your phone, open the chat you want to use and send:

   ```
   hisab here
   ```

   Hisab replies to confirm. From then on, every photo posted in that chat, by you or by anyone else in a group, becomes a sheet.

Other commands (only work when sent from your own phone): `hisab stop` stops reading photos, and `hisab status` says which chat is being read.

### How WhatsApp sheets are saved

By default a WhatsApp photo becomes a draft and the reply asks you to check it, the same as an upload. Set `WHATSAPP_AUTO_SAVE=true` to save it straight to the ledger when nothing looks off: the lines add up to the totals written on the sheet, the balances agree, and no line was hard to read. Sheets with any warning still wait for you. A caption on the photo (for example "27 Oct") is passed to the reader as a hint.

### WhatsApp troubleshooting

- **Status stays "failed" or "unreachable":** check `docker compose logs waha`. The Pi needs to reach `web.whatsapp.com`. If WAHA reports "API key", make sure `WAHA_API_KEY` in `.env` has a value and both containers were recreated after you set it.
- **Photos aren't picked up:** send `hisab status` in the chat. If Hisab doesn't reply at all, check `docker compose logs hisab` for `/whatsapp/webhook` requests. A `401` means `WHATSAPP_WEBHOOK_SECRET` doesn't match what the bridge was started with; run `docker compose up -d` again.
- **On x86_64** (not a Pi) set `WAHA_IMAGE_TAG=gows`.
- **Changed Hisab's port inside the container?** The bridge calls `http://hisab:8000/whatsapp/webhook`; update `WHATSAPP_HOOK_URL` in `docker-compose.yml` to match. Changing only the published (host) port needs nothing.
- **Unlink:** use **Unlink** on the WhatsApp page, or remove "Hisab" from Linked devices on your phone.

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
| `PUBLIC_URL` | empty | Address of the app, used for links in WhatsApp replies. |
| `WAHA_API_KEY` | empty | Shared key between Hisab and the WhatsApp bridge. |
| `WHATSAPP_WEBHOOK_SECRET` | empty | Shared secret that signs messages from the bridge. Webhooks without a valid signature are rejected. |
| `WHATSAPP_REPLY` | `true` | Reply in the chat with what was read. |
| `WHATSAPP_AUTO_SAVE` | `false` | Save WhatsApp sheets with no warnings straight to the ledger. |
| `WHATSAPP_CHAT_ID` | empty | Fix the chat by id instead of using `hisab here`. |
| `WAHA_IMAGE_TAG` | `gows-arm` | WAHA image tag; use `gows` on x86_64. |

## Privacy and cost

Sheet photos are sent to the Anthropic API to be read. The photos, the database and the ledger stay on your Pi. With WhatsApp connected, the bridge only downloads images, and only photos from the chosen chat are kept or sent for reading. The bridge's session files in `./waha/sessions` give access to your WhatsApp account, so keep them private. Each sheet is one API request, and you pay for its tokens at your API account's rates. The model's safety fallback (`fallbacks: "default"`) is turned on, so in the rare case the model declines an image, the API retries on its recommended fallback model.

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
  main.py      FastAPI routes: upload, review/save, ledger, CSV/JSON, WhatsApp pages + webhook
  ingest.py    Photo → draft sheet pipeline shared by uploads and WhatsApp
  whatsapp.py  WAHA client, webhook handling, chat commands, replies
  parser.py    Image prep + Claude vision call with a structured-output schema
  db.py        SQLite schema and queries (amounts stored as integer paise)
  config.py    Environment settings
  templates/   Jinja pages (mobile-first)
  static/      CSS
```

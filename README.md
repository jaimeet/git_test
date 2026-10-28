# Hisab

A small self-hosted web app for your daily hisab (account) sheet. Photograph the page with your phone, the app reads every line, you check and correct it, and it is saved to a ledger you can search, total, and export to CSV. It runs in Docker on a Raspberry Pi 5.

Photos can also arrive through WhatsApp: post the sheet photo in a chosen chat and Hisab reads it and replies with what it found (see [Connect WhatsApp](#connect-whatsapp)).

## How it works

1. **Upload.** Open the app on your phone and tap *Take photo*. The photo is kept on the Pi under `data/images/`.
2. **Read.** The photo is straightened (EXIF rotation), resized, and sent to an AI vision model: a **free local model on another computer** (via Ollama), **Google Gemini** (free API key), or **Anthropic Claude** (paid). The model returns structured lines: description, original text, party, in (jama) or out (naam/kharch), amount, category, and a confidence level. It also returns any opening/closing balances and column totals written on the sheet. Hindi, Gujarati, Marathi and English sheets work, including Indian-script digits and `1,25,000/-` style amounts.
3. **Review.** The photo appears next to an editable table. Lines the model was unsure about are highlighted. If the lines don't add up to the totals written on the sheet, or opening + in − out doesn't equal the closing balance, you get a warning. You can fix, add or remove lines.
4. **Save.** Only sheets you have reviewed and saved appear in the **Ledger**. The ledger has date and text filters, in/out/net totals, spending by category, and CSV export.

If the photo can't be read (no API key, no internet, unreadable image), the sheet is still created and you can type the lines in by hand.

## Run on a Raspberry Pi 5

You need Raspberry Pi OS (64-bit), DietPi, or another arm64 Linux, plus one AI option: a computer on your network running [Ollama](#run-the-ai-on-your-laptop-free-and-private), a **free Gemini key** from <https://aistudio.google.com/apikey>, or an Anthropic key from <https://console.anthropic.com>.

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

Set `OLLAMA_URL` (local model), `GEMINI_API_KEY` (free) or `ANTHROPIC_API_KEY` in `.env`. Set `APP_PASSWORD` too if the Pi can be reached from outside your home network.

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
| `GEMINI_API_KEY` | — | Free Google Gemini key for reading photos (get it at aistudio.google.com/apikey). |
| `ANTHROPIC_API_KEY` | — | Paid alternative: Anthropic key. |
| `AI_PROVIDER` | `auto` | `auto` uses the first configured of Ollama, Gemini, Claude. Or `ollama` / `gemini` / `claude`. |
| `OLLAMA_URL` | empty | Ollama on another computer, e.g. `http://192.168.1.50:11434`. |
| `OLLAMA_MODEL` | `qwen3-vl:8b` | Local vision model; must be pulled on that computer. |
| `OLLAMA_TIMEOUT` | `900` | Seconds to wait for one sheet. |
| `OLLAMA_NUM_CTX` | `16384` | Context size for the local model. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Gemini model; the default tracks Google's current free-tier Flash model. |
| `APP_USERNAME` / `APP_PASSWORD` | `hisab` / empty | HTTP basic auth for the whole UI. Off when the password is empty. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model used to read sheets. |
| `PARSE_EFFORT` | `high` | Claude only. `low` / `medium` / `high` / `xhigh` / `max`. Higher effort reads messy handwriting more carefully but costs more and takes longer. |
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

## Run the AI on your laptop (free and private)

A vision model can run on any always-on computer on your home network with [Ollama](https://ollama.com), and Hisab on the Pi sends it the photos. Nothing leaves your network and there is no cost.

**What the laptop needs:** at least 16 GB of RAM for the default `qwen3-vl:8b` (about 6 GB download), or 8 GB of RAM for `qwen3-vl:4b`. An NVIDIA GPU or Apple Silicon makes reading take seconds; on CPU alone expect one to several minutes per sheet. Hisab shows "Reading…" meanwhile and reads one photo at a time.

1. **Install Ollama** from <https://ollama.com/download> (Windows, macOS or Linux).
2. **Download the model** in a terminal or PowerShell on the laptop:

   ```bash
   ollama pull qwen3-vl:8b
   ```

3. **Let other devices connect.** Ollama only listens to the laptop itself by default. Set `OLLAMA_HOST` to `0.0.0.0:11434`, then restart Ollama:
   - **Windows:** Start → "Edit environment variables for your account" → New: name `OLLAMA_HOST`, value `0.0.0.0:11434`. Quit Ollama from the tray icon and start it again. Allow it through Windows Firewall on **private** networks when asked, or add an inbound rule for TCP port 11434.
   - **macOS:** `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"`, then quit and reopen the Ollama app.
   - **Linux:** `sudo systemctl edit ollama.service`, add `Environment="OLLAMA_HOST=0.0.0.0:11434"` under `[Service]`, then `sudo systemctl restart ollama`.
4. **Give the laptop a fixed address** in your router (DHCP reservation), so its IP doesn't change.
5. **Check from the Pi** that it can reach the laptop. This should list the model:

   ```bash
   curl http://LAPTOP_IP:11434/api/tags
   ```

6. **Point Hisab at it** in `.env`, then run `docker compose up -d`:

   ```
   OLLAMA_URL=http://LAPTOP_IP:11434
   OLLAMA_MODEL=qwen3-vl:8b
   ```

The home page shows which AI is reading ("Read by local model …").

**Trying other models:** local models vary a lot on handwriting, so test a few real sheets. `gemma4` is a good alternative (`ollama pull gemma4`, then set `OLLAMA_MODEL=gemma4`); bigger models read better but need more memory. Use **Read photo again** on a sheet to compare.

**Keep Ollama on your home network.** It has no password, so don't forward port 11434 on your router.

If the laptop is off, the sheet says Ollama couldn't be reached; tap **Read photo again** once it's back. If a long sheet comes back cut off, raise `OLLAMA_NUM_CTX` (uses more memory); if the laptop is slow, raise `OLLAMA_TIMEOUT`.

## Choosing the AI: local, Gemini (free) or Claude (paid)

**Local (Ollama), free and private.** See above. If `OLLAMA_URL` is set it is used first.

**Gemini, free.** Create a key at <https://aistudio.google.com/apikey> while signed in to your Google account, and put it in `.env` as `GEMINI_API_KEY`. No card is needed. The free tier allows a limited number of requests per minute and per day. That is plenty for a few sheets a day; your project's current limits are on AI Studio's rate-limit page. Note that a Gemini app subscription (Google AI Pro) does **not** include API access, so the free key is what Hisab uses either way. On the free tier, **Google may use what you send to improve its products**, so your sheet photos may be seen by Google.

**Claude, paid.** Set `ANTHROPIC_API_KEY` instead and you pay per sheet at Anthropic's API rates. Claude tends to be stronger on messy handwriting. With `AI_PROVIDER=auto` the first configured option wins: Ollama, then Gemini, then Claude. Set `AI_PROVIDER=ollama`, `gemini` or `claude` to force one.

To switch, edit `.env` and run `docker compose up -d`. Sheets that already failed can be re-read with **Read photo again**.

## Privacy and cost

With a local model, photos never leave your network. Otherwise they are sent to the AI provider you chose (Google or Anthropic) to be read. The photos, the database and the ledger stay on your Pi. With WhatsApp connected, the bridge only downloads images, and only photos from the chosen chat are kept or sent for reading. The bridge's session files in `./waha/sessions` give access to your WhatsApp account, so keep them private. A local model and Gemini's free tier cost nothing. With Claude, each sheet is one API request billed at your Anthropic account's rates; Claude's safety fallback (`fallbacks: "default"`) is on, so if the model declines an image the API retries on its recommended fallback model.

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

The tests use a fake parser for the web flow and drive the Ollama client and the real Gemini and Anthropic SDKs against mocked HTTP transports, so they need no API key or network.

Layout:

```
app/
  main.py      FastAPI routes: upload, review/save, ledger, CSV/JSON, WhatsApp pages + webhook
  ingest.py    Photo → draft sheet pipeline shared by uploads and WhatsApp
  whatsapp.py  WAHA client, webhook handling, chat commands, replies
  parser.py    Image prep + Ollama / Gemini / Claude vision call with a structured-output schema
  db.py        SQLite schema and queries (amounts stored as integer paise)
  config.py    Environment settings
  templates/   Jinja pages (mobile-first)
  static/      CSS
```

# Kagaz

Your documents, one WhatsApp message away. Send any document to the Kagaz WhatsApp number; later ask
*"send my 10th marksheet under 100 KB"* or *"what's my 12th roll number?"*. Or open your documents in a
browser: type **WEB** in the chat.

See [PLAN.md](PLAN.md) for the product plan, roadmap and monetization.

## What the MVP does

- **Save:** any PDF or photo is encrypted and stored. AI reads it once to give it a name and pull out
  details (roll no., certificate no., policy no.…).
- **Find:** plain-language requests in English, Hindi or Hinglish. If several documents match, the bot
  shows a pick list. If the AI is down or out of quota, it still finds documents by name.
- **Answer details:** "what's my 12th roll number?" → the number **plus the document**, to double-check.
- **Convert:** shrink to N KB, PDF ↔ JPG, merge into one PDF, resize photos/signatures to exact pixels.
- **Two-sided cards:** front and back photos (Aadhaar, DL, RC…) are matched automatically and sent as
  **one page with both sides**, like a photocopy. "aadhaar back" sends just one side.
- **"What is this?"** When the AI can't tell (blurry photo, damaged file, AI busy), the bot asks with a
  10-option list. A label can add protection but never remove it.
- **Re-reading:** documents the AI skipped while busy are read again automatically (5 min, 15 min,
  1 h, 3 h, 12 h later). Locally, run `python manage.py reread_documents`, or press "Try reading again"
  on the document's web page.
- **Forgot PIN:** `FORGOT PIN` starts a **24-hour delayed reset**. The owner can stop it with `CANCEL
  RESET`, and using the correct PIN meanwhile cancels it automatically. After 24 hours, `PIN` gives a
  link to set a new PIN without the old one, and it works even if locked out.
- **Web chat** (Django templates): the same conversation as WhatsApp, in the browser. It has bubbles,
  quick-reply chips, file cards with Download / Options, a paperclip for uploads, a PIN sheet that pops
  up for ID documents, and a document viewer. Opened from a one-time WhatsApp link plus the PIN.
- **Control:** LIST, WEB, PIN, FORGOT PIN, CANCEL RESET, DELETE MY DATA, plus consent before anything is stored.

### What goes in the chat vs. the secure page

| | In the WhatsApp chat | Only on the secure web page (link + PIN) |
|---|---|---|
| **Documents** | Marksheets, degrees, certificates (income, caste, domicile…), letters, bills, photos, signatures | Aadhaar, PAN, driving licence, passport, voter ID, ration card, bank papers, salary slips |
| **Details** | Roll no., certificate no., percentage, date of birth… | Any ID or bank number, even if it's printed on an ordinary document |

Why: WhatsApp's Business Messaging Policy forbids businesses from asking for, or sending, ID card and
bank numbers in chat (breaking it can get the whole Meta business blocked), and the PIN page is safer.

## Setup (Windows, PowerShell)

You need PostgreSQL running with an empty database for Kagaz.

```powershell
cd C:\Users\vaibh\Desktop\Kagaz
.venv\Scripts\Activate.ps1               # or call .venv\Scripts\python directly
pip install -r requirements.txt          # only needed on a fresh machine

# create .env (see "Your .env" below)
python manage.py generate_master_key     # paste the output line into .env
python manage.py migrate
python manage.py createsuperuser         # login for /admin/
```

> **Back up `KAGAZ_MASTER_KEY`.** Every stored document is encrypted under it. If you lose it, the
> documents are gone for good.

### Your .env

One `KEY=value` per line; put comments on their own line.

| Key | What |
|---|---|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `PUBLIC_BASE_URL` | Django basics. `PUBLIC_BASE_URL` starts every link the bot sends |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | Email (printed in the terminal while `EMAIL_HOST_USER` is empty) |
| `SUPPORT_EMAIL` | Shown in the bot's help: WhatsApp requires a way to reach a person |
| `KAGAZ_MASTER_KEY` | Encryption master key (`python manage.py generate_master_key`) |
| `AI_PROVIDER`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `AI_MODEL` | Which AI reads documents |
| `WHATSAPP_BACKEND` and `WHATSAPP_*` | `console` for the terminal chat; `cloud` plus Meta's values for real WhatsApp |
| `CSRF_TRUSTED_ORIGINS`, `REDIS_URL` | Only for ngrok/production |
| `ADMIN_URL` | Server only: a random word, so the admin lives at `/<word>/` instead of `/admin/` |

## Get a free Gemini API key

1. Open [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with a Google account.
2. Accept the terms. AI Studio creates a Google Cloud project for you.
3. Click **Create API key** and copy it.
4. Optional but recommended: on the key, choose **Add restrictions → Restrict to Gemini API only**.
5. In `.env`: `AI_PROVIDER=gemini` and `GEMINI_API_KEY=<your key>`.

The default model is `gemini-3.5-flash-lite` (the free tier allows the most requests per day on it).
To try another, set `AI_MODEL=gemini-3.8-flash` in `.env`. AI Studio shows your current free limits.

> ⚠️ **Free tier = test documents only.** On the free tier, Google may use what you send to improve
> its products, human reviewers may read it, and Google's terms say not to send sensitive or
> personal information. Use sample or dummy documents while developing. Before any real user sends a
> document, enable billing on that Google Cloud project (paid tier: not used for training) or switch
> to `AI_PROVIDER=claude`. Before going live, run `python manage.py check --deploy`; it warns about this.

## Try it in your terminal

```powershell
python manage.py runserver     # terminal 1: the website (PIN pages and web vault)
python manage.py chat          # terminal 2: you are a WhatsApp user
```

In the chat: type `hi`, then `/tap 1` to agree, `/file C:\path\to\marksheet.pdf` to send a file, then
ask for it back (`send my marksheet under 100kb`). Files the bot sends you land in `storage\outbox\`.
`/reset` erases the test user and starts fresh.

**Web chat:** type `web` in the chat and open the link in your browser. Enter (or create) your PIN, and
you're in. Asking for an ID document (`send my aadhaar`) also gives a link; it opens the chat with that
document as the newest message.

How web sign-in works:
- A link works once and expires in 10 minutes. It signs in only the browser that opens it.
- The session ends after 30 idle minutes. Tick "shared or cyber café computer" to make it 10 minutes
  and end when the browser closes.
- ID and bank items stay open for 5 minutes after a PIN. The **Lock** button in the header closes them
  at once.
- The web conversation lives in the signed-in session only. It disappears at sign-out, and nothing in it
  is stored as plain text: only document ids, so numbers are looked up fresh each time.
- Sign out from the ⋮ menu.

### The look ("paper and ink")

All styling is in `core/static/kagaz/kagaz.css`: colours are variables at the top, with a dark mode
under them. The chat behaviour is in `core/static/kagaz/app.js` (plain JavaScript, no build step), and
the icons are in `core/templates/_icons.html`. On a live server, run `python manage.py collectstatic`
and let Nginx serve `/static/` from `staticfiles/`.

## Background jobs (Huey) in plain words

Some work is slow: the AI reading a document takes 5–20 seconds. WhatsApp expects our server to answer
its webhook within a few seconds, so slow work is handed to a **job queue**:

- **Huey** is the job queue library. Code calls `tasks.process_inbound(id)`, and Huey decides when and
  where that runs.
- **Now (development):** Huey runs each job immediately inside the same process. You don't start
  anything extra, and no files are created.
- **Production:** set `REDIS_URL`. Jobs are then stored in **Redis** (a small, very fast database
  server), and a separate worker, `python manage.py run_huey`, picks them up. The website answers
  instantly while the worker does the slow part.

## Connect real WhatsApp (later)

1. Create an app at [developers.facebook.com](https://developers.facebook.com) → add **WhatsApp**.
   Start **Business verification** early; it can take days.
2. Copy the **Phone number ID**, a **permanent access token** (System User token) and the **App secret**
   into `.env`. Set `WHATSAPP_BACKEND=cloud` and choose any random `WHATSAPP_VERIFY_TOKEN`.
3. Run `ngrok http 8000`. Put the https URL in `PUBLIC_BASE_URL` and `CSRF_TRUSTED_ORIGINS`, and add
   `.ngrok-free.app` to `ALLOWED_HOSTS`.
4. In *WhatsApp → Configuration*, set the webhook to `https://<ngrok-url>/webhooks/whatsapp/` with the
   same verify token, and subscribe to **messages**.

## Tests

```powershell
python manage.py test
```

Tests use fake AI answers and a console WhatsApp client, so they cost nothing and need no network.
Django creates a temporary `test_<DB_NAME>` database and deletes it afterwards.

## Project layout

| Where | What it holds |
|---|---|
| `kagaz/settings.py` | Django settings. Secrets and per-machine values come from `.env` |
| `core/` | Encryption (`crypto.py`), the AI wrapper for Gemini or Claude (`llm.py`), home and privacy pages, test helpers |
| `accounts/` | `Member` (a WhatsApp user: consent, PIN, plan, encryption key), `AccessLink` (one-time links), `User` (admin staff), web sign-in (`sessions.py`), PIN pages, plan limits and PIN rules |
| `vault/` | `Document` (encrypted file and details), `Conversion`, AI reading (`extraction.py`), conversions (`conversions.py`), `services.py`, the web chat (`chat.py`, `views.py`, `templates/vault/`) |
| `bot/` | Webhook, WhatsApp client (`whatsapp.py`), message handling (`handlers.py`), AI intent (`intent.py`), all bot text (`replies.py`), background jobs (`tasks.py`), `chat` command |

## How data is protected

- One master key (from `.env`; a cloud KMS in production) encrypts a separate data key for each member.
  That data key encrypts the member's files and document details with AES-256-GCM.
- "Delete my data" deletes the member's key and files.
- The database holds only non-identifying columns in plain text (category, size, dates). Admin pages
  never show file contents or document details.
- ID and bank documents, and ID/bank numbers on any document, never go inside WhatsApp; they open on the
  web after the PIN. Previews are JPEGs we generate ourselves, and vault pages are never cached.
- Web uploads stay in memory until encrypted; no unencrypted copy is written to disk.
- PINs are hashed like passwords. There are 5 tries, then a 30-minute lock. PIN links are single-use,
  expire after 10 minutes, and only their hash is stored.
- The admin sits at a secret address (`ADMIN_URL`). 5 wrong passwords from one address lock its sign-in
  for 15 minutes, even if the next password is right.

**Honest limits:** whoever controls the server and the master key can technically decrypt data, and
WhatsApp (Meta) and the AI provider see documents in transit. Don't advertise "nobody can ever see
your data". See PLAN.md → Security.

# Kagaz: Product & Build Plan

_Last updated: 14 Oct 2026_

## 1. The idea in one paragraph

People in India keep important documents scattered across their phone gallery, laptop folders and
WhatsApp "send to self" chats. When a form needs a marksheet, PAN or certificate, they waste time
searching, old WhatsApp media is often "missing", and then they still have to shrink the file to
"under 100 KB" at a cyber café. **Kagaz is a WhatsApp number that keeps every document safe, finds it
from a plain request, and returns it in the size and format the form needs.**

**Why it can win:** no app to install; it solves an urgent need (form deadlines); and the
resize/convert feature is useful on day one, even before someone trusts us with their whole vault.

**Where we don't compete:** DigiLocker (free, government-issued documents, has its own WhatsApp bot).
Our edge is everything else: university degrees, offer and experience letters, rent agreements,
insurance, medical reports, photos and signatures. Plus plain-language Q&A, conversions, and later family.

---

## 2. What is built now (MVP code in this repo)

| Feature | Status |
|---|---|
| WhatsApp webhook with signature check, duplicate-safe | ✅ |
| Consent before storing anything; files sent earlier are held, then processed | ✅ |
| Save PDFs and photos, encrypted per member (AES-256-GCM, envelope keys) | ✅ |
| AI reads each document: title, holder, issuer, dates, numbers, keywords | ✅ |
| Duplicate detection (keyed hash) | ✅ |
| Plain-language requests (English/Hindi/Hinglish) → send document | ✅ |
| "What is my X number?" answers (value sent alone, easy to copy) | ✅ |
| Pick list when several documents match | ✅ |
| Compress to N KB, PDF↔JPG, merge to one PDF, resize to exact pixels | ✅ |
| PIN for sensitive documents, entered on a one-time web page | ✅ |
| ID/bank documents and ID/bank numbers never sent in WhatsApp (policy-safe): link → PIN → web | ✅ |
| "What's my roll number?" → the number plus the document, to double-check | ✅ |
| Web vault (Django pages): Ask box, list, details, preview, download with size/format, upload, delete | ✅ |
| WEB command, sign-in sessions with idle timeout and shared-computer mode | ✅ |
| Name search when the AI is down or out of quota; automatic retries on Gemini "busy" errors | ✅ |
| Front + back of a card matched and sent as one page; "front/back only" on request | ✅ |
| "What is this?" type list when the AI can't tell; labels can't remove protection | ✅ |
| Automatic re-reading of documents skipped while the AI was busy (+ manual command / web button) | ✅ |
| Forgotten PIN: 24-hour delayed reset, cancellable, auto-cancelled by correct PIN use | ✅ |
| LIST, HELP, PIN, PRIVACY, DELETE MY DATA (PIN-protected), delete one document | ✅ |
| Free plan limits (documents, conversions per month) | ✅ |
| Admin that never shows document contents | ✅ |
| Terminal chat simulator (`manage.py chat`) to test without Meta | ✅ |
| 116 automated tests | ✅ |

**Next up:** optional instant PIN reset by email code, WhatsApp Flows for PIN entry (needs real
WhatsApp), unlocking password-protected PDFs (e-Aadhaar, bank statements), exam photo/signature presets.

**Not built yet** (see roadmap): payments, expiry reminders, family vault, voice notes, replies in
Hindi, web dashboard/export, PIN recovery, password-protected PDF unlock, KMS, S3, monitoring.

---

## 3. How it works

```
 WhatsApp user
      │  message / file
      ▼
 Meta Cloud API ──webhook──► Django  /webhooks/whatsapp/
                               │  verify signature, save InboundMessage (unique id)
                               ▼
                         Huey job queue ──► bot/handlers.py
                                              │
          ┌───────────────────────────────────┼───────────────────────────────┐
          ▼                                   ▼                               ▼
   vault/services.save_document      bot/intent.parse (AI)          perform(): PIN check
   encrypt + store blob              catalog = titles + field       │ sensitive? → one-time
   vault/extraction (AI) reads it    NAMES only, never values       │ link /k/<token>/ (web PIN)
                                                                    ▼
                                                    vault/conversions → send document back
```

**Stack:** Django 6.1 · PostgreSQL · Huey (jobs run in-process in dev, Redis + worker in prod) ·
Gemini or Claude for reading and understanding (`AI_PROVIDER`) · Pillow + pypdf + pypdfium2 +
img2pdf (all permissive licenses) · WhatsApp Cloud API.

### Data model

| Model | Purpose | Encrypted? |
|---|---|---|
| `accounts.Member` | WhatsApp user: wa_id, plan, consent time, PIN hash, lockout, unlock window | Holds the member's wrapped data key |
| `accounts.AccessLink` | One-time PIN page: purpose, pending action, expiry | Only the token's hash is stored |
| `accounts.User` | Admin/staff login | n/a |
| `vault.Document` | One file: category, status, size, sensitivity, expiry date | File + details (title, holder, numbers, summary) encrypted |
| `vault.Conversion` | Each conversion done (for limits and stats) | No personal data |
| `bot.InboundMessage` | Webhook log, makes retries safe | Message body deleted after processing; row deleted after 30 days |

---

## 4. Security: what we promise, honestly

**We can promise:**
- Each member's files and details are encrypted with their own key; keys are wrapped by a master key.
- Staff tools (admin) cannot open documents.
- ID, bank and salary documents, and sensitive numbers, need a PIN. The PIN is typed on a one-time
  web page, never in chat (anyone holding the phone could scroll back and read it).
- "Delete my data" really erases files, details and the member's key.
- Data is not used for AI training; message bodies aren't kept; logs never contain document contents.

**We must NOT promise "nobody can see it, not even us".** That would be false, because:
- The server needs to decrypt to read, convert and send documents.
- WhatsApp Cloud API is hosted by Meta, which decrypts messages before handing them to us.
- The AI provider sees each document once when reading it.

**Threats and what we do about them:**

| Threat | Mitigation now | Later |
|---|---|---|
| Someone uses the owner's unlocked phone / WhatsApp Web | PIN on web page for sensitive docs and wipe; 5 tries then 30-min lock | PIN recovery via verified email; escalating lockouts; alert owner on lockout |
| Database leak | Files and details encrypted; keyed hashes only | Master key in cloud KMS; separate backup keys |
| Server compromise | Secrets only in `.env`; admin on a custom path | KMS, least-privilege IAM, Sentry alerts, security audit before 1,000 users |
| Fake webhook calls | HMAC signature check (`X-Hub-Signature-256`) | Rate limiting per number |
| Text inside a document tries to steer the AI | AI only returns schema-validated JSON; it can only pick the member's own documents | Evals on tricky documents |
| AI misreads a number and user fills a form wrong | Reply always names the source document | "Reply WRONG to report", show a cropped image of the number |

---

## 5. Roadmap

### Phase 0: this week
- [ ] Fill `.env`, run tests, play with `manage.py chat` using real documents (yours, family's).
- [ ] Start **Meta Business verification** and display-name approval (slowest step).
- [ ] Check the name: "Kaagaz Scanner" already exists in India; do a trademark search.
- [ ] Talk to 20 people (students filling exam forms, job seekers, parents). Key question: *would you
      send your Aadhaar to a WhatsApp bot? What would make you trust it?*

### Phase 1: pilot launch (weeks 1–8)
- [ ] Week 1–2: Meta app + webhook live via ngrok, then deploy to a small server (Render/Railway/VPS
      in Mumbai) with PostgreSQL, Redis and S3 (ap-south-1).
- [ ] Week 2: test AI accuracy on 50 messy Indian documents (blurry photos, Hindi text, old
      marksheets) with Gemini Flash-Lite, Gemini Flash, Claude Haiku/Sonnet/Opus. Pick the cheapest
      model that's accurate (`AI_PROVIDER` + `AI_MODEL`). Use dummy documents on Gemini's free tier;
      real ones only on a paid tier.
- [ ] Before the first real user: move off Gemini's free tier (enable billing, or use Claude). The
      free tier lets Google use and human-review submitted content.
- [ ] Week 3: add exam presets ("SSC photo", "signature 10–20 KB") as an admin-editable model,
      checked against the latest official notifications.
- [ ] Week 3: fallback search when the AI is down (simple keyword match on titles).
- [ ] Week 4: password-protected PDFs (e-Aadhaar, bank statements): ask for the password on a one-time
      page, unlock, store the unlocked copy.
- [ ] Week 4: Sentry error tracking, daily encrypted DB backup, uptime check on `/healthz/`.
- [ ] Week 5–6: private pilot with 30–50 users from your network; watch metrics (section 8) weekly.
- [ ] Week 7–8: fix what the pilot shows; lawyer review of privacy policy and terms; launch publicly.

### Phase 2: months 3–6 (retention + first revenue)
- Expiry reminders (passport, licence, insurance, PUC) using WhatsApp utility templates.
- Family vault: one account manages parents' and children's documents (Family plan).
- Voice notes ("mera 10th ka marksheet bhejo") via speech-to-text.
- Replies in Hindi when the user writes in Hindi.
- Razorpay payments inside WhatsApp (UPI) for Plus/Family and pay-per-use packs.
- Web login with OTP: view, export everything as a ZIP, manage PIN.
- PIN entry via WhatsApp Flows, so users never leave the chat.

### Phase 3: months 6–12 (scale + B2B)
- DigiLocker import (needs approval as a DigiLocker partner).
- Time-limited share links ("send my documents to HR"), with consent and expiry.
- B2B "request documents" for CA firms, colleges, HR onboarding.
- Bulk import: forward 50 old files at once and get one summary.
- Independent security audit; move master key to KMS; SOC2-style controls if selling to businesses.

---

## 6. Monetization

**Principle:** Indians don't pay for "storage", they pay when they urgently need something (the
cyber café charges ₹20–50 to resize a file). Price for those moments, keep the vault generous, and
sell annual plans because people need documents only a few times a year.

| Plan | Price | What's included |
|---|---|---|
| Free | ₹0 | 25 documents, unlimited retrieval, 5 conversions/month |
| Plus | ₹399/year (or ₹49/month) | Unlimited documents and conversions, expiry reminders, priority support |
| Family | ₹699/year | Plus for up to 5 people, managed from one number |
| Pay-per-use | ₹9–19 per conversion, ₹49 "Exam Form Pack" | For people who won't subscribe; pay by UPI in chat |

**Later B2B (bigger money, year 2):**
- CA firms and tax consultants: collect client documents on WhatsApp during ITR season,
  ₹500–2,000/month per firm.
- Colleges, coaching institutes, HR teams: request documents with the person's consent, priced per
  request or per seat.
- Cyber cafés and CSC centres: bulk resize/format tool.

**Opt-in partner offers (be careful):** e.g. "Your car insurance expires in 15 days. Want renewal
quotes?" can earn referral commissions. Only with explicit opt-in. Never share documents, never sell
data, never show ads based on document contents. One privacy scandal ends a vault product.

**When to turn payments on:** after ~500 weekly active users, or as soon as the free limits are
regularly hit (the code already tracks both).

### Unit economics (rough, check current prices)

| Cost | Per unit | Notes |
|---|---|---|
| AI: read one document | Opus 5.5 ≈ ₹2 · Sonnet 5.5 ≈ ₹1 · Haiku 5.5 ≈ ₹0.05 | Once per document |
| AI: understand one request | Opus ≈ ₹1.5 · Sonnet ≈ ₹0.75 · Haiku ≈ ₹0.04 | Commands like LIST/HELP use no AI |
| WhatsApp replies to the user | Free | Inside the 24-hour window after their message |
| WhatsApp reminders (utility template) | ≈ ₹0.12 each | Phase 2 |
| Storage (S3 Mumbai) | ≈ ₹2 per GB-month | ~50 MB per user |

A free user with 25 documents costs roughly ₹50 one-time on Opus, or ₹1–2 on Haiku. Model choice
matters a lot at scale, so test accuracy first (Phase 1, week 2), then decide. One Plus subscriber at
₹399/year covers many free users on Haiku/Sonnet.

**Fixed monthly costs at pilot scale:** server ₹1,500–4,000, managed PostgreSQL ₹1,500, Redis ₹500,
domain/email ₹200. Budget ~₹8,000/month plus AI.

---

## 7. Meta / WhatsApp checklist

- [ ] Meta Business account + Business verification (GST/business proof).
- [ ] Display name approval ("Kagaz").
- [ ] Permanent System User token (not the 24-hour test token).
- [ ] Privacy policy URL (`/privacy/`) in the app settings.
- [ ] Webhook subscribed to `messages`.
- [ ] Keep the bot document-only: Meta's terms ban general-purpose AI chatbots on the Business API.
      The bot already politely refuses off-topic requests.
- [ ] Plan for platform risk: the backend doesn't depend on WhatsApp, and the web login (Phase 2)
      gives users another way in.

---

## 8. Metrics to watch (weekly)

| Metric | Target in pilot |
|---|---|
| Activation: consented and saved ≥3 documents in week 1 | > 50% |
| Retrieval success: right document sent within one request | > 85% |
| AI read rate: documents READY vs UNREADABLE | > 90% |
| Conversion success: hit the requested size | > 95% |
| Weekly users who retrieve something | grows week on week |
| 30-day retention | > 30% |
| Later: free → paid | 2–5% |

---

## 9. Legal & compliance checklist (get a lawyer before public launch)

- [ ] DPDP Act 2023 + DPDP Rules: clear consent notice (done in chat), purpose limitation, erasure on
      request (done), breach notification process, grievance officer contact on the privacy page.
- [ ] Privacy policy and terms reviewed (current `/privacy/` page is a draft).
- [ ] Disclose that AI processing may happen outside India; sign the AI provider's data processing terms.
- [ ] Aadhaar: store only what the user sends, show masked numbers, never use Aadhaar for
      authentication.
- [ ] Set up a business entity and current account before taking payments (payment gateways need business KYC).

---

## 10. Risks

| Risk | Likelihood | What we do |
|---|---|---|
| People don't trust a bot with ID documents | High | Start with conversions (no trust needed); clear privacy page; PIN; "delete my data"; publish a plain-language security page |
| Meta changes rules or blocks the number | Medium | Stay document-only; web login as a backup channel; backend not tied to WhatsApp |
| Rare use → low retention | High | Expiry reminders, family vault, annual pricing |
| AI misreads a number | Medium | Always cite the source document; "report wrong" flow; accuracy testing |
| Data breach | Low, but fatal | Encryption per member, KMS, audit, minimal data, fast breach response plan |
| DigiLocker adds conversions/Q&A | Medium | Move faster on non-government documents, family and B2B |

---

## 11. Decisions to make soon

1. **AI model** (cost vs accuracy) after testing on real documents.
2. **Hosting**: Render/Railway (easy) vs AWS Mumbai (data residency, more work).
3. **Name/trademark**: keep "Kagaz" or pick a distinct name.
4. **Pilot group**: which 30–50 people first (students? job seekers? families?).

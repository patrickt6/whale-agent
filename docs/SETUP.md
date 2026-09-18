# whale-agent setup

Everything here is optional. With an empty config the agent already ingests SEC EDGAR,
Taiwan TWSE and Dataroma, ranks what clears the $5M gate, and prints a digest to your
terminal. Each section below turns on one more thing.

Work top to bottom and you will have a free daily email digest with AI-written prose in
about fifteen minutes, with FMP and Quiver left switched off until you decide to buy
them.

---

## 0. First run — prove it works before configuring anything

```bash
cd whale-agent   # your clone
.venv/bin/python -m pip install -e ".[dev]"     # first time only

./whale test              # everything should pass
./whale digest --demo     # offline: no network, no keys, no database writes
```

`./whale` is the launcher for every job: `./whale digest`, `./whale weekly`,
`./whale watchdog`, `./whale test`. Use it rather than `python -m whale_agent...` —
on Homebrew Python 3.14 the editable install's path file is not honoured, and the
launcher sets `PYTHONPATH` explicitly so it always works.

Then create your config:

```bash
cp .env.example .env
```

> **Put your keys in `.env`, never in `.env.example`.** `.env.example` is committed to
> git; `.env` is gitignored. A password pasted into the example file gets published the
> next time you commit.

At any point, ask the agent what it thinks is switched on:

```bash
./whale digest --show-config
```

That command is the answer to "why is source X missing from my digest?" — every source
that is off tells you exactly which variable would turn it on.

---

## 1. SEC EDGAR — free, no key, already on

The SEC requires a real contact string in the User-Agent of every request. This is not
optional; requests without one get blocked.

```
WHALE_SEC_USER_AGENT="Your Name you@example.com"
```

That is all EDGAR needs. Form 4 and SC 13D/G ingestion works immediately.

---

## 2. Gemini API key — free, powers the digest prose

The prose layer writes the "why it matters" sentence on each row and the two-sentence
summary at the top. It **cannot** touch any number: every sentence it produces is
checked against the structured filing data, and anything containing a figure that does
not trace back to a field is silently discarded. Worst case the digest reads exactly as
it does with the LLM switched off.

Google's free tier is generous enough that a daily digest costs nothing.

1. Go to **https://aistudio.google.com/apikey** and sign in with a Google account.
2. Click **Create API key**. If it asks for a project, let it create one — you do not
   need to set up billing, and the free tier does not require a credit card.
3. Copy the key (it starts with `AIza...`).
4. Put it in `.env`:

```
WHALE_LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...your key...
GEMINI_MODEL=gemini-2.5-flash
```

`gemini-2.5-flash` is the right default: fast, cheap, and this task is summarisation
rather than reasoning. `gemini-2.5-pro` also works if you want to spend the quota.

**Free-tier limits.** Google publishes per-minute and per-day request caps that change
periodically; a once-a-day digest is far below any of them. If you ever exceed one, the
run logs a warning and ships the deterministic digest — nothing breaks.

**To use Anthropic instead**, get a key from https://console.anthropic.com/settings/keys
(this one is paid, roughly $10–30/month at this volume) and set:

```
WHALE_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

Both providers implement the same interface, so switching is a one-line change with no
difference in behaviour or safety checks.

**To disable prose entirely:** `WHALE_LLM_PROVIDER=none`, or run with `--no-llm`.

---

## 3. Email delivery — Gmail App Password

This is the fastest path to a digest actually landing in your inbox: no domain, no
signup, no verification. It uses your existing Gmail account over SMTP.

You cannot use your normal Gmail password — Google blocks that. You need a 16-character
**App Password**, which requires 2-Step Verification to be on.

1. Turn on 2-Step Verification: **https://myaccount.google.com/signinoptions/two-step-verification**
   (skip if already on).
2. Go to **https://myaccount.google.com/apppasswords**.
   - If that page says App Passwords are unavailable, 2-Step Verification is not fully
     enabled yet. Finish step 1 and come back.
3. Give it a name like `whale-agent` and click **Create**.
4. Google shows a 16-character password like `abcd efgh ijkl mnop`. Copy it. You cannot
   view it again — generate a new one if you lose it.
5. Put it in `.env` (spaces are fine, or strip them — both work):

```
WHALE_EMAIL_PROVIDER=smtp
WHALE_EMAIL_TO=you@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=abcdefghijklmnop
```

`WHALE_EMAIL_FROM` is optional — it defaults to `SMTP_USERNAME`.

Test it — this sends a real email built from the offline fixture, so it exercises the
whole mail path without a live SEC pull:

```bash
./whale digest --demo --test-email
```

The last line says `[smtp] sent to 1 recipient(s)` or `[smtp] NOT sent: <reason>`. Check
your inbox; the message is titled `WHALE DIGEST — <today>`.

Once that works, the real run:

```bash
./whale digest
```

**Multiple recipients:** comma-separate them in `WHALE_EMAIL_TO`.

**Gmail sending limits** are around 500 messages/day for consumer accounts — irrelevant
for one digest a day.

### Alternative: Resend

Better deliverability and a proper unsubscribe story, at the cost of owning a domain.
Sign up at https://resend.com, verify a sending domain, create an API key, then:

```
WHALE_EMAIL_PROVIDER=resend
RESEND_API_KEY=re_...
WHALE_EMAIL_FROM=alerts@yourdomain.com     # required — must be on the verified domain
WHALE_EMAIL_TO=you@example.com
```

Free tier is 3,000 emails/month, 100/day.

---

## 4. Instant alerts (optional)

The daily digest is the product. These are for the three-or-four-times-a-year events
that are worth interrupting you for: a position over $100M, a first-time activist 13D by
a fund you would recognise, or three-plus insiders buying the same name.

### Telegram — free, 2 minutes

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
2. It replies with a token like `123456789:AAF...`. That is `TELEGRAM_BOT_TOKEN`.
3. Send any message to your new bot (it cannot message you first).
4. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[0].message.chat.id`. That is `TELEGRAM_CHAT_ID`.

```
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=987654321
```

### SMS via Twilio — pay-as-you-go, ~$5

Sign up at https://twilio.com, buy a number, then take the Account SID and Auth Token
from the console dashboard:

```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+15551234567     # the number you bought
TWILIO_TO_NUMBER=+15559876543       # your phone
```

---

## 5. Free data sources

### Taiwan (TWSE OpenAPI) — no key, already on

Keyless and public. Just identify yourself politely:

```
WHALE_ENABLE_TAIWAN=1
TAIWAN_MOPS_USER_AGENT="Your Name you@example.com"
```

One caveat worth understanding: Taiwan publishes **share counts and percentages, never
dollar values**. To convert those into USD the agent needs a share price, which comes
from FMP (section 6). Without FMP these filings are still ingested and stored, but they
cannot clear the $5M gate, so they will not appear in the digest. Switch FMP on later
and subsequent filings are valued with no re-ingestion needed.

### Japan (EDINET) — free, but needs a registered key

EDINET's API is free and covers roughly ten years of 5% Rule large-shareholding reports,
but it requires registration.

1. Go to **https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1** (the EDINET API
   registration page; reachable from https://disclosure2.edinet-fsa.go.jp under "API").
2. Register with an email address and accept the terms. The site is Japanese; browser
   translation handles it fine.
3. You receive a **Subscription Key**.

```
JAPAN_EDINET_API_KEY=your-subscription-key
WHALE_ENABLE_JAPAN=1
```

Filings are Japanese-only. Names are stored verbatim; only the prose layer renders them
into English, and it is never allowed near the numbers.

### Dataroma — no key, on by default

Free superinvestor 13F portfolios and a significant-insider-buys feed. **Read this
before relying on it:** Dataroma has no API, so the agent scrapes HTML. It is owned by
Morningstar and its terms of use may not permit that. Treat it as personal ingestion,
never redistribute it, and keep the request rate low. To switch it off:

```
WHALE_ENABLE_DATAROMA=0
```

### Crypto — Arkham and Whale Alert, both free tier

Both have free tiers, and both still issue an API key.

- **Arkham**: https://platform.arkhamintelligence.com — create an account, then generate
  an API key from settings. Arkham's value is the entity layer: it names the fund or
  exchange behind a wallet.
- **Whale Alert**: https://whale-alert.io/ — sign up and request a free API key from the
  dashboard. Covers ~30 chains.

```
ARKHAM_API_KEY=...
WHALE_ALERT_API_KEY=...
WHALE_ALERT_MIN_USD=5000000
```

Both sources are deliberately down-weighted in scoring. An on-chain transfer is not a
disclosed position: nobody filed anything, the "owner" is a heuristic label, and an
exchange-to-exchange move of $50M usually means nothing. Transfers that are unlabelled
on both ends are dropped entirely.

---

## 6. Paid vendors — leave these off until you want them

Both are fully implemented and fully tested. They stay off because their keys are blank,
and the digest names them in its coverage note so you always know what you are not
seeing. Nothing else changes when you buy one — paste the key, and the next run includes
it.

### FMP (Financial Modeling Prep)

**This is the one that matters most**, and not for the reason you would guess. Beyond
insider and congressional data, FMP is the **price provider**. Any filing that reports a
share count or a percentage rather than a dollar amount — every Taiwan filing, every
Japan filing, most 13D/G cover pages — cannot be valued in USD without it. Turning FMP
on is what makes the non-US half of this product work.

1. https://site.financialmodelingprep.com/developer/docs/pricing
2. Pick a plan. Insider and congressional endpoints appear on the mid tiers;
   institutional 13F extracts are Ultimate-tier. Prices change, so check the page.
3. Copy the API key from your dashboard.

```
FMP_API_KEY=your-key
WHALE_ENABLE_FMP=1
```

Verify: `--show-config` should show `[ON ] FMP insider trades`.

To pause it without deleting the key (e.g. to conserve API calls):
`WHALE_ENABLE_FMP=0`.

### Quiver Quantitative

Congressional trades, corporate lobbying spend, and federal contract awards. Roughly
$30–75/month depending on tier.

1. https://www.quiverquant.com/pricing/ and subscribe to the API tier.
2. Copy your token from the account page.

```
QUIVER_QUANT_API_KEY=your-token
WHALE_ENABLE_QUIVER=1
```

Note what Quiver is for. Congressional **trades** flow into the daily digest like any
other event — though they are disclosed as dollar *ranges* (`"$1,001 - $15,000"`), so the
agent uses the range's **lower bound** and marks the figure an estimate. A midpoint would
be a number nobody disclosed. Most congressional trades are far below $5M and correctly
never appear.

Lobbying spend and government contracts are **not** whale events — they are quarterly,
company-level, and nobody is taking a position. They are routed to the weekly report as
context, never to the daily digest and never to an instant alert.

FMP also covers congressional trades. Running both is harmless: identical trades collapse
on the deduplication key.

---

## 7. Running it on a schedule

### Local (macOS)

```bash
crontab -e
```

```cron
# Daily digest, 08:00 local
0 8 * * *  <repo>/whale digest   >> ~/whale.log 2>&1
# Weekly report, Sunday 09:00
0 9 * * 0  <repo>/whale weekly   >> ~/whale.log 2>&1
# Watchdog, hourly — separate on purpose: a crashed digest job cannot report its own absence
0 * * * *  <repo>/whale watchdog >> ~/whale.log 2>&1
```

Set `DIGEST_DEADLINE_HOUR_UTC` to a couple of hours after your digest time, converted to
UTC. Eastern time is UTC-4 in summer, so an 08:00 ET digest with a 10:00 ET deadline is
`DIGEST_DEADLINE_HOUR_UTC=14`.

### GitHub Actions

Works well and costs nothing, but the SQLite database lives in the runner and disappears
between runs — which breaks first-time-filer detection and alert deduplication. Either
commit the database back to the repo (ugly but functional) or move to Postgres by
setting `DATABASE_URL`.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A source is missing from the digest | `--show-config`. Every off source states the variable that enables it. |
| `[smtp] NOT sent: ... Username and Password not accepted` | You used your Gmail login password. It must be a 16-character App Password (section 3). |
| App Passwords page unavailable | 2-Step Verification is not fully enabled on the account. |
| Digest has no prose | `GEMINI_API_KEY` unset, or the model returned a number that failed the provenance check. Run with `-v` to see which. |
| Taiwan/Japan rows never appear | Expected without FMP: those filings report percentages, not dollars, so they cannot be valued or gated (section 5). |
| `HTTP 403` from SEC | `WHALE_SEC_USER_AGENT` is missing a real contact string. |
| Congressional trades look too small | They are disclosed as ranges; the lower bound is used deliberately. Not a bug. |
| Everything is empty and there are no coverage notes | Check `WHALE_SOURCES` — a stale allowlist silently narrows the run. |

---

## 9. What this is not

An awareness tool, not a trading system, and not investment advice. Every figure in the
digest is copied from a filing field rather than computed by a model, which is the whole
point of the provenance gate. The weekly report carries a permanent evidence block
explaining how weak this data actually is — insider signal was only ever in
*opportunistic* trades, concentrated in small caps, and has decayed since the research
was published; 13F data is up to 135 days stale; and the Taiwanese evidence specifically
found no reliable abnormal returns. Read it before you act on any of this.

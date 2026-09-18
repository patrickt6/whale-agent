# Public newsletter (WA-10, ticket #21)

This page covers the free public brief: how people subscribe, how the send works, which
email provider to use, what Patrick must set up, and how it grows. Prices and limits were
read from the provider pages on 2026-09-17. Each one has its URL. Anything without a URL
is marked unverified.

## How it works

1. A reader enters an address on the site. The Worker (`deploy-site/src/worker.js` on
   branch `wa21-newsletter-worker`) stores a pending record in KV with a consent
   timestamp and, if `IP_HASH_SALT` is set, an HMAC hash of the IP address.
2. If a sender is configured, the Worker emails a confirmation link built from the site
   origin. If no sender is configured, the record stays pending and nothing is sent.
3. The reader opens the link. The record becomes confirmed, with a second timestamp and
   IP hash.
4. Each Monday the `newsletter` GitHub workflow runs `whale newsletter send --cadence
   weekly`. The command fetches confirmed subscribers from `GET /api/subscribers`,
   renders the brief once from free sources, runs the egress screen, then sends one
   message per subscriber.
5. Every message has a personal unsubscribe link and the headers
   `List-Unsubscribe: <https://.../api/unsubscribe?token=...>` and
   `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. A mail client that supports
   RFC 8058 sends a POST to that URL. The Worker deletes the record and always answers 200.

### Free sources only

`jobs/newsletter.py` turns off FMP, Quiver, Arkham, Whale Alert, Finnhub and Unusual
Whales, and it also clears their keys. Clearing the keys matters: `build_price_provider`
in `jobs/pipeline.py` builds an FMP price provider from the key alone. The LLM is `none`
unless `WHALE_LLM_PROVIDER=gemini` and a Gemini key are both present. Anthropic and
OpenAI keys are cleared.

The brief is collected into a throwaway in-memory store, not the shared corpus. The
corpus also holds rows from paid feeds, and the public copy must not show them. The
cost of this choice: the weekly public brief only sees what the free adapters return
on the day of the run, not a full week of stored history.

### Safety checks in the send

- The egress screen (`find_egress_violations`) runs on the shared render, then again on
  each personalised message after the footer and unsubscribe link are added. A block
  stops the whole run with exit code 3. No one gets a partial send.
- A real send refuses to start without an `https://` API URL, the admin token, and
  `WHALE_NEWSLETTER_FOOTER`.
- The From address must be on your own domain. A real send refuses a free-mail sender
  such as gmail.com. Set `WHALE_NEWSLETTER_REPLY_TO` to send replies somewhere else.
- Sends go one per recipient, with a pause between messages
  (`WHALE_NEWSLETTER_PAUSE_S`, default 0.2 s, so 5 per second) and a longer pause
  between batches (`WHALE_NEWSLETTER_BATCH_SIZE`, default 50).
- HTTP 408, 409, 425, 429 and 5xx get up to 4 attempts with backoff of 1, 2 and 4 s.
  Other errors fail that one recipient. After 5 failures in a row the run stops, which
  covers a provider outage or a spent daily quota.
- The sent log is JSONL keyed by cadence, period and an HMAC-SHA256 of the address. The
  key is `WHALE_NEWSLETTER_HASH_KEY`, or the admin token when that is not set. The log
  sits in the Actions cache, and a plain hash of an address is easy to reverse, so it is
  keyed. It stores no plain addresses. A rerun in the same period skips anyone already sent, and
  retries anyone who failed.

### Known gaps

- The sent log lives in the GitHub Actions cache (`actions/cache` restore and save, the
  save step runs with `if: always()`). If the cache entry is evicted, a rerun in the
  same week can send twice. How long GitHub keeps an unused cache entry: unverified.
- The run does not check that report links in the brief resolve. The weekly private job
  does that check inside `run_weekly_digest`; the newsletter render does not call it.
- `/api/subscribers` and `/api/stats` read every `sub:` key, one KV read per subscriber.
  See the scaling section.
- `stat:unsubscribed` is a best effort counter. KV has no atomic increment, so two
  unsubscribes at the same moment can count as one.

## Provider comparison

| | Cloudflare Email Service | Resend | Amazon SES | Buttondown | Postmark |
|---|---|---|---|---|---|
| Newsletter (bulk) mail allowed | No. "Email Service is intended only for transactional emails." [1] | Yes, it sells marketing plans [2]. Whether bulk mail through the plain Emails API is allowed: unverified | Yes. The production access form asks you to pick Marketing or Transactional [6] | Yes, it is a newsletter service [9] | Yes, on a Broadcast message stream [12] |
| Free tier | None on Workers Free. Workers Paid includes 3,000 per month [3] | 3,000 per month, 100 per day [2] | No SES free allowance. New AWS accounts get up to $200 in general credits [7] | Free for the first 100 subscribers [9] | 100 per month [11] |
| Price after that | $0.35 per 1,000 on Workers Paid [3]. Base Workers Paid price: unverified | Pro $20 per month for 50,000; $35 for 100,000 [2] | $0.10 per 1,000 a la carte [7] | Price at 1,000 and 10,000 subscribers: unverified (the page is a calculator) [9] | $15 per month for 10,000 on Basic, then $1.80 per 1,000 [11] |
| Own domain needed | Yes [4] | Yes, "You must add and verify at least one domain" [5] | Yes for production (details not rechecked: unverified) | No, a custom domain is optional [10] | Yes, DKIM and Return-Path records [13] |
| List-Unsubscribe one-click | Yes. Needs an HTTPS URI, and the Post value must be exactly `List-Unsubscribe=One-Click` [8] | Custom headers are sent in the `headers` field. Header limits: unverified | Yes, "one-click unsubscribe in accordance with Bulk Sender Requirements" [6b] | Built in (details unverified) | Required on Broadcast streams [12] |
| API | Workers `send_email` binding, max 50 recipients per message [4b] | REST, 10 requests per second per team [5b]; batch endpoint takes up to 100 emails [5c] | REST and SMTP. Sandbox: 200 per 24 h, 1 per second, verified recipients only [6] | REST (rate limits unverified) [10b] | REST; batch takes up to 500 messages [12b] |
| Bounce and complaint webhooks | Not found: unverified | `email.bounced`, `email.complained` [5d] | Through SNS (not rechecked: unverified) | Unverified | Bounce and Spam Complaint webhooks [13b] |

Cloudflare Email Service stays in the Worker only, for the confirmation email. A reader
triggers that email by filling in the form, so it is transactional. The weekly brief is
bulk mail and does not use it.

### Recommendation

For 0 to 1,000 subscribers: **Resend.** The app already has a Resend client, the
setup is one domain and one key, and the free tier covers a weekly send to 100
subscribers (100 per day cap [2]). Between about 100 and 1,000 subscribers the weekly
send passes 100 in a day, so move to Pro at $20 per month [2]. Before launch, confirm
with Resend that a newsletter through the Emails API is within its terms. That point is
unverified.

For 10,000 and more: **Amazon SES.** At $0.10 per 1,000 [7] a weekly send to 10,000
people costs about $4.30 a month, against $20 on Resend Pro. SES needs production
access first, and the request asks for your bounce and complaint process [6]. Adding it
means a second class in `delivery/newsletter_sender.py` next to
`ResendNewsletterSender`.

### Cost table

A weekly send is about 4.3 emails per subscriber per month (52 weeks divided by 12).
Confirmation emails are extra and small.

| Subscribers | Emails per month | Resend | Amazon SES (a la carte) | Postmark Basic |
|---|---|---|---|---|
| 100 | 430 | $0 (free: 3,000 per month, 100 per day) | $0.04 | $15 (the free 100 per month is too small) |
| 1,000 | 4,300 | $20 Pro (a send of 1,000 in one day passes the free 100 per day) | $0.43 | $15 |
| 10,000 | 43,000 | $20 Pro (50,000 included) | $4.30 | $15 + 33,000 x $1.80 per 1,000 = $74.40 |

These are arithmetic on the cited list prices [2] [7] [11]. Taxes, AWS data transfer and
any dedicated IP cost are not included.

## What Patrick must do

Nothing here was created or set by the build. Every step needs Patrick.

1. **Buy a domain.** Every option except Buttondown needs one. The current site runs on
   a `workers.dev` host, and you cannot add mail DNS records to that.
2. **Create a Resend account** and add the domain. Resend shows the DNS records to add.
   Expect an MX record and an SPF TXT record on a sending subdomain, and a DKIM TXT
   record (the exact record names were not read from an opened Resend page:
   unverified). Add a DMARC record too, for example `_dmarc` TXT `v=DMARC1; p=none;
   rua=mailto:you@your-domain`. Raise `p=` later once reports look clean.
3. **Choose a postal address** for the footer. The FTC CAN-SPAM guide says: "Your
   message must include your valid physical postal address. This can be your current
   street address, a post office box you've registered with the U.S. Postal Service, or
   a private mailbox..." [14]. Canada's CASL requires an unsubscribe to take effect "no
   later than 10 business days" [15]. Whether CASL also requires a mailing address in
   this message, and whether a free brief counts as a commercial message under either
   law: unverified. Get advice before launch.
4. **Worker secrets and vars** (run in `deploy-site/`, the integrator deploys after merge):

       npx wrangler secret put RESEND_API_KEY
       npx wrangler secret put IP_HASH_SALT
       npx wrangler secret put ADMIN_TOKEN        # only if not set already

   Then add to `wrangler.toml` under `[vars]`: `EMAIL_FROM = "brief@<domain>"`,
   `EMAIL_FROM_NAME = "Whale Agent"`, `SITE_ORIGIN = "https://<site host>"`.
   To use the Cloudflare binding for the confirmation email instead, run
   `npx wrangler email sending enable <domain>` and uncomment `[[send_email]]` in
   `wrangler.toml`. That needs a Workers Paid plan [3].
5. **GitHub secrets and variables** on your fork of this repo:

       gh secret set RESEND_API_KEY
       gh secret set WHALE_NEWSLETTER_ADMIN_TOKEN     # same value as the Worker ADMIN_TOKEN
       gh variable set WHALE_NEWSLETTER_API --body "https://<site host>"
       gh variable set WHALE_NEWSLETTER_FROM --body "Whale Agent <brief@<domain>>"
       gh variable set WHALE_NEWSLETTER_REPLY_TO --body "hello@<domain>"   # optional
       gh secret set WHALE_NEWSLETTER_HASH_KEY        # optional, a long random string
       gh variable set WHALE_NEWSLETTER_FOOTER --body "Whale Agent, <postal address>"

   `WHALE_SEC_USER_AGENT` already exists for the other jobs. `GEMINI_API_KEY` is
   optional.
6. **Test with a dry run:** Actions, newsletter, Run workflow. `dry_run` is on by
   default. It prints the subject and the counts and sends nothing. Then subscribe one
   address you own and run with `dry_run` off.

## Scaling notes

### KV limits

Free plan: 100,000 key reads, 1,000 writes, 1,000 deletes and 1,000 list requests per
day [16]. One write per second to the same key [17]. A `list()` call returns at most
1,000 keys [18]. Changes "may take up to 60 seconds or more to be visible" in other
locations [19].

What that means here:

- Each subscribe costs 2 writes plus 1 rate limit write. Each confirmation email adds 1
  more. So about 250 signups a day use the free write quota.
- `/api/subscribers` does one list call per 1,000 keys and one read per subscriber. At
  10,000 subscribers one send costs about 10,000 reads, so a few calls a day fit the
  free 100,000.
- Eventual consistency: someone who unsubscribes a minute before the send can still get
  that one email.

### When to move to D1

Move the subscriber table to D1 when any of these is true: signups pass a few hundred a
day, the list passes about 10,000, or you need queries such as "confirmed since a date".
D1 free: 5 million rows read and 100,000 rows written per day, 500 MB per database [20]
[21]. One `SELECT email, token FROM subscribers WHERE status='confirmed'` replaces
thousands of KV reads. Keep the same Worker routes so the Python side does not change.

### Send batching

At the default 5 per second, 10,000 recipients take about 33 minutes. That is under
Resend's 10 requests per second [5b] and inside the 45 minute job timeout, but close.
Past 10,000, either use a batch endpoint (Resend takes 100 per call [5c]; whether each
message in a batch can carry its own headers is unverified) or move to SES.

### Bounces and complaints (design only, not built)

1. Add `POST /api/webhooks/<provider>` to the Worker. Check the provider signature with
   a secret. Resend has `email.bounced` and `email.complained` events [5d]; Postmark
   has bounce and spam complaint webhooks [13b]; SES uses SNS.
2. On a hard bounce or a complaint, set the record to `status: "suppressed"` with the
   reason and time. Do not delete it, so the address cannot sign up and be mailed again
   without a new confirmation.
3. `/api/subscribers` already returns only `confirmed`, so suppressed rows drop out of
   the next send with no Python change.
4. Show suppressed counts in `/api/stats`.
5. Watch the complaint rate. The Cloudflare email skill in this repo's tooling gives a
   target under 0.1%; that figure was not checked on a provider page (unverified).

## Sources

- [1] https://developers.cloudflare.com/email-service/reference/faq/
- [2] https://resend.com/pricing
- [3] https://developers.cloudflare.com/email-service/platform/pricing/
- [4] https://developers.cloudflare.com/email-service/configuration/domains/
- [4b] https://developers.cloudflare.com/email-service/platform/limits/
- [5] https://resend.com/docs/dashboard/domains/introduction
- [5b] https://resend.com/docs/api-reference/introduction
- [5c] https://resend.com/docs/api-reference/emails/send-batch-emails
- [5d] https://resend.com/docs/dashboard/webhooks/event-types
- [6] https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html
- [6b] https://docs.aws.amazon.com/ses/latest/dg/sending-email-subscription-management.html
- [7] https://aws.amazon.com/ses/pricing/ (a la carte $0.10 per 1,000; the Essentials, Pro and Enterprise plans charge $0.16 to $0.23 per 1,000 plus fees)
- [8] https://developers.cloudflare.com/email-service/reference/headers/
- [9] https://buttondown.com/pricing
- [10] https://docs.buttondown.com/sending-from-a-custom-domain
- [10b] https://docs.buttondown.com/api-introduction
- [11] https://postmarkapp.com/pricing
- [12] https://postmarkapp.com/developer/api/message-streams-api
- [12b] https://postmarkapp.com/developer/api/email-api
- [13] https://postmarkapp.com/support/article/how-do-i-verify-a-domain
- [13b] https://postmarkapp.com/developer/webhooks/webhooks-overview
- [14] https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- [15] https://laws-lois.justice.gc.ca/eng/acts/E-1.6/page-2.html (section 11(3))
- [16] https://developers.cloudflare.com/kv/platform/pricing/
- [17] https://developers.cloudflare.com/kv/platform/limits/
- [18] https://developers.cloudflare.com/kv/api/list-keys/
- [19] https://developers.cloudflare.com/kv/concepts/how-kv-works/
- [20] https://developers.cloudflare.com/d1/platform/limits/
- [21] https://developers.cloudflare.com/d1/platform/pricing/

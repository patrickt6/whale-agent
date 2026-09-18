# deploy-site

One Cloudflare Worker serves the static site (`../site`), the newsletter API, and `/install`.

## Deploy

    ./sync-install.sh                  # copies ../install.sh into the bundle if it exists
    npx --yes wrangler@latest deploy

If `../install.sh` does not exist, `/install` returns 503 "installer not published yet".

## One-time setup

- KV namespace `SUBSCRIBERS` already exists; its id is in `wrangler.toml`.
- Admin token for the sender job (not in git): `npx wrangler secret put ADMIN_TOKEN`

## Routes

- `POST /api/subscribe` JSON `{email, website}` (`website` is a honeypot). Stores a pending subscriber.
- `GET /api/confirm?token=...` marks the subscriber confirmed.
- `GET /api/unsubscribe?token=...` deletes the subscriber and shows a page.
- `POST /api/unsubscribe?token=...` one-click unsubscribe (RFC 8058). Always returns 200.
- `GET /api/subscribers` with `Authorization: Bearer <ADMIN_TOKEN>` returns confirmed emails and their tokens (for unsubscribe links).
- `GET /api/stats` with the same Bearer token returns total, pending, confirmed and unsubscribed counts, and the active sender.
- `GET /install` serves the installer as text/plain.

The confirmation email goes out through `src/sender.js` when a sender is set (the `EMAIL` binding or `RESEND_API_KEY`, plus `EMAIL_FROM`). With no sender, nothing is sent. See the comments at the end of `wrangler.toml`.

## Tests

    node --test

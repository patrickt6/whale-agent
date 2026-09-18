# Publishing the weekly

Free, no account for the reader, about five minutes.

## What this is

Static pages on Cloudflare. **They are public to anyone holding the URL.** The URL is long
and is not linked from anywhere, which is obscurity, not access control. Every figure on
these pages is copied from a public filing, so there is nothing confidential to protect,
and making the reader type a password to read his own newsletter was not worth it.

If that changes, the previous version of this file in git history has a worker that adds
HTTP Basic auth in about twenty lines.

## First time

```bash
cd deploy
npx wrangler login
npx wrangler deploy
```

Wrangler prints the live URL, something like `https://whale-weekly.<subdomain>.workers.dev`.

## Every week

Generate the pages straight into the published directory, then deploy:

```bash
./whale weekly --articles-dir deploy/public \
               --article-base-url https://whale-weekly.<subdomain>.workers.dev
cd deploy && npx wrangler deploy
```

`--article-base-url` is what turns the email's links and its "Read the weekly overview"
button into real URLs. Without it they are `file://` paths that only work on the machine
that generated them.

Old pages stay up on purpose. A note the reader opened last month should not disappear
because a newer one was generated. Filenames are stable per date and slug, so regenerating
the same week overwrites rather than duplicating.

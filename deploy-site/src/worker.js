// whale-agent site worker: static assets plus the newsletter API and /install.
//
// KV layout (binding SUBSCRIBERS):
//   sub:<email>  -> {"email","status":"pending"|"confirmed","token","created","confirmed",
//                    "consent":{"subscribed_at","subscribe_ip_hash","confirmed_at",
//                               "confirm_ip_hash","method"},"confirm_sent_at"}
//   tok:<token>  -> <email>
//   rl:<ip>:<hour> -> count (expires after 1 hour)
//   stat:unsubscribed -> running count of unsubscribes (best effort, KV is not atomic)
//
// Confirmation email: sent through src/sender.js when a sender is configured
// (env.EMAIL binding or env.RESEND_API_KEY, plus env.EMAIL_FROM). With no sender the
// subscriber stays "pending" until they open /api/confirm?token=... , as before.
// Only confirmed addresses are returned by /api/subscribers.
//
// Secrets: ADMIN_TOKEN, optional RESEND_API_KEY, optional IP_HASH_SALT.
// Vars: optional EMAIL_FROM, EMAIL_FROM_NAME, SITE_ORIGIN.

import installer from "./installer.js";
import { pickSender, confirmationMessage } from "./sender.js";

const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,}$/;
const RATE_LIMIT = 5; // subscribe attempts per IP per hour
const RESEND_CONFIRM_AFTER_MS = 3600000; // at most one confirmation email per hour per address

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function page(title, text, status = 200) {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)} | whale-agent</title><link rel="stylesheet" href="/style.css"></head>
<body><section class="hero"><div class="wrap"><p class="eyebrow">whale-agent brief</p>
<h1>${escapeHtml(title)}</h1><p class="lede">${escapeHtml(text)}</p>
<div class="cta-row"><a class="btn" href="/">Back to the site</a></div></div></section></body></html>`;
  return new Response(html, { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

function newToken() {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  if (crypto.subtle && typeof crypto.subtle.timingSafeEqual === "function") {
    return crypto.subtle.timingSafeEqual(x, y);
  }
  let d = 0;
  for (let i = 0; i < x.length; i++) d |= x[i] ^ y[i];
  return d === 0;
}

async function rateLimited(env, ip, now) {
  const key = `rl:${ip}:${Math.floor(now / 3600000)}`;
  const n = parseInt((await env.SUBSCRIBERS.get(key)) || "0", 10);
  if (n >= RATE_LIMIT) return true;
  await env.SUBSCRIBERS.put(key, String(n + 1), { expirationTtl: 3600 });
  return false;
}

// Audit hash of the client IP. HMAC with a secret salt; an unsalted hash of an IPv4
// address is easy to reverse, so without IP_HASH_SALT we store null.
export async function ipHash(env, ip) {
  if (!ip || typeof env.IP_HASH_SALT !== "string" || env.IP_HASH_SALT === "") return null;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(env.IP_HASH_SALT), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(ip));
  return Array.from(new Uint8Array(sig), (x) => x.toString(16).padStart(2, "0")).join("");
}

export function siteOrigin(request, env) {
  if (typeof env.SITE_ORIGIN === "string" && /^https:\/\/[^/]+$/.test(env.SITE_ORIGIN.trim())) return env.SITE_ORIGIN.trim();
  return new URL(request.url).origin;
}

async function sendConfirmation(sender, env, record, origin, now) {
  const link = `${origin}/api/confirm?token=${record.token}`;
  try {
    await sender.send(confirmationMessage(record.email, link, now));
    record.confirm_sent_at = new Date(now).toISOString();
    await env.SUBSCRIBERS.put(`sub:${record.email}`, JSON.stringify(record));
  } catch (err) {
    // Never log the address. The subscriber stays pending and can submit the form again.
    console.error(JSON.stringify({ msg: "confirmation send failed", sender: sender.name, error: String(err) }));
  }
}

export async function handleSubscribe(request, env, now = Date.now(), ctx = null, fetchImpl = fetch) {
  let body;
  try {
    const ct = request.headers.get("content-type") || "";
    if (ct.includes("application/json")) body = await request.json();
    else body = Object.fromEntries(await request.formData());
  } catch {
    return json({ ok: false, message: "Bad request." }, 400);
  }
  if (body && typeof body.website === "string" && body.website.trim() !== "") {
    return json({ ok: false, message: "Bad request." }, 400);
  }
  const email = typeof body?.email === "string" ? body.email.trim().toLowerCase() : "";
  if (email.length > 254 || !EMAIL_RE.test(email)) {
    return json({ ok: false, message: "Please enter a valid email address." }, 400);
  }
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  if (await rateLimited(env, ip, now)) {
    return json({ ok: false, message: "Too many attempts. Try again in an hour." }, 429);
  }
  const sender = pickSender(env, fetchImpl);
  const message = sender
    ? "Almost done. Check your inbox and click the link to confirm."
    : "You are on the list. Confirmation email is coming soon.";
  const origin = siteOrigin(request, env);
  const existing = await env.SUBSCRIBERS.get(`sub:${email}`, "json");
  let record = existing;
  if (!record) {
    const token = newToken();
    record = {
      email,
      status: "pending",
      token,
      created: new Date(now).toISOString(),
      confirmed: null,
      consent: { method: "site-form double opt-in", subscribed_at: new Date(now).toISOString(), subscribe_ip_hash: await ipHash(env, ip), confirmed_at: null, confirm_ip_hash: null },
      confirm_sent_at: null,
    };
    await env.SUBSCRIBERS.put(`sub:${email}`, JSON.stringify(record));
    await env.SUBSCRIBERS.put(`tok:${token}`, email);
  }
  // Same answer for new, pending and confirmed addresses, so the form does not reveal
  // who is on the list.
  const lastSent = record.confirm_sent_at ? Date.parse(record.confirm_sent_at) : 0;
  if (sender && record.status === "pending" && now - lastSent >= RESEND_CONFIRM_AFTER_MS) {
    const work = sendConfirmation(sender, env, record, origin, now);
    if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(work);
    else await work;
  }
  return json({ ok: true, message });
}

async function lookupTokenValue(env, token) {
  if (!/^[0-9a-f]{48}$/.test(token)) return null;
  const email = await env.SUBSCRIBERS.get(`tok:${token}`);
  if (!email) return null;
  const record = await env.SUBSCRIBERS.get(`sub:${email}`, "json");
  return record ? { token, email, record } : null;
}

function lookupToken(env, url) {
  return lookupTokenValue(env, url.searchParams.get("token") || "");
}

export async function handleConfirm(url, env, now = Date.now(), request = null) {
  const hit = await lookupToken(env, url);
  if (!hit) return page("Link not valid", "This confirm link is not valid or was already used for an unsubscribe.", 404);
  if (hit.record.status !== "confirmed") {
    hit.record.status = "confirmed";
    hit.record.confirmed = new Date(now).toISOString();
    const ip = request ? request.headers.get("cf-connecting-ip") || "" : "";
    hit.record.consent = { ...(hit.record.consent || {}), confirmed_at: hit.record.confirmed, confirm_ip_hash: await ipHash(env, ip) };
    await env.SUBSCRIBERS.put(`sub:${hit.email}`, JSON.stringify(hit.record));
  }
  return page("You are subscribed", "You will get the free weekly brief. Every email has a one-click unsubscribe link.");
}

async function removeSubscriber(env, hit) {
  await env.SUBSCRIBERS.delete(`sub:${hit.email}`);
  await env.SUBSCRIBERS.delete(`tok:${hit.token}`);
  const n = parseInt((await env.SUBSCRIBERS.get("stat:unsubscribed")) || "0", 10);
  await env.SUBSCRIBERS.put("stat:unsubscribed", String(n + 1));
}

export async function handleUnsubscribe(url, env) {
  const hit = await lookupToken(env, url);
  if (!hit) return page("Link not valid", "This unsubscribe link is not valid, or you are already off the list.", 404);
  await removeSubscriber(env, hit);
  return page("You are unsubscribed", "Your address is deleted. You will not get more emails.");
}

// RFC 8058 one-click unsubscribe. The mail client POSTs "List-Unsubscribe=One-Click" to
// the List-Unsubscribe URL. Always 200, also for an unknown or used token, so a retry
// from the mailbox provider never sees an error.
export async function handleUnsubscribePost(request, url, env) {
  let token = url.searchParams.get("token") || "";
  if (!token) {
    try {
      const ct = request.headers.get("content-type") || "";
      if (ct.includes("application/x-www-form-urlencoded") || ct.includes("multipart/form-data")) {
        const form = await request.formData();
        token = String(form.get("token") || "");
      }
    } catch {
      token = "";
    }
  }
  const hit = await lookupTokenValue(env, token);
  if (hit) await removeSubscriber(env, hit);
  return new Response("Unsubscribed.\n", { status: 200, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
}

function authorized(request, env) {
  const auth = request.headers.get("authorization") || "";
  const want = env.ADMIN_TOKEN ? `Bearer ${env.ADMIN_TOKEN}` : "";
  return Boolean(want) && safeEqual(auth, want);
}

async function eachSubscriber(env, fn) {
  let cursor;
  do {
    const res = await env.SUBSCRIBERS.list({ prefix: "sub:", cursor });
    for (const k of res.keys) {
      const r = await env.SUBSCRIBERS.get(k.name, "json");
      if (r) fn(r);
    }
    cursor = res.list_complete ? undefined : res.cursor;
  } while (cursor);
}

export async function handleSubscribers(request, env) {
  if (!authorized(request, env)) return json({ ok: false, message: "Unauthorized." }, 401);
  const out = [];
  await eachSubscriber(env, (r) => {
    if (r.status === "confirmed") out.push({ email: r.email, token: r.token });
  });
  return json({ ok: true, count: out.length, subscribers: out });
}

export async function handleStats(request, env) {
  if (!authorized(request, env)) return json({ ok: false, message: "Unauthorized." }, 401);
  let pending = 0;
  let confirmed = 0;
  await eachSubscriber(env, (r) => {
    if (r.status === "confirmed") confirmed += 1;
    else pending += 1;
  });
  const unsubscribed = parseInt((await env.SUBSCRIBERS.get("stat:unsubscribed")) || "0", 10);
  return json({ ok: true, total: pending + confirmed, pending, confirmed, unsubscribed, sender: pickSender(env)?.name || "none" });
}

export function handleInstall(script = installer) {
  if (typeof script !== "string" || script === "") {
    return new Response("installer not published yet\n", { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } });
  }
  return new Response(script, { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "public, max-age=300" } });
}

// Headers for every response the Worker builds itself. site/_headers covers static
// assets only; Cloudflare does not apply it to Worker responses.
export const SECURITY_HEADERS = {
  "content-security-policy": "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "x-frame-options": "DENY",
  "strict-transport-security": "max-age=31536000",
};

export function withSecurityHeaders(response) {
  const r = new Response(response.body, response);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) r.headers.set(k, v);
  return r;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/") && url.pathname !== "/install") return env.ASSETS.fetch(request);
    return withSecurityHeaders(await route(request, env, ctx, url));
  },
};

async function route(request, env, ctx, url) {
  {
    const p = url.pathname;
    try {
      if (p === "/install") return handleInstall();
      if (p === "/api/subscribe") {
        if (request.method !== "POST") return json({ ok: false, message: "Use POST." }, 405);
        return await handleSubscribe(request, env, Date.now(), ctx);
      }
      if (p === "/api/confirm" && request.method === "GET") return await handleConfirm(url, env, Date.now(), request);
      if (p === "/api/unsubscribe" && request.method === "GET") return await handleUnsubscribe(url, env);
      if (p === "/api/unsubscribe" && request.method === "POST") return await handleUnsubscribePost(request, url, env);
      if (p === "/api/subscribers" && request.method === "GET") return await handleSubscribers(request, env);
      if (p === "/api/stats" && request.method === "GET") return await handleStats(request, env);
      if (p.startsWith("/api/")) return json({ ok: false, message: "Not found." }, 404);
      return env.ASSETS.fetch(request);
    } catch (err) {
      console.error(JSON.stringify({ msg: "worker error", path: p, error: String(err) }));
      return json({ ok: false, message: "Server error." }, 500);
    }
  }
}

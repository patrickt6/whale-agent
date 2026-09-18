import test from "node:test";
import assert from "node:assert/strict";
import worker, { handleSubscribe, ipHash, siteOrigin } from "../src/worker.js";
import { pickSender, confirmationMessage, nextBriefUtc, formatBriefDate } from "../src/sender.js";

function fakeKV() {
  const m = new Map();
  return {
    m,
    async get(k, type) { const v = m.has(k) ? m.get(k) : null; return v !== null && type === "json" ? JSON.parse(v) : v; },
    async put(k, v) { m.set(k, v); },
    async delete(k) { m.delete(k); },
    async list({ prefix = "" } = {}) { return { keys: [...m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true }; },
  };
}
const B = "https://site.test";
const baseEnv = (extra = {}) => ({ SUBSCRIBERS: fakeKV(), ADMIN_TOKEN: "secret-admin", ASSETS: { fetch: async () => new Response("asset") }, ...extra });
const subReq = (email, ip = "1.1.1.1", origin = B) => new Request(`${origin}/api/subscribe`, { method: "POST", headers: { "content-type": "application/json", "cf-connecting-ip": ip }, body: JSON.stringify({ email }) });
const fakeBinding = () => { const sent = []; return { sent, async send(m) { sent.push(m); return { messageId: "cf-1" }; } }; };
const admin = { authorization: "Bearer secret-admin" };

test("pickSender: none without EMAIL_FROM or without a provider", () => {
  assert.equal(pickSender({ EMAIL: fakeBinding() }), null);
  assert.equal(pickSender({ EMAIL_FROM: "brief@example.com" }), null);
  assert.equal(pickSender({ EMAIL_FROM: "brief@example.com", RESEND_API_KEY: "" }), null);
});

test("pickSender: binding wins over Resend", () => {
  const s = pickSender({ EMAIL_FROM: "brief@example.com", EMAIL: fakeBinding(), RESEND_API_KEY: "re_x" });
  assert.equal(s.name, "cloudflare");
});

test("no sender keeps old behaviour: pending, old message, nothing sent", async () => {
  const e = baseEnv();
  const r = await worker.fetch(subReq("a@example.com"), e, { waitUntil() { throw new Error("must not be called"); } });
  const d = await r.json();
  assert.match(d.message, /Confirmation email is coming soon/);
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:a@example.com"));
  assert.equal(rec.status, "pending");
  assert.equal(rec.confirm_sent_at, null);
  assert.equal(rec.consent.subscribe_ip_hash, null); // no IP_HASH_SALT
  assert.ok(rec.consent.subscribed_at);
});

test("binding sender: confirmation uses site origin, waitUntil, records send time", async () => {
  const b = fakeBinding();
  const e = baseEnv({ EMAIL: b, EMAIL_FROM: "brief@example.com", IP_HASH_SALT: "salt" });
  const waits = [];
  const r = await worker.fetch(subReq("b@example.com", "9.9.9.9"), e, { waitUntil(p) { waits.push(p); } });
  assert.match((await r.json()).message, /Check your inbox/);
  assert.equal(waits.length, 1);
  await Promise.all(waits);
  assert.equal(b.sent.length, 1);
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:b@example.com"));
  assert.equal(b.sent[0].to, "b@example.com");
  assert.deepEqual(b.sent[0].from, { email: "brief@example.com", name: "Whale Agent" });
  assert.ok(b.sent[0].text.includes(`${B}/api/confirm?token=${rec.token}`));
  assert.ok(b.sent[0].html.includes(`${B}/api/confirm?token=${rec.token}`));
  assert.ok(rec.confirm_sent_at);
  assert.match(rec.consent.subscribe_ip_hash, /^[0-9a-f]{64}$/);
  assert.equal(rec.consent.subscribe_ip_hash, await ipHash({ IP_HASH_SALT: "salt" }, "9.9.9.9"));
  assert.notEqual(rec.consent.subscribe_ip_hash, await ipHash({ IP_HASH_SALT: "other" }, "9.9.9.9"));
});

test("SITE_ORIGIN overrides request origin; non-https value is ignored", () => {
  const req = new Request("https://worker.dev/api/subscribe");
  assert.equal(siteOrigin(req, { SITE_ORIGIN: "https://whaleagent.example" }), "https://whaleagent.example");
  assert.equal(siteOrigin(req, { SITE_ORIGIN: "http://evil.example" }), "https://worker.dev");
  assert.equal(siteOrigin(req, {}), "https://worker.dev");
});

test("resubmit: no second email within an hour, one after; confirmed gets none", async () => {
  const b = fakeBinding();
  const e = baseEnv({ EMAIL: b, EMAIL_FROM: "brief@example.com" });
  const t0 = 1_800_000_000_000;
  await handleSubscribe(subReq("c@example.com", "1.1.1.1"), e, t0);
  await handleSubscribe(subReq("c@example.com", "1.1.1.2"), e, t0 + 60_000);
  assert.equal(b.sent.length, 1);
  await handleSubscribe(subReq("c@example.com", "1.1.1.3"), e, t0 + 3_700_000);
  assert.equal(b.sent.length, 2);
  const token = JSON.parse(e.SUBSCRIBERS.m.get("sub:c@example.com")).token;
  await worker.fetch(new Request(`${B}/api/confirm?token=${token}`, { headers: { "cf-connecting-ip": "4.4.4.4" } }), e);
  await handleSubscribe(subReq("c@example.com", "1.1.1.4"), e, t0 + 9_000_000);
  assert.equal(b.sent.length, 2);
});

test("sender failure: still 200, stays pending, no send time", async () => {
  const e = baseEnv({ EMAIL: { async send() { throw new Error("E_SENDER_NOT_VERIFIED"); } }, EMAIL_FROM: "brief@example.com" });
  const orig = console.error; const logged = []; console.error = (m) => logged.push(m);
  try {
    const r = await handleSubscribe(subReq("d@example.com"), e, Date.now());
    assert.equal(r.status, 200);
  } finally { console.error = orig; }
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:d@example.com"));
  assert.equal(rec.status, "pending");
  assert.equal(rec.confirm_sent_at, null);
  assert.equal(logged.length, 1);
  assert.ok(!logged[0].includes("d@example.com"), "log must not carry the address");
});

test("Resend sender: posts JSON with bearer key and from name", async () => {
  const calls = [];
  const fakeFetch = async (url, init) => { calls.push({ url, init }); return new Response(JSON.stringify({ id: "re-1" }), { status: 200 }); };
  const e = baseEnv({ RESEND_API_KEY: "re_test", EMAIL_FROM: "brief@example.com", EMAIL_FROM_NAME: "Whale Brief" });
  await handleSubscribe(subReq("e@example.com"), e, Date.now(), null, fakeFetch);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.resend.com/emails");
  assert.equal(calls[0].init.headers.authorization, "Bearer re_test");
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.from, "Whale Brief <brief@example.com>");
  assert.deepEqual(body.to, ["e@example.com"]);
  assert.ok(body.text.includes("/api/confirm?token="));
});

test("Resend non-2xx is treated as a failed send", async () => {
  const fakeFetch = async () => new Response("{}", { status: 422 });
  const s = pickSender({ RESEND_API_KEY: "re_test", EMAIL_FROM: "brief@example.com" }, fakeFetch);
  await assert.rejects(() => s.send(confirmationMessage("x@example.com", "https://site.test/api/confirm?token=1")), /422/);
});

test("confirm stores consent confirm timestamp and hash", async () => {
  const e = baseEnv({ IP_HASH_SALT: "salt" });
  await handleSubscribe(subReq("f@example.com"), e, Date.now());
  const token = JSON.parse(e.SUBSCRIBERS.m.get("sub:f@example.com")).token;
  await worker.fetch(new Request(`${B}/api/confirm?token=${token}`, { headers: { "cf-connecting-ip": "5.5.5.5" } }), e);
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:f@example.com"));
  assert.equal(rec.status, "confirmed");
  assert.equal(rec.consent.confirmed_at, rec.confirmed);
  assert.equal(rec.consent.confirm_ip_hash, await ipHash(e, "5.5.5.5"));
  assert.ok(rec.consent.subscribed_at);
});

test("RFC 8058 one-click POST unsubscribes; unknown token is still 200", async () => {
  const e = baseEnv();
  await handleSubscribe(subReq("g@example.com"), e, Date.now());
  const token = JSON.parse(e.SUBSCRIBERS.m.get("sub:g@example.com")).token;
  const r = await worker.fetch(new Request(`${B}/api/unsubscribe?token=${token}`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: "List-Unsubscribe=One-Click" }), e);
  assert.equal(r.status, 200);
  assert.equal(e.SUBSCRIBERS.m.has("sub:g@example.com"), false);
  assert.equal(e.SUBSCRIBERS.m.has(`tok:${token}`), false);
  const again = await worker.fetch(new Request(`${B}/api/unsubscribe?token=${token}`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: "List-Unsubscribe=One-Click" }), e);
  assert.equal(again.status, 200);
  const bad = await worker.fetch(new Request(`${B}/api/unsubscribe`, { method: "POST", body: "junk" }), e);
  assert.equal(bad.status, 200);
  assert.equal(e.SUBSCRIBERS.m.get("stat:unsubscribed"), "1");
});

test("POST unsubscribe accepts token in form body", async () => {
  const e = baseEnv();
  await handleSubscribe(subReq("h@example.com"), e, Date.now());
  const token = JSON.parse(e.SUBSCRIBERS.m.get("sub:h@example.com")).token;
  const r = await worker.fetch(new Request(`${B}/api/unsubscribe`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: `token=${token}` }), e);
  assert.equal(r.status, 200);
  assert.equal(e.SUBSCRIBERS.m.has("sub:h@example.com"), false);
});

test("GET /api/stats: admin only, counts pending, confirmed, unsubscribed", async () => {
  const e = baseEnv();
  assert.equal((await worker.fetch(new Request(`${B}/api/stats`), e)).status, 401);
  assert.equal((await worker.fetch(new Request(`${B}/api/stats`, { headers: { authorization: "Bearer nope" } }), e)).status, 401);
  for (const [i, x] of ["p1", "p2", "c1", "u1"].entries()) await handleSubscribe(subReq(`${x}@example.com`, `7.7.7.${i}`), e, Date.now());
  const tok = (x) => JSON.parse(e.SUBSCRIBERS.m.get(`sub:${x}@example.com`)).token;
  await worker.fetch(new Request(`${B}/api/confirm?token=${tok("c1")}`), e);
  await worker.fetch(new Request(`${B}/api/unsubscribe?token=${tok("u1")}`), e);
  const s = await (await worker.fetch(new Request(`${B}/api/stats`, { headers: admin }), e)).json();
  assert.deepEqual({ total: s.total, pending: s.pending, confirmed: s.confirmed, unsubscribed: s.unsubscribed, sender: s.sender }, { total: 3, pending: 2, confirmed: 1, unsubscribed: 1, sender: "none" });
  const noAdmin = baseEnv(); delete noAdmin.ADMIN_TOKEN;
  assert.equal((await worker.fetch(new Request(`${B}/api/stats`, { headers: { authorization: "Bearer " } }), noAdmin)).status, 401);
});

test("confirmation copy has no em or en dashes", () => {
  const m = confirmationMessage("x@example.com", "https://site.test/api/confirm?token=abc");
  for (const part of [m.subject, m.text, m.html]) assert.ok(!/[–—]/.test(part));
});

test("the confirmation names the next brief, which is the Monday 14:00 UTC cron", () => {
  // .github/workflows/newsletter.yml sends on cron "0 14 * * 1".
  const thursday = Date.UTC(2026, 8, 17, 12, 0, 0);
  assert.equal(formatBriefDate(nextBriefUtc(thursday)), "Monday 21 September, 14:00 UTC");
  // On the send day the next one is only later than the send itself.
  assert.equal(formatBriefDate(nextBriefUtc(Date.UTC(2026, 8, 21, 13, 0, 0))), "Monday 21 September, 14:00 UTC");
  assert.equal(formatBriefDate(nextBriefUtc(Date.UTC(2026, 8, 21, 14, 0, 0))), "Monday 28 September, 14:00 UTC");
});

test("the confirmation tells the subscriber when mail arrives, in both parts", () => {
  const m = confirmationMessage("x@example.com", "https://site.test/api/confirm?token=abc", Date.UTC(2026, 8, 17, 12, 0, 0));
  for (const part of [m.text, m.html]) {
    assert.match(part, /Monday 21 September, 14:00 UTC/);
  }
  // A subscriber who cannot see the button still has the link.
  assert.ok(m.text.includes("https://site.test/api/confirm?token=abc"));
  assert.equal((m.html.match(/site\.test\/api\/confirm\?token=abc/g) || []).length, 2);
});

test("inbox previews start with words, not art", () => {
  const m = confirmationMessage("x@example.com", "https://site.test/api/confirm?token=a");
  assert.ok(m.text.startsWith("Someone asked"), "plain text opens with a sentence");
  assert.ok(!m.text.slice(0, 160).includes("#"), "no hashes in the first 160 chars");
  const visible = m.html.replace(/<[^>]+>/g, " ").replace(/&#?\w+;/g, " ").replace(/\s+/g, " ").trim();
  assert.ok(!/[#\u2580-\u259f]/.test(visible.slice(0, 300)), "no art characters in the first HTML text");
});

test("the confirmation carries the whale and escapes the link", () => {
  const m = confirmationMessage("x@example.com", "https://site.test/api/confirm?token=a&b=1");
  assert.ok(m.html.includes('aria-hidden="true"') && m.html.includes("#6fa0e6"), "pixel whale is present");
  assert.ok(!m.html.includes("<pre"), "no text art that an inbox preview could show");
  assert.ok(m.html.includes("token=a&amp;b=1"), "query is escaped");
  assert.ok(!m.html.includes("token=a&b=1"), "raw ampersand does not reach the markup");
});


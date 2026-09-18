import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.js";
import { handleInstall } from "../src/worker.js";

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
const env = () => ({ SUBSCRIBERS: fakeKV(), ADMIN_TOKEN: "secret-admin", ASSETS: { fetch: async () => new Response("asset") } });
const B = "https://x.test";
const sub = (e, body, ip = "1.1.1.1") => worker.fetch(new Request(`${B}/api/subscribe`, { method: "POST", headers: { "content-type": "application/json", "cf-connecting-ip": ip }, body: JSON.stringify(body) }), e);

test("subscribe stores pending and is idempotent", async () => {
  const e = env();
  const r = await sub(e, { email: "A@Example.com" });
  assert.equal(r.status, 200);
  const d = await r.json();
  assert.equal(d.ok, true);
  assert.match(d.message, /Confirmation email is coming soon/);
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:a@example.com"));
  assert.equal(rec.status, "pending");
  assert.match(rec.token, /^[0-9a-f]{48}$/);
  assert.ok(rec.created);
  await sub(e, { email: "a@example.com" });
  assert.equal(JSON.parse(e.SUBSCRIBERS.m.get("sub:a@example.com")).token, rec.token);
  assert.equal([...e.SUBSCRIBERS.m.keys()].filter((k) => k.startsWith("tok:")).length, 1);
});

test("rejects bad email, honeypot, and GET", async () => {
  const e = env();
  assert.equal((await sub(e, { email: "nope" })).status, 400);
  assert.equal((await sub(e, { email: "b@example.com", website: "spam" })).status, 400);
  assert.equal(e.SUBSCRIBERS.m.has("sub:b@example.com"), false);
  assert.equal((await worker.fetch(new Request(`${B}/api/subscribe`), e)).status, 405);
});

test("rate limit per IP", async () => {
  const e = env();
  for (let i = 0; i < 5; i++) assert.equal((await sub(e, { email: `u${i}@example.com` })).status, 200);
  assert.equal((await sub(e, { email: "u9@example.com" })).status, 429);
  assert.equal((await sub(e, { email: "u9@example.com" }, "2.2.2.2")).status, 200);
});

test("confirm, list, unsubscribe", async () => {
  const e = env();
  await sub(e, { email: "c@example.com" });
  await sub(e, { email: "p@example.com" }, "3.3.3.3");
  const token = JSON.parse(e.SUBSCRIBERS.m.get("sub:c@example.com")).token;
  assert.equal((await worker.fetch(new Request(`${B}/api/confirm?token=bad`), e)).status, 404);
  const c = await worker.fetch(new Request(`${B}/api/confirm?token=${token}`), e);
  assert.equal(c.status, 200);
  assert.match(c.headers.get("content-type"), /text\/html/);
  assert.equal((await worker.fetch(new Request(`${B}/api/subscribers`), e)).status, 401);
  assert.equal((await worker.fetch(new Request(`${B}/api/subscribers`, { headers: { authorization: "Bearer wrong" } }), e)).status, 401);
  const l = await (await worker.fetch(new Request(`${B}/api/subscribers`, { headers: { authorization: "Bearer secret-admin" } }), e)).json();
  assert.deepEqual(l.subscribers.map((s) => s.email), ["c@example.com"]);
  const u = await worker.fetch(new Request(`${B}/api/unsubscribe?token=${token}`), e);
  assert.equal(u.status, 200);
  assert.equal(e.SUBSCRIBERS.m.has("sub:c@example.com"), false);
  assert.equal(e.SUBSCRIBERS.m.has(`tok:${token}`), false);
});

test("subscribers endpoint fails closed with no ADMIN_TOKEN", async () => {
  const e = env(); delete e.ADMIN_TOKEN;
  assert.equal((await worker.fetch(new Request(`${B}/api/subscribers`, { headers: { authorization: "Bearer " } }), e)).status, 401);
});

test("install 503 when missing, text when present; assets pass through", async () => {
  const r = handleInstall(null);
  assert.equal(r.status, 503);
  assert.equal(await r.text(), "installer not published yet\n");
  const s = handleInstall("#!/bin/sh\necho hi\n");
  assert.equal(s.status, 200);
  assert.match(s.headers.get("content-type"), /text\/plain/);
  assert.equal(await (await worker.fetch(new Request(`${B}/`), env())).text(), "asset");
});

test("worker responses carry security headers and no CORS", async () => {
  const e = env();
  for (const r of [
    await sub(e, { email: "h@example.com" }),
    await worker.fetch(new Request(`${B}/api/subscribers`), e),
    await worker.fetch(new Request(`${B}/api/confirm?token=${"0".repeat(48)}`), e),
  ]) {
    assert.equal(r.headers.get("x-content-type-options"), "nosniff");
    assert.match(r.headers.get("content-security-policy"), /frame-ancestors 'none'/);
    assert.equal(r.headers.get("access-control-allow-origin"), null);
  }
  const denied = await worker.fetch(new Request(`${B}/api/subscribers`, { headers: { authorization: "Bearer wrong" } }), e);
  assert.equal(denied.status, 401);
});

test("subscribe gives the same answer for new and confirmed addresses", async () => {
  const e = env();
  const first = await (await sub(e, { email: "same@example.com" })).json();
  const rec = JSON.parse(e.SUBSCRIBERS.m.get("sub:same@example.com"));
  rec.status = "confirmed";
  e.SUBSCRIBERS.m.set("sub:same@example.com", JSON.stringify(rec));
  const again = await (await sub(e, { email: "same@example.com" }, "2.2.2.2")).json();
  assert.deepEqual(again, first);
});

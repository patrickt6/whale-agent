// Pluggable sender for the double opt-in confirmation email.
//
// Precedence:
//   1. env.EMAIL        Cloudflare Email Service `send_email` binding. The confirmation is
//                       transactional (a user action triggers it), which that service allows.
//                       The weekly newsletter itself is bulk mail and must NOT use it.
//   2. env.RESEND_API_KEY  Resend REST API through fetch.
//   3. nothing          returns null; the caller keeps the old behaviour (no email).
//
// Both real senders need env.EMAIL_FROM, an address on a domain verified with the provider.
// Without it we return null, because a send from an unverified domain fails anyway.

const RESEND_URL = "https://api.resend.com/emails";

function fromParts(env) {
  const email = typeof env.EMAIL_FROM === "string" ? env.EMAIL_FROM.trim() : "";
  const name = typeof env.EMAIL_FROM_NAME === "string" && env.EMAIL_FROM_NAME.trim() ? env.EMAIL_FROM_NAME.trim() : "Whale Agent";
  return { email, name };
}

export function pickSender(env, fetchImpl = fetch) {
  const from = fromParts(env);
  if (!from.email) return null;
  if (env.EMAIL && typeof env.EMAIL.send === "function") {
    return {
      name: "cloudflare",
      async send(msg) {
        const res = await env.EMAIL.send({
          to: msg.to,
          from: { email: from.email, name: from.name },
          subject: msg.subject,
          html: msg.html,
          text: msg.text,
          ...(msg.headers ? { headers: msg.headers } : {}),
        });
        return { ok: true, id: res && res.messageId ? String(res.messageId) : "" };
      },
    };
  }
  if (typeof env.RESEND_API_KEY === "string" && env.RESEND_API_KEY !== "") {
    return {
      name: "resend",
      async send(msg) {
        const res = await fetchImpl(RESEND_URL, {
          method: "POST",
          headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
          body: JSON.stringify({
            from: `${from.name} <${from.email}>`,
            to: [msg.to],
            subject: msg.subject,
            html: msg.html,
            text: msg.text,
            ...(msg.headers ? { headers: msg.headers } : {}),
          }),
        });
        if (!res.ok) throw new Error(`resend status ${res.status}`);
        const data = await res.json().catch(() => ({}));
        return { ok: true, id: data && data.id ? String(data.id) : "" };
      },
    };
  }
  return null;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// User-facing copy. Plain words, short sentences, no dashes.
// The whale from the CLI start screen (src/whale_agent/cli/app.py WHALE_PIXELS), drawn as
// a table of coloured cells. No text characters, so inbox previews never show art.
const WHALE_PIXELS = [
  ".......LLLLL..............LL..........LL..",
  ".....LDDDDDDDDL..........LDDD........DDDL.",
  "....LDDLLLLLLDDD.........DDLDDDDLLDDDDLDL.",
  "...LDDLLLLLLLLLDDL.......DDLLLLDDDDLLLLDL.",
  "..LDDLLLLLLLLLLLDDL......LDLLLLLDLLLLLLDL.",
  "..DDLLLLLLLLLLLLLLDL.....LDLLLLLLLLLLLLD..",
  "..DLLLLLLLLLLLLLLLLDL.....DDLLLLLLLLLLDL..",
  ".LDLLLLLLLLLLLLLLLLDDL.....DDLLLLLLLDDD...",
  ".LDLLLLLLLLLLLLLLLLLDD......LDDDLLDDDL....",
  ".LDDLLLLLLLLLEDLLLLLLDD.......LDLLD.......",
  ".LDDDLLLLLLLDEEDLLLLLLDD......DDLLD.......",
  ".LDDDLLLLLLLLEELLLLLLLLDDL...LDLLLD.......",
  ".LDLLDDLLLLLLLLLLLLLLLLLDDDDDDLLLLD.......",
  "..DLLLDDLLLLLLLLLLLLLLLLLLLDLLLLLDD.......",
  "..DDLLLDDDLLLLLLLLLLLLLLLLLLLLLLLDL.......",
  "..LDLLLLLDDDDDLLLLLLLLLLLLLLLLLLLDL.......",
  "...DDLLLLLLLLLLLLLLLLLLLLLLLLLLLLD........",
  "...LDLLLLLLLLLLLLLLLLLLLLLLLLLLLDL........",
  "....DDLLLLLLLLLLLLLLLLLDLLLLLLLDD.........",
  ".....DDLLLLLLLLLLLLLLLDDLLLLLLLDL.........",
  ".....LDDLLLLLLLLLLLLLLDLLLLLLLDL..........",
  "......LDDLLLLLLDDLLLLLDLLLLLDDL...........",
  ".......LDDLLLLLDDLLLLLDDLLLDD.............",
  ".......LDDDDLLLLDLLLLLDDLDDL..............",
  ".......DDDDDDDLLDLLLLLDDDL................",
  "......LDDDDDDDDDDDLLLLDD..................",
  "......DDDDDL...LDDLLLLDL..................",
  "......DDDL......LDLLLLD...................",
  ".................DLLLDD...................",
  ".................DDLLDL...................",
  ".................LDLDD....................",
  ".................LDDD.....................",
  "..................DD......................",
  "..................L.......................",
];
const WHALE_RGB = { D: "#2e5696", L: "#6fa0e6", E: "#141c2d" };

export function whaleTableHtml(px = 3) {
  const w = WHALE_PIXELS[0].length;
  const rows = WHALE_PIXELS.map((row) => {
    let cells = "";
    for (let i = 0; i < row.length; ) {
      let j = i;
      while (j < row.length && row[j] === row[i]) j++;
      const bg = WHALE_RGB[row[i]];
      cells += `<td${j - i > 1 ? ` colspan="${j - i}"` : ""} width="${(j - i) * px}" height="${px}" style="height:${px}px;line-height:${px}px;font-size:0;padding:0;${bg ? `background:${bg};` : ""}"></td>`;
      i = j;
    }
    return `<tr>${cells}</tr>`;
  }).join("");
  const spacer = `<tr>${Array.from({ length: w }, () => `<td width="${px}" style="height:0;font-size:0;line-height:0;padding:0;"></td>`).join("")}</tr>`;
  return `<table role="presentation" aria-hidden="true" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;margin:0 auto;">${spacer}${rows}</table>`;
}

// Invisible filler after the preheader so Gmail and iOS stop the preview there.
const PREVIEW_PAD = "&#8199;&#65279;&#847; ".repeat(90);

// The weekly brief goes out on the Monday 14:00 UTC schedule in
// .github/workflows/newsletter.yml (cron "0 14 * * 1"). This names the next one so a new
// subscriber knows when to expect mail instead of wondering whether it worked.
export function nextBriefUtc(now = Date.now()) {
  const d = new Date(now);
  const next = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 14, 0, 0));
  while (next.getUTCDay() !== 1 || next.getTime() <= d.getTime()) {
    next.setUTCDate(next.getUTCDate() + 1);
  }
  return next;
}

export function formatBriefDate(date) {
  const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  return `${days[date.getUTCDay()]} ${date.getUTCDate()} ${months[date.getUTCMonth()]}, 14:00 UTC`;
}

export function confirmationMessage(to, confirmUrl, now = Date.now()) {
  const when = formatBriefDate(nextBriefUtc(now));
  const subject = "Confirm your Whale Agent subscription";
  const text = [
    "Someone asked to send the free Whale Agent brief to this address.",
    "If that was you, open this link to confirm:",
    "",
    confirmUrl,
    "",
    `Once you confirm, your first brief arrives ${when}, and then every Monday.`,
    "Every figure in it is copied from a public filing. You can unsubscribe from any",
    "email in one click.",
    "",
    "If it was not you, do nothing. We will not email you again.",
    "",
    "Whale Agent",
  ].join("\n");

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>${escapeHtml(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#141c2d;">
<div style="display:none;font-size:1px;color:#141c2d;max-height:0;overflow:hidden;">Confirm your address and your first brief arrives ${escapeHtml(when)}.${PREVIEW_PAD}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#141c2d;">
<tr><td align="center" style="padding:32px 16px;">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#2e5696;border-radius:10px;">
  <tr><td style="padding:28px 32px 8px 32px;" align="center">
    ${whaleTableHtml()}
  </td></tr>

  <tr><td style="padding:16px 32px 0 32px;" align="center">
    <div style="font-family:Helvetica,Arial,sans-serif;font-size:19px;font-weight:600;color:#f2f2f2;letter-spacing:.2px;">Whale Agent</div>
    <div style="font-family:Helvetica,Arial,sans-serif;font-size:13px;color:rgba(242,242,242,.78);padding-top:5px;">Large public disclosures, ranked and explained</div>
  </td></tr>

  <tr><td style="padding:24px 32px 0 32px;">
    <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#f2f2f2;">Someone asked to send the free Whale Agent brief to this address. If that was you, confirm below.</p>
  </td></tr>

  <tr><td style="padding:24px 32px 0 32px;" align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
      <td align="center" style="background:#f2f2f2;border-radius:6px;">
        <a href="${escapeHtml(confirmUrl)}" style="display:inline-block;padding:13px 30px;font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;color:#141c2d;text-decoration:none;">Confirm my subscription</a>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:22px 32px 0 32px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:rgba(111,160,230,.18);border-radius:8px;">
      <tr><td style="padding:14px 18px;">
        <div style="font-family:Helvetica,Arial,sans-serif;font-size:11px;letter-spacing:.7px;text-transform:uppercase;color:rgba(242,242,242,.62);">Your first brief</div>
        <div style="font-family:Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;color:#f2f2f2;padding-top:4px;">${escapeHtml(when)}</div>
        <div style="font-family:Helvetica,Arial,sans-serif;font-size:13px;color:rgba(242,242,242,.78);padding-top:4px;">Then every Monday at the same time.</div>
      </td></tr>
    </table>
  </td></tr>

  <tr><td style="padding:22px 32px 0 32px;">
    <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:13px;line-height:1.6;color:rgba(242,242,242,.78);">Every figure in the brief is copied from a public filing and links back to it. You can unsubscribe from any email in one click.</p>
  </td></tr>

  <tr><td style="padding:18px 32px 0 32px;">
    <div style="border-top:1px solid rgba(242,242,242,.35);"></div>
  </td></tr>

  <tr><td style="padding:14px 32px 28px 32px;">
    <p style="margin:0;font-family:Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:rgba(242,242,242,.62);">If it was not you, do nothing and we will not email you again. If the button does not work, paste this into your browser:<br><span style="color:rgba(242,242,242,.78);word-break:break-all;">${escapeHtml(confirmUrl)}</span></p>
  </td></tr>
</table>

<div style="font-family:Helvetica,Arial,sans-serif;font-size:11px;color:rgba(242,242,242,.55);padding-top:16px;">Whale Agent is an awareness tool, not investment advice.</div>

</td></tr>
</table>
</body>
</html>`;
  return { to, subject, text, html };
}

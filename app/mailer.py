"""Transactional email via Resend. Deliberately best-effort: if no token is configured or the
API call fails, we log and return False rather than raising — the invite link is always shown
in the UI as a fallback, so email is a convenience, never a hard dependency of any request."""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger("warpyard.mail")
RESEND_URL = "https://api.resend.com/emails"


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    s = get_settings()
    if not s.WARPYARD_RESEND_TOKEN:
        log.info("email disabled (no WARPYARD_RESEND_TOKEN); would have sent %r to %s", subject, to)
        return False
    payload = {"from": s.MAIL_FROM, "to": [to], "subject": subject, "html": html}
    if text:
        payload["text"] = text
    try:
        r = httpx.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {s.WARPYARD_RESEND_TOKEN}"},
            timeout=10,
        )
        if r.status_code >= 300:
            log.warning("resend send failed (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError as e:
        log.warning("resend request error: %s", e)
        return False


def _invite_html(join_url: str, note: str | None) -> str:
    intro = f"You've been invited to <b>Warpyard</b>{f' — {note}' if note else ''}."
    base_domain = get_settings().BASE_DOMAIN
    return f"""\
<div style="margin:0;padding:24px;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d23">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e7e9ee;border-radius:14px;overflow:hidden">
    <div style="padding:24px 28px 8px">
      <div style="display:inline-block;width:26px;height:26px;border-radius:7px;background:#6a7dff;vertical-align:middle"></div>
      <span style="font-size:17px;font-weight:650;letter-spacing:-.02em;vertical-align:middle;margin-left:8px">Warpyard</span>
    </div>
    <div style="padding:8px 28px 28px">
      <h1 style="font-size:20px;font-weight:600;margin:14px 0 6px">You're invited</h1>
      <p style="font-size:14px;line-height:1.6;color:#4a5160;margin:0 0 20px">{intro} Spin up your own
        cloud servers with a public <span style="font-family:ui-monospace,Menlo,monospace;color:#1a1d23">.{base_domain}</span>
        address in about a minute.</p>
      <a href="{join_url}" style="display:inline-block;background:#6a7dff;color:#ffffff;text-decoration:none;
        font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px">Accept your invite</a>
      <p style="font-size:12px;line-height:1.6;color:#8a92a1;margin:20px 0 0">Or paste this link into your browser:<br>
        <a href="{join_url}" style="color:#6a7dff;word-break:break-all">{join_url}</a></p>
      <p style="font-size:12px;color:#a6adba;margin:16px 0 0">This invite can be used once. If you weren't expecting it, you can ignore this email.</p>
    </div>
  </div>
</div>"""


def send_invite(to: str, join_url: str, note: str | None = None) -> bool:
    text = (
        f"You've been invited to Warpyard{f' — {note}' if note else ''}.\n\n"
        f"Accept your invite:\n{join_url}\n\nThis invite can be used once."
    )
    return send_email(to, "Your Warpyard invite", _invite_html(join_url, note), text=text)


def send_invite_request_notice(to: str, requester: str, message: str | None) -> bool:
    """Heads-up to an admin that someone asked for an invite via the public form.
    Requester email + message are untrusted input — escape them."""
    import html as _html

    who = _html.escape(requester)
    msg = _html.escape(message or "")
    invites_url = get_settings().PUBLIC_URL.rstrip("/") + "/invites"
    text = f"{requester} requested a Warpyard invite."
    if message:
        text += f'\n\nTheir note: "{message}"'
    text += f"\n\nApprove or dismiss it: {invites_url}"
    body = f"""\
<div style="margin:0;padding:24px;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d23">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e7e9ee;border-radius:14px;overflow:hidden">
    <div style="padding:24px 28px 8px">
      <div style="display:inline-block;width:26px;height:26px;border-radius:7px;background:#6a7dff;vertical-align:middle"></div>
      <span style="font-size:17px;font-weight:650;letter-spacing:-.02em;vertical-align:middle;margin-left:8px">Warpyard</span>
    </div>
    <div style="padding:8px 28px 28px">
      <h1 style="font-size:20px;font-weight:600;margin:14px 0 6px">Invite request</h1>
      <p style="font-size:14px;line-height:1.6;color:#4a5160;margin:0 0 6px"><b>{who}</b> wants a Warpyard account.</p>
      {f'<p style="font-size:13px;line-height:1.6;color:#4a5160;margin:0 0 14px">&ldquo;{msg}&rdquo;</p>' if msg else ""}
      <a href="{invites_url}" style="display:inline-block;background:#6a7dff;color:#ffffff;text-decoration:none;
        font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px">Review on the Invites page</a>
    </div>
  </div>
</div>"""
    return send_email(to, f"Invite request from {requester}", body, text=text)


def _reset_html(reset_url: str) -> str:
    return f"""\
<div style="margin:0;padding:24px;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d23">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e7e9ee;border-radius:14px;overflow:hidden">
    <div style="padding:24px 28px 8px">
      <div style="display:inline-block;width:26px;height:26px;border-radius:7px;background:#6a7dff;vertical-align:middle"></div>
      <span style="font-size:17px;font-weight:650;letter-spacing:-.02em;vertical-align:middle;margin-left:8px">Warpyard</span>
    </div>
    <div style="padding:8px 28px 28px">
      <h1 style="font-size:20px;font-weight:600;margin:14px 0 6px">Reset your password</h1>
      <p style="font-size:14px;line-height:1.6;color:#4a5160;margin:0 0 20px">Click below to choose a new password.
        This link works once and expires in an hour.</p>
      <a href="{reset_url}" style="display:inline-block;background:#6a7dff;color:#ffffff;text-decoration:none;
        font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px">Reset password</a>
      <p style="font-size:12px;line-height:1.6;color:#8a92a1;margin:20px 0 0">Or paste this link into your browser:<br>
        <a href="{reset_url}" style="color:#6a7dff;word-break:break-all">{reset_url}</a></p>
      <p style="font-size:12px;color:#a6adba;margin:16px 0 0">Didn't ask for this? You can safely ignore this email — your password won't change.</p>
    </div>
  </div>
</div>"""


def send_password_reset(to: str, reset_url: str) -> bool:
    text = f"Reset your Warpyard password (link works once, expires in 1 hour):\n{reset_url}\n\nDidn't ask for this? Ignore this email."
    return send_email(to, "Reset your Warpyard password", _reset_html(reset_url), text=text)


# kind -> (subject template, lead paragraph template)
_ALERTS = {
    "stopped": (
        "Your server '{label}' stopped unexpectedly",
        "<b>{label}</b> ({hostname}) was running and has powered off, and it wasn't through the dashboard. You can boot it again from your dashboard.",
    ),
    "error": (
        "Your server '{label}' hit an error",
        "<b>{label}</b> ({hostname}) ran into a problem the platform couldn't recover from automatically. Check the server page — a reboot or rebuild usually clears it.",
    ),
}


def _alert_html(label: str, hostname: str, lead: str, detail: str, server_url: str) -> str:
    detail_block = (
        f"""<p style="font-size:12px;line-height:1.6;color:#4a5160;background:#f4f5f7;border:1px solid #e7e9ee;
        border-radius:9px;padding:10px 14px;font-family:ui-monospace,Menlo,monospace;word-break:break-word;margin:0 0 20px">{detail}</p>"""
        if detail
        else ""
    )
    return f"""\
<div style="margin:0;padding:24px;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d23">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e7e9ee;border-radius:14px;overflow:hidden">
    <div style="padding:24px 28px 8px">
      <div style="display:inline-block;width:26px;height:26px;border-radius:7px;background:#6a7dff;vertical-align:middle"></div>
      <span style="font-size:17px;font-weight:650;letter-spacing:-.02em;vertical-align:middle;margin-left:8px">Warpyard</span>
    </div>
    <div style="padding:8px 28px 28px">
      <h1 style="font-size:20px;font-weight:600;margin:14px 0 6px">Heads up about {label}</h1>
      <p style="font-size:14px;line-height:1.6;color:#4a5160;margin:0 0 20px">{lead}</p>
      {detail_block}
      <a href="{server_url}" style="display:inline-block;background:#6a7dff;color:#ffffff;text-decoration:none;
        font-weight:600;font-size:14px;padding:11px 20px;border-radius:9px">Open the server page</a>
      <p style="font-size:12px;color:#a6adba;margin:20px 0 0">You get these alerts because you own this server on Warpyard.</p>
    </div>
  </div>
</div>"""


def send_server_alert(
    to: str, label: str, hostname: str, kind: str, detail: str = "", server_id: int | None = None
) -> bool:
    """Owner alert for a server that died (kind: 'stopped' | 'error'). Same best-effort
    semantics as every other mail — never raises, returns False when not sent."""
    # dev/system accounts live on the platform's own domain and nobody reads that
    # mailbox — don't burn email sends on them
    if to.lower().endswith("@" + get_settings().BASE_DOMAIN.lower()):
        return False
    if kind not in _ALERTS:
        log.warning("unknown server alert kind %r", kind)
        return False
    subject_tpl, lead_tpl = _ALERTS[kind]
    subject = subject_tpl.format(label=label)
    lead = lead_tpl.format(label=label, hostname=hostname or "no hostname yet")
    detail = (detail or "")[:300]
    base = get_settings().PUBLIC_URL.rstrip("/")
    server_url = f"{base}/servers/{server_id}" if server_id else base
    text = f"{subject}.\n\n{detail + chr(10) + chr(10) if detail else ''}Server page: {server_url}"
    return send_email(to, subject, _alert_html(label, hostname, lead, detail, server_url), text=text)

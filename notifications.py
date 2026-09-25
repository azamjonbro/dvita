"""Email (Resend) and SMS (Twilio) notification helpers.

All functions are non-blocking and fail gracefully when keys are missing -
they only log a warning instead of raising. Backend never crashes if
credentials are not configured.
"""

import os
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("notify")


def _resend_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def _twilio_configured() -> bool:
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN") and os.environ.get("TWILIO_FROM_NUMBER"))


async def send_email(to_email: Optional[str], subject: str, html: str) -> bool:
    if not to_email:
        return False
    if not _resend_configured():
        logger.warning("Resend API key not configured - skipping email to %s", to_email)
        return False
    try:
        import resend
        resend.api_key = os.environ["RESEND_API_KEY"]
        params = {
            "from": os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev"),
            "to": [to_email],
            "subject": subject,
            "html": html,
        }
        result = await asyncio.to_thread(resend.Emails.send, params)
        logger.info("Email sent to %s id=%s", to_email, (result or {}).get("id"))
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False


async def send_sms(to_phone: Optional[str], body: str) -> bool:
    if not to_phone:
        return False
    if not _twilio_configured():
        logger.warning("Twilio not configured - skipping SMS to %s", to_phone)
        return False
    try:
        from twilio.rest import Client
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

        def _send():
            return client.messages.create(
                body=body,
                from_=os.environ["TWILIO_FROM_NUMBER"],
                to=to_phone,
            )

        msg = await asyncio.to_thread(_send)
        logger.info("SMS sent to %s sid=%s", to_phone, msg.sid)
        return True
    except Exception as e:
        logger.error("Failed to send SMS to %s: %s", to_phone, e)
        return False


def _wrap_html(title: str, message: str, footer: str = "Maison Glow CRM") -> str:
    return f"""\
<!doctype html>
<html><body style="margin:0;padding:0;background:#FAFAF7;font-family:Arial,sans-serif;color:#1A1919">
  <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
    <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid #E5E1D8;border-radius:14px;overflow:hidden">
      <tr><td style="background:#2A1114;padding:18px 24px;color:#FAFAF7">
        <div style="font-family:Georgia,serif;font-size:22px;letter-spacing:-0.01em">Maison<span style="color:#9C433E">·</span>Glow</div>
      </td></tr>
      <tr><td style="padding:28px 24px">
        <h2 style="font-family:Georgia,serif;color:#2A1114;margin:0 0 12px 0;font-size:22px">{title}</h2>
        <div style="font-size:15px;line-height:1.6;color:#1A1919;white-space:pre-line">{message}</div>
      </td></tr>
      <tr><td style="padding:18px 24px;background:#F2EFE9;color:#594F4D;font-size:12px;text-align:center">{footer}</td></tr>
    </table>
  </td></tr></table>
</body></html>"""


async def notify_director(title: str, message: str):
    """Send email + SMS to director (non-blocking, fire-and-forget). Errors swallowed."""
    email = os.environ.get("DIRECTOR_NOTIFY_EMAIL", "")
    phone = os.environ.get("DIRECTOR_NOTIFY_PHONE", "")
    html = _wrap_html(title, message)
    sms_body = f"Maison Glow: {title}\n{message}"
    await asyncio.gather(
        send_email(email, f"[Maison Glow] {title}", html),
        send_sms(phone, sms_body),
        return_exceptions=True,
    )


async def notify_admin(title: str, message: str):
    email = os.environ.get("ADMIN_NOTIFY_EMAIL", "")
    phone = os.environ.get("ADMIN_NOTIFY_PHONE", "")
    html = _wrap_html(title, message)
    sms_body = f"Maison Glow: {title}\n{message}"
    await asyncio.gather(
        send_email(email, f"[Maison Glow] {title}", html),
        send_sms(phone, sms_body),
        return_exceptions=True,
    )

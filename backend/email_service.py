from __future__ import annotations

import os

import resend


def _api_key() -> str:
    key = os.getenv("RESEND_API_KEY", "")
    if not key:
        raise RuntimeError(
            "RESEND_API_KEY is not set. "
            "Add it to your .env file to enable password reset emails."
        )
    return key


def _from_address() -> str:
    return os.getenv("RESEND_FROM_EMAIL", "IMDB AutoFill <onboarding@resend.dev>")


def send_password_reset(to_email: str, code: str) -> None:
    """Send a password reset code to the given address via Resend."""
    resend.api_key = _api_key()
    resend.Emails.send(
        {
            "from": _from_address(),
            "to": [to_email],
            "subject": "Your IMDB AutoFill password reset code",
            "html": _reset_email_html(code),
        }
    )


def _reset_email_html(code: str) -> str:
    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f5f4f2;font-family:Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:48px 16px;">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#1e1c19;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="padding:36px 40px 28px;border-bottom:1px solid rgba(240,225,205,0.08);">
              <p style="margin:0;font-size:13px;font-weight:600;color:#818cf8;
                         letter-spacing:0.5px;text-transform:uppercase;">
                IMDB Auto-Fill
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px 12px;">
              <p style="margin:0 0 8px;font-size:22px;font-weight:700;
                         color:#f0ebe5;letter-spacing:-0.5px;">
                Password reset code
              </p>
              <p style="margin:0;font-size:14px;color:#6b6560;line-height:1.6;">
                Use the code below to reset your password. It expires in
                <strong style="color:#a09890;">10 minutes</strong>.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 40px;">
              <div style="background:rgba(99,102,241,0.1);
                           border:1px solid rgba(99,102,241,0.3);
                           border-radius:10px;padding:20px;text-align:center;">
                <p style="margin:0;font-size:36px;font-weight:700;
                           letter-spacing:10px;color:#818cf8;
                           font-family:'Courier New',monospace;">
                  {code}
                </p>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 40px 36px;">
              <p style="margin:0;font-size:12px;color:#4a4641;line-height:1.6;">
                If you didn't request a password reset, you can safely ignore this email.
                Your password will not change.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

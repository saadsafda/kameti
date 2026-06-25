"""Send FCM push notifications via the Firebase Cloud Messaging HTTP v1 API.

Uses google-auth (already in the Frappe virtualenv) to obtain a short-lived
OAuth2 access token from the service account credentials file, then POSTs to:
  https://fcm.googleapis.com/v1/projects/<project_id>/messages:send

The credentials file path is resolved relative to this module so no extra
site_config setup is needed.
"""

import os
import json
import threading

import frappe
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleRequest

# ---------------------------------------------------------------------------
# Credentials & token cache
# ---------------------------------------------------------------------------

_CREDS_PATH = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_PROJECT_ID = "kameti-notification"
_FCM_URL = f"https://fcm.googleapis.com/v1/projects/{_PROJECT_ID}/messages:send"

_creds_lock = threading.Lock()
_creds: service_account.Credentials | None = None


def _get_access_token() -> str:
    global _creds
    with _creds_lock:
        if _creds is None:
            _creds = service_account.Credentials.from_service_account_file(
                _CREDS_PATH, scopes=[_FCM_SCOPE]
            )
        if not _creds.valid or _creds.expired:
            _creds.refresh(GoogleRequest())
        return _creds.token


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_for_activity(doc, method=None):
    """Frappe after_insert hook — fires for every new Activity document."""
    if not doc.recipient:
        return

    token = frappe.db.get_value(
        "Kameti Profile", {"user": doc.recipient}, "fcm_token"
    )
    if not token:
        return

    send_push(
        token=token,
        title=doc.title or "Kameti",
        body=doc.body or "",
        data={
            "type": doc.type or "",
            "kameti_id": doc.kameti or "",
            "activity_id": doc.name or "",
        },
    )


def send_push(token: str, title: str, body: str, data: dict | None = None):
    """Send a single FCM v1 notification to one device token."""
    try:
        access_token = _get_access_token()
    except Exception as e:
        frappe.logger().error(f"[push] Could not get FCM access token: {e}")
        return

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body,
            },
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "kameti_high",
                    "sound": "default",
                },
            },
            "apns": {
                "payload": {
                    "aps": {
                        "sound": "default",
                        "badge": 1,
                    }
                }
            },
            "data": {k: str(v) for k, v in (data or {}).items()},
        }
    }

    try:
        resp = requests.post(
            _FCM_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if not resp.ok:
            frappe.logger().warning(
                f"[push] FCM v1 error {resp.status_code}: {resp.text[:300]}"
            )
    except Exception as e:
        # Never let a push failure crash the main request.
        frappe.logger().error(f"[push] FCM request failed: {e}")

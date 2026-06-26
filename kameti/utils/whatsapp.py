"""WhatsApp delivery with a selectable backend.

The backend is chosen by `whatsapp_backend` in the **OTP Settings** DocType
(legacy `whatsapp_provider` site_config fallback is still accepted):

  - "meta"     (default) — official Meta WhatsApp Cloud API. Template-based OTP.
  - "vonage"             — Vonage Messages API, including the WhatsApp Sandbox.
                           Free-form text (sandbox has no template approval).
  - "ultramsg"           — UltraMsg gateway (unofficial, QR-linked). Free-form
                           text only. Convenient but the linked number can be
                           banned by WhatsApp — not recommended as the sole
                           production auth channel.

In `developer_mode`, all sends are stubbed (code is logged, no HTTP call).

----------------------------------------------------------------------------
Meta Cloud API config (store in OTP Settings, backend = "meta"):

  OTP Settings fields:
    - whatsapp_phone_number_id
    - whatsapp_access_token
    - whatsapp_api_version
    - whatsapp_otp_template_name
    - whatsapp_otp_template_lang

Setup (one-time, in Meta Business Manager):
  1. Create a Meta Business Account + WhatsApp Business app.
  2. Verify a phone number — note the `phone_number_id`.
  3. Generate a permanent system-user access token (NOT the temporary 24h one).
  4. Create + submit an Authentication template (e.g. `kameti_otp`) for OTP and
     wait for Meta approval before going live.

----------------------------------------------------------------------------
Vonage Messages API config (whatsapp_provider = "vonage"):

  {
    "whatsapp_provider":     "vonage",
    "vonage_api_key":        "abcd1234",
    "vonage_api_secret":     "your-api-secret",
    "vonage_whatsapp_from":  "14157386102",
    "vonage_messages_url":   "https://messages-sandbox.nexmo.com/v1/messages"
  }

Setup for the Sandbox (https://dashboard.vonage.com/messages/sandbox):
  1. Note your API key + secret from the dashboard.
  2. `vonage_whatsapp_from` is the sandbox WhatsApp number (default 14157386102).
  3. EACH recipient must join the sandbox once: from their WhatsApp, message the
     sandbox number with the whitelist phrase shown on the dashboard
     (e.g. "join <two-words>"). Un-joined numbers will error.

For production (not the sandbox), set `vonage_messages_url` to
"https://api.nexmo.com/v1/messages". Basic Auth with the account API key/secret
is what the sandbox uses; production also accepts it for the Messages API.

----------------------------------------------------------------------------
UltraMsg (whatsapp_backend = "ultramsg") — configured in the OTP Settings desk
page (/app/otp-settings), NOT site_config.json:

  - WhatsApp Backend:    ultramsg
  - UltraMsg Instance ID: instance182401
  - UltraMsg Token:       <instance token>   (stored encrypted)
  - UltraMsg Base URL:    https://api.ultramsg.com
  - UltraMsg Priority:    10

Setup (https://user.ultramsg.com): create an instance, scan the QR with the
WhatsApp account that will send messages, and copy the instance id + token.
The OTP message is sent as free-form text via /messages/chat.
"""

import base64

import requests

import frappe

# ---- Meta defaults --------------------------------------------------
META_API_BASE = "https://graph.facebook.com"
META_DEFAULT_VERSION = "v18.0"
DEFAULT_OTP_TEMPLATE = "kameti_otp"
DEFAULT_OTP_LANG = "en"

# ---- Vonage defaults ------------------------------------------------
VONAGE_SANDBOX_URL = "https://messages-sandbox.nexmo.com/v1/messages"
VONAGE_SANDBOX_FROM = "14157386102"

# ---- UltraMsg defaults ----------------------------------------------
ULTRAMSG_BASE_URL = "https://api.ultramsg.com"

# Free-form OTP text shared by the text-only backends (vonage, ultramsg).
OTP_TEXT_MESSAGE = (
	"Your Kameti verification code is {code}. It is valid for 5 minutes. "
	"Do not share it with anyone."
)

HTTP_TIMEOUT_SECONDS = 10


def _settings():
	"""OTP Settings Single doc, or None before it has been migrated."""
	try:
		return frappe.get_cached_doc("OTP Settings")
	except Exception:
		return None


def _provider() -> str:
	"""WhatsApp backend: OTP Settings.whatsapp_backend wins, then site_config,
	then 'meta'."""
	doc = _settings()
	backend = getattr(doc, "whatsapp_backend", None) if doc else None
	if not backend:
		backend = frappe.conf.get("whatsapp_provider")
	return (backend or "meta").lower()


def _otp_text(code: str) -> str:
	return (frappe.conf.get("whatsapp_otp_message") or OTP_TEXT_MESSAGE).format(code=code)


# ---- public API -----------------------------------------------------

def send_otp(phone: str, code: str) -> dict:
	"""Send a 6-digit OTP.

	Returns {provider_id, status}. Raises on failure (the auth caller decides
	how to surface the error to the client).
	"""
	if frappe.conf.developer_mode:
		frappe.logger("kameti").info(f"[DEV-WA-OTP] {phone}: code={code}")
		return {"provider_id": "dev", "status": "sent"}

	provider = _provider()
	if provider == "vonage":
		# Sandbox has no OTP template — deliver the code as free-form text.
		return _vonage_send_text(phone, _otp_text(code))
	if provider == "ultramsg":
		return _ultramsg_send_text(phone, _otp_text(code))

	return _meta_send_otp(phone, code)


def send_text(phone: str, body: str) -> dict:
	"""Send a freeform text message.

	Meta only allows this inside the 24h customer-service window. Vonage allows
	it to any number that has joined the sandbox (or any number in production).
	For business-initiated Meta messages outside the window, use send_template.
	"""
	if frappe.conf.developer_mode:
		frappe.logger("kameti").info(f"[DEV-WA-TEXT] {phone}: {body}")
		return {"provider_id": "dev", "status": "sent"}

	provider = _provider()
	if provider == "vonage":
		return _vonage_send_text(phone, body)
	if provider == "ultramsg":
		return _ultramsg_send_text(phone, body)

	return _meta_send_text(phone, body)


def send_template(
	phone: str,
	template_name: str,
	language: str = "en",
	body_params: list[str] | None = None,
) -> dict:
	"""Send a pre-approved template with optional body parameters.

	Meta: sends the approved template. Vonage sandbox: templates are not
	supported, so this raises — use send_text within the sandbox session.
	"""
	if frappe.conf.developer_mode:
		frappe.logger("kameti").info(
			f"[DEV-WA-TEMPLATE] {phone}: tpl={template_name} params={body_params}"
		)
		return {"provider_id": "dev", "status": "sent"}

	if _provider() in ("vonage", "ultramsg"):
		frappe.throw(
			"WhatsApp templates are only supported on the Meta backend. The "
			"vonage/ultramsg backends send free-form text only — use send_text, "
			"or set OTP Settings.whatsapp_backend to 'meta' for approved templates."
		)

	return _meta_send_template(phone, template_name, language, body_params)


# ---- Meta Cloud API backend ----------------------------------------

def _meta_send_otp(phone: str, code: str) -> dict:
	cfg = _meta_config()
	payload = {
		"messaging_product": "whatsapp",
		"to": phone.lstrip("+"),
		"type": "template",
		"template": {
			"name": cfg["otp_template_name"],
			"language": {"code": cfg["otp_template_lang"]},
			"components": [
				{
					"type": "body",
					"parameters": [{"type": "text", "text": code}],
				},
				{
					"type": "button",
					"sub_type": "url",
					"index": "0",
					"parameters": [{"type": "text", "text": code}],
				},
			],
		},
	}
	return _meta_post(payload, cfg)


def _meta_send_text(phone: str, body: str) -> dict:
	cfg = _meta_config()
	payload = {
		"messaging_product": "whatsapp",
		"to": phone.lstrip("+"),
		"type": "text",
		"text": {"body": body, "preview_url": False},
	}
	return _meta_post(payload, cfg)


def _meta_send_template(
	phone: str,
	template_name: str,
	language: str,
	body_params: list[str] | None,
) -> dict:
	cfg = _meta_config()
	components = []
	if body_params:
		components.append({
			"type": "body",
			"parameters": [{"type": "text", "text": p} for p in body_params],
		})
	payload = {
		"messaging_product": "whatsapp",
		"to": phone.lstrip("+"),
		"type": "template",
		"template": {
			"name": template_name,
			"language": {"code": language},
			"components": components,
		},
	}
	return _meta_post(payload, cfg)


def _meta_config() -> dict:
	doc = _settings()
	conf = frappe.conf
	phone_number_id = getattr(doc, "whatsapp_phone_number_id", None) if doc else None
	access_token = (
		doc.get_password("whatsapp_access_token", raise_exception=False) if doc else None
	)
	api_version = getattr(doc, "whatsapp_api_version", None) if doc else None
	otp_template_name = getattr(doc, "whatsapp_otp_template_name", None) if doc else None
	otp_template_lang = getattr(doc, "whatsapp_otp_template_lang", None) if doc else None

	# Legacy site_config fallback keeps existing sites working until they move
	# the values into OTP Settings.
	if not phone_number_id:
		phone_number_id = conf.get("whatsapp_phone_number_id")
	if not access_token:
		access_token = conf.get("whatsapp_access_token")
	if not api_version:
		api_version = conf.get("whatsapp_api_version")
	if not otp_template_name:
		otp_template_name = conf.get("whatsapp_otp_template_name")
	if not otp_template_lang:
		otp_template_lang = conf.get("whatsapp_otp_template_lang")
	if not phone_number_id or not access_token:
		frappe.throw(
			"WhatsApp Cloud API is not configured. Set "
			"whatsapp_phone_number_id and whatsapp_access_token in OTP Settings."
		)
	return {
		"phone_number_id": phone_number_id,
		"access_token": access_token,
		"api_version": api_version or META_DEFAULT_VERSION,
		"otp_template_name": otp_template_name or DEFAULT_OTP_TEMPLATE,
		"otp_template_lang": otp_template_lang or DEFAULT_OTP_LANG,
	}


def _meta_post(payload: dict, cfg: dict) -> dict:
	url = f"{META_API_BASE}/{cfg['api_version']}/{cfg['phone_number_id']}/messages"
	try:
		response = requests.post(
			url,
			headers={
				"Authorization": f"Bearer {cfg['access_token']}",
				"Content-Type": "application/json",
			},
			json=payload,
			timeout=HTTP_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.logger("kameti").error(f"WhatsApp (meta) network error: {e}")
		raise

	if not response.ok:
		frappe.logger("kameti").error(
			f"WhatsApp (meta) API error {response.status_code}: {response.text}"
		)
		response.raise_for_status()

	data = response.json()
	messages = data.get("messages") or []
	return {
		"provider_id": messages[0].get("id") if messages else None,
		"status": "sent",
	}


# ---- Vonage Messages API backend -----------------------------------

def _vonage_send_text(phone: str, body: str) -> dict:
	cfg = _vonage_config()
	payload = {
		"from": cfg["from"],
		"to": phone.lstrip("+"),
		"channel": "whatsapp",
		"message_type": "text",
		"text": body,
	}
	return _vonage_post(payload, cfg)


def _vonage_config() -> dict:
	conf = frappe.conf
	api_key = conf.get("vonage_api_key")
	api_secret = conf.get("vonage_api_secret")
	if not api_key or not api_secret:
		frappe.throw(
			"Vonage is not configured. Set vonage_api_key and vonage_api_secret "
			"in site_config.json."
		)
	return {
		"api_key": api_key,
		"api_secret": api_secret,
		"from": conf.get("vonage_whatsapp_from") or VONAGE_SANDBOX_FROM,
		"url": conf.get("vonage_messages_url") or VONAGE_SANDBOX_URL,
	}


def _vonage_post(payload: dict, cfg: dict) -> dict:
	token = base64.b64encode(
		f"{cfg['api_key']}:{cfg['api_secret']}".encode()
	).decode()
	try:
		response = requests.post(
			cfg["url"],
			headers={
				"Authorization": f"Basic {token}",
				"Content-Type": "application/json",
				"Accept": "application/json",
			},
			json=payload,
			timeout=HTTP_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.logger("kameti").error(f"WhatsApp (vonage) network error: {e}")
		raise

	if not response.ok:
		frappe.logger("kameti").error(
			f"WhatsApp (vonage) API error {response.status_code}: {response.text}"
		)
		response.raise_for_status()

	data = response.json()
	return {
		"provider_id": data.get("message_uuid"),
		"status": "sent",
	}


# ---- UltraMsg backend ----------------------------------------------

def _ultramsg_send_text(phone: str, body: str) -> dict:
	cfg = _ultramsg_config()
	# UltraMsg accepts E.164 with or without the leading '+'; keep it as-is.
	payload = {
		"token": cfg["token"],
		"to": phone,
		"body": body,
		"priority": cfg["priority"],
	}
	return _ultramsg_post(payload, cfg)


def _ultramsg_config() -> dict:
	doc = _settings()
	if not doc:
		frappe.throw("OTP Settings is not available. Run `bench migrate` first.")
	instance_id = getattr(doc, "ultramsg_instance_id", None)
	token = doc.get_password("ultramsg_token", raise_exception=False)
	if not instance_id or not token:
		frappe.throw(
			"UltraMsg is not configured. Set the UltraMsg Instance ID and Token "
			"in OTP Settings."
		)
	base_url = (getattr(doc, "ultramsg_base_url", None) or ULTRAMSG_BASE_URL).rstrip("/")
	return {
		"token": token,
		"priority": getattr(doc, "ultramsg_priority", None) or 10,
		"url": f"{base_url}/{instance_id}/messages/chat",
	}


def _ultramsg_post(payload: dict, cfg: dict) -> dict:
	try:
		response = requests.post(
			cfg["url"],
			data=payload,
			timeout=HTTP_TIMEOUT_SECONDS,
		)
	except requests.RequestException as e:
		frappe.logger("kameti").error(f"WhatsApp (ultramsg) network error: {e}")
		raise

	if not response.ok:
		frappe.logger("kameti").error(
			f"WhatsApp (ultramsg) HTTP error {response.status_code}: {response.text}"
		)
		response.raise_for_status()

	data = response.json()
	# UltraMsg returns {"sent": "true", "message": "ok", "id": <int>} on success
	# or {"error": "..."} on failure (often with HTTP 200).
	if data.get("error"):
		frappe.logger("kameti").error(f"WhatsApp (ultramsg) API error: {data['error']}")
		frappe.throw(f"UltraMsg send failed: {data['error']}")

	return {
		"provider_id": data.get("id"),
		"status": "sent",
	}

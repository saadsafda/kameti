"""WhatsApp delivery with a selectable backend.

The backend is chosen by `whatsapp_provider` in `sites/<site>/site_config.json`:

  - "meta"   (default) — official Meta WhatsApp Cloud API. Template-based OTP.
  - "vonage"           — Vonage Messages API, including the WhatsApp Sandbox.
                         Free-form text (sandbox has no template approval).

In `developer_mode`, all sends are stubbed (code is logged, no HTTP call).

----------------------------------------------------------------------------
Meta Cloud API config (whatsapp_provider = "meta"):

  {
    "whatsapp_provider":           "meta",
    "whatsapp_phone_number_id":    "1234567890",
    "whatsapp_access_token":       "EAAG...",
    "whatsapp_api_version":        "v18.0",
    "whatsapp_otp_template_name":  "kameti_otp",
    "whatsapp_otp_template_lang":  "en"
  }

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
VONAGE_OTP_MESSAGE = (
	"Your Kameti verification code is {code}. It is valid for 5 minutes. "
	"Do not share it with anyone."
)

HTTP_TIMEOUT_SECONDS = 10


def _provider() -> str:
	return (frappe.conf.get("whatsapp_provider") or "meta").lower()


# ---- public API -----------------------------------------------------

def send_otp(phone: str, code: str) -> dict:
	"""Send a 6-digit OTP.

	Returns {provider_id, status}. Raises on failure (the auth caller decides
	how to surface the error to the client).
	"""
	if frappe.conf.developer_mode:
		frappe.logger("kameti").info(f"[DEV-WA-OTP] {phone}: code={code}")
		return {"provider_id": "dev", "status": "sent"}

	if _provider() == "vonage":
		# Sandbox has no OTP template — deliver the code as free-form text.
		message = (
			frappe.conf.get("vonage_otp_message") or VONAGE_OTP_MESSAGE
		).format(code=code)
		return _vonage_send_text(phone, message)

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

	if _provider() == "vonage":
		return _vonage_send_text(phone, body)

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

	if _provider() == "vonage":
		frappe.throw(
			"WhatsApp templates are not supported on the Vonage backend "
			"(the Sandbox sends free-form text only). Use send_text instead, "
			"or switch whatsapp_provider to 'meta' for approved templates."
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
	conf = frappe.conf
	phone_number_id = conf.get("whatsapp_phone_number_id")
	access_token = conf.get("whatsapp_access_token")
	if not phone_number_id or not access_token:
		frappe.throw(
			"WhatsApp Cloud API is not configured. Set "
			"whatsapp_phone_number_id and whatsapp_access_token in site_config.json."
		)
	return {
		"phone_number_id": phone_number_id,
		"access_token": access_token,
		"api_version": conf.get("whatsapp_api_version") or META_DEFAULT_VERSION,
		"otp_template_name": (
			conf.get("whatsapp_otp_template_name") or DEFAULT_OTP_TEMPLATE
		),
		"otp_template_lang": (
			conf.get("whatsapp_otp_template_lang") or DEFAULT_OTP_LANG
		),
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

"""SMS dispatch via Twilio.

OTP delivery now goes over WhatsApp (`kameti.utils.whatsapp`). This module
remains available for the `sms` reminder channel and as a possible future
OTP fallback.

In `developer_mode`, sends are stubbed (message is logged, no HTTP call).
"""

import frappe


def send_sms(phone: str, body: str) -> dict:
	if frappe.conf.developer_mode:
		frappe.logger("kameti").info(f"[DEV-SMS] {phone}: {body}")
		return {"provider_id": "dev", "status": "sent"}

	sid = frappe.conf.get("twilio_account_sid")
	if not sid:
		frappe.throw("SMS provider is not configured.")

	try:
		from twilio.rest import Client
	except ImportError:
		frappe.throw("twilio package is not installed. `pip install twilio` and restart.")

	client = Client(sid, frappe.conf.get("twilio_auth_token"))
	msg = client.messages.create(
		to=phone,
		from_=frappe.conf.get("twilio_from_number"),
		body=body,
	)
	return {"provider_id": msg.sid, "status": "sent"}

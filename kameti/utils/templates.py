"""Multi-language reminder message rendering."""

from frappe.utils import flt


METHOD_LABELS = {
	"easypaisa": "Easypaisa",
	"jazzcash": "JazzCash",
	"bank": "Bank",
	"cash": "Cash",
	"other": "",
}


def render_reminder(
	channel: str,
	language: str,
	*,
	member_name: str,
	kameti_title: str,
	amount: float,
	due_date: str,
	admin_name: str,
	payment_account: dict,
	payment_link: str | None = None,
) -> str:
	account = payment_account or {}
	method = account.get("method", "")
	acct_no = account.get("account_number", "")
	method_label = METHOD_LABELS.get(method, method)
	if method == "bank" and account.get("bank_name"):
		method_label = account["bank_name"]
	amt = f"Rs. {int(flt(amount)):,}"

	if language == "ur":
		body = (
			f"السلام علیکم {member_name}،\n"
			f"کمیٹی '{kameti_title}' کی قسط {amt} {due_date} تک ادا کریں۔\n"
			f"{method_label}: {acct_no} ({admin_name})\n"
			f"ادائیگی کی رسید ایپ میں اپلوڈ کریں۔"
		)
	elif language == "rom":
		body = (
			f"Assalamu alaikum {member_name}!\n"
			f"Aap ki kameti '{kameti_title}' ki qist {amt} {due_date} tak ada karein.\n"
			f"{method_label}: {acct_no} ({admin_name})\n"
			f"Payment ki screenshot app mein upload karein."
		)
	else:
		body = (
			f"Hi {member_name},\n"
			f"Your installment of {amt} for {kameti_title} is due by {due_date}.\n"
			f"Send to {method_label} {acct_no} ({admin_name}) and upload the screenshot in the app."
		)

	if payment_link and channel != "sms":
		body += f"\n{payment_link}"
	return body

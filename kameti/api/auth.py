"""Phone + OTP authentication. See spec §3."""

import frappe
from frappe.exceptions import DuplicateEntryError
from frappe.model.rename_doc import rename_doc

from kameti.utils import common, otp, whatsapp


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_otp(phone: str, purpose: str = "login"):
	common.validate_e164(phone)
	if purpose not in ("login", "register", "phone_change"):
		frappe.throw("Invalid purpose.", frappe.ValidationError)

	settings = otp.get_settings()
	otp.check_rate_limit(phone, settings=settings)
	code, provider = otp.create_otp(
		phone, purpose,
		requested_ip=getattr(frappe.local, "request_ip", None),
		settings=settings,
	)
	try:
		if provider == "whatsapp":
			whatsapp.send_otp(phone, code)
		elif provider == "sms":
			from kameti.utils import sms as sms_provider
			sms_provider.send_sms(
				phone, f"Your Kameti code is {code}. Valid for 5 minutes.",
			)
	except whatsapp.NotOnWhatsAppError:
		# Nothing was delivered — drop the unusable code and let the user retry
		# immediately with a different number instead of burning their quota.
		frappe.db.rollback()
		raise
	except whatsapp.WhatsAppDeliveryError:
		frappe.db.rollback()
		raise
	except Exception:
		# Any other provider failure: log the detail for us, show the user a
		# plain message rather than a raw traceback.
		frappe.log_error(
			title="OTP send failed",
			message=frappe.get_traceback(),
		)
		frappe.db.rollback()
		frappe.throw(
			"We couldn't send your verification code right now. Please try "
			"again in a moment.",
			whatsapp.WhatsAppDeliveryError,
		)
	# provider == "console": code is in Phone OTP doctype, nothing to send.
	otp.record_request(phone, settings=settings)
	frappe.db.commit()
	return {
		"ok": True,
		"provider": provider,
		"expires_in": int(settings["otp_ttl_seconds"]),
		"resend_after": int(settings["resend_interval_seconds"]),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def check_otp(phone: str, code: str, purpose: str = "login"):
	"""Verify the code without doing the login flow.

	Consumes the OTP (so a valid code can't be reused) and bumps attempts on a
	miss. Returns {ok: True} on success; throws the same errors as verify_otp
	on failure. Use this when you want to confirm the OTP system works
	end-to-end without creating a user or rotating API keys.
	"""
	common.validate_e164(phone)
	if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
		frappe.throw("Code must be 6 digits.", frappe.ValidationError)
	otp.consume_otp(phone, code, purpose)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def verify_otp(phone: str, code: str, purpose: str = "login"):
	common.validate_e164(phone)
	if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
		frappe.throw("Code must be 6 digits.", frappe.ValidationError)

	otp.consume_otp(phone, code, purpose)

	user_name, has_profile = _resolve_user_for_phone(phone)
	if user_name:
		user_doc = frappe.get_doc("User", user_name)
		is_new = not has_profile
	else:
		user_doc = _create_user_for_phone(phone)
		is_new = True

	api_key, api_secret = _rotate_api_keys(user_doc)
	frappe.db.commit()

	result = {
		"ok": True,
		"is_new_user": is_new,
		"user": phone,
		"api_key": api_key,
		"api_secret": api_secret,
	}
	if not is_new:
		result["profile"] = _profile_payload(user_doc.name)
	return result


@frappe.whitelist(methods=["POST"])
def complete_registration(
	display_name: str,
	urdu_name: str | None = None,
	language: str = "en",
	avatar_tone: str | None = None,
	gender: str | None = None,
	date_of_birth: str | None = None,
	avatar_image: str | None = None,
):
	user_name = common.require_session_user()
	if not display_name or not display_name.strip():
		frappe.throw("Display name is required.", frappe.ValidationError)
	display_name = display_name.strip()[:60]

	profile_name = frappe.db.get_value("Kameti Profile", {"user": user_name}, "name")
	if profile_name:
		profile = frappe.get_doc("Kameti Profile", profile_name)
	else:
		profile = frappe.new_doc("Kameti Profile")
		profile.user = user_name
		profile.phone = common.phone_for_email(user_name)

	profile.display_name = display_name
	if urdu_name is not None:
		profile.urdu_name = urdu_name
	if language in ("en", "rom", "ur"):
		profile.language = language
	if avatar_tone in common.AVATAR_TONES:
		profile.avatar_tone = avatar_tone
	elif not profile.avatar_tone:
		profile.avatar_tone = common.random_tone()
	if gender in ("Male", "Female", "Other"):
		profile.gender = gender
	if date_of_birth:
		profile.date_of_birth = date_of_birth
	if avatar_image:
		profile.avatar_image = avatar_image

	profile.save(ignore_permissions=True)
	frappe.db.set_value(
		"User", user_name, "first_name", display_name, update_modified=False,
	)
	frappe.db.set_value(
		"User", user_name, "full_name", display_name, update_modified=False,
	)
	frappe.db.commit()
	return {"ok": True, "profile": _profile_payload(user_name)}


@frappe.whitelist(methods=["POST"])
def sign_out():
	user_name = frappe.session.user
	if not user_name or user_name == "Guest":
		return {"ok": True}
	frappe.db.set_value(
		"User", user_name,
		{"api_key": "", "api_secret": ""},
		update_modified=False,
	)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def delete_account():
	"""Permanently delete the caller's account (App Store Guideline 5.1.1(v)).

	Personal data is deleted outright: Kameti Profile, profile photo, push
	token, the caller's own notifications and OTP rows. The User row itself
	is anonymised and disabled rather than hard-deleted, because other members'
	financial records (payments reviewed, reminders sent, completed kametis)
	link to it; the rename rewrites every link, owner and modified_by so the
	phone-derived id disappears from the database.

	Kametis:
	- The caller runs one with payments that is still in progress → refused
	  (409). They must transfer ownership or finish/archive it first, otherwise
	  the other members would be left with an orphaned, unmanageable group.
	- The caller runs one with no payments yet → deleted entirely (members are
	  notified), exactly like `kameti.delete_committee`.
	- Any other kameti the caller is in: if no payments yet, their membership
	  is removed and payout slots freed; if payments exist, the membership is
	  kept as an unlinked ledger entry (name and phone as the organiser
	  entered them) so the group's payout history stays correct.
	"""
	from kameti.api.kameti import purge_committee, settlement_started

	user_name = common.require_session_user()
	if user_name == "Administrator" or "System Manager" in frappe.get_roles(user_name):
		frappe.throw("System users cannot be deleted from the app.", frappe.PermissionError)

	running = [
		c.title for c in frappe.get_all(
			"Kameti Committee",
			filters={"admin": user_name, "archived": 0, "cycle_state": ("!=", "completed")},
			fields=["name", "title"],
		)
		if settlement_started(c.name)
	]
	if running:
		names = ", ".join(f'"{t}"' for t in running)
		frappe.throw(
			f"You are the admin of {names}, which already has payments. Transfer "
			"ownership to another member (or archive it) before deleting your account.",
			DuplicateEntryError,  # → 409 / ApiConflictException in the app
		)

	# 1. Kametis the caller runs that have no money in them: delete outright.
	for name in frappe.get_all("Kameti Committee", filters={"admin": user_name}, pluck="name"):
		if not settlement_started(name):
			purge_committee(frappe.get_doc("Kameti Committee", name))

	# 2. Every remaining membership: remove, or keep as an unlinked ledger row.
	for mem in frappe.get_all(
		"Kameti Membership", filters={"user": user_name}, fields=["name", "kameti", "status"],
	):
		if mem.status == "active" and not settlement_started(mem.kameti):
			_release_membership_slots(mem.name, mem.kameti)
			frappe.db.set_value("Kameti Membership", mem.name, {
				"status": "removed",
				"removed_on": frappe.utils.now_datetime(),
				"user": None,
				"phone": "",
				"payout_month": None,
				"assignment_method": None,
			})
		else:
			frappe.db.set_value("Kameti Membership", mem.name, "user", None)

	# 3. Personal data.
	profile = frappe.db.get_value(
		"Kameti Profile", {"user": user_name}, ["name", "phone", "avatar_image"], as_dict=True,
	)
	phone = (profile and profile.phone) or (
		common.phone_for_email(user_name) if user_name.endswith("@kameti.local") else None
	)
	if profile:
		if profile.avatar_image:
			for f in frappe.get_all("File", filters={"file_url": profile.avatar_image}, pluck="name"):
				frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc("Kameti Profile", profile.name, force=True, ignore_permissions=True,
						  delete_permanently=True)
	frappe.db.delete("Activity", {"recipient": user_name})
	if phone:
		frappe.db.delete("Phone OTP", {"phone": phone})
	for row in frappe.get_all(
		"Scheduled Notification Recipient", filters={"user": user_name}, pluck="name",
	):
		frappe.db.set_value("Scheduled Notification Recipient", row,
							{"display_name": "Deleted user", "phone": ""}, update_modified=False)

	# 4. Anonymise + disable the User. Field updates happen BEFORE the rename:
	# anything saved after it would stamp the old id back into modified_by.
	anon = f"deleted-{frappe.generate_hash(length=12)}@kameti.local"
	user = frappe.get_doc("User", user_name)
	user.update({
		"enabled": 0,
		"first_name": "Deleted user",
		"middle_name": None,
		"last_name": None,
		"username": None,
		"phone": None,
		"mobile_no": None,
		"birth_date": None,
		"gender": None,
		"user_image": None,
		"api_key": None,
		"api_secret": None,
	})
	user.flags.ignore_permissions = True
	user.save()
	# The low-level rename (not frappe.rename_doc) is the one that accepts
	# ignore_permissions — the caller is a Website User renaming themselves.
	rename_doc(doctype="User", old=user_name, new=anon, force=True, ignore_permissions=True,
			   show_alert=False, rebuild_search=False)
	frappe.db.set_value("User", anon, "email", anon, update_modified=False)
	# rename_doc leaves "renamed from <old id>" comments and Version diffs on
	# the User and on docs named after it (e.g. Notification Settings).
	frappe.db.delete("Comment", {"reference_name": anon})
	frappe.db.delete("Version", {"docname": anon})
	frappe.db.delete("Activity Log", {"user": anon})
	frappe.db.commit()
	return {"ok": True}


# ---- helpers ------------------------------------------------------

def _release_membership_slots(membership: str, kameti: str) -> None:
	"""Free every payout slot and split share a membership holds (a member can
	hold several months — the "double share" case)."""
	from kameti.api.roster import drop_slot_share

	for share in frappe.get_all(
		"Slot Share", filters={"kameti": kameti, "membership": membership},
		fields=["name", "payout_slot"],
	):
		month = frappe.db.get_value("Payout Slot", share.payout_slot, "month_index")
		drop_slot_share(share.payout_slot, share.name, membership, month)
	for slot in frappe.get_all(
		"Payout Slot", filters={"kameti": kameti, "recipient": membership}, pluck="name",
	):
		frappe.db.set_value("Payout Slot", slot, {
			"recipient": None, "assignment_method": None, "assigned_on": None,
		})


def _resolve_user_for_phone(phone: str) -> tuple[str | None, bool]:
	"""Return (user_name, has_profile) for a phone number."""
	user_name = common.user_name_for_phone(phone)
	if user_name:
		return user_name, True

	user_name = _user_name_from_identity(phone)
	if not user_name:
		return None, False

	has_profile = bool(frappe.db.exists("Kameti Profile", {"user": user_name}))
	return user_name, has_profile


def _user_name_from_identity(phone: str) -> str | None:
	email = common.email_for_phone(phone)
	user_name = frappe.db.get_value("User", {"email": email}, "name")
	if user_name:
		return user_name
	return frappe.db.get_value("User", {"username": phone[1:]}, "name")


def _create_user_for_phone(phone: str):
	existing = _user_name_from_identity(phone)
	if existing:
		return frappe.get_doc("User", existing)

	email = common.email_for_phone(phone)
	user = frappe.new_doc("User")
	user.email = email
	user.username = phone[1:]
	user.first_name = "User"
	user.enabled = 1
	user.user_type = "Website User"
	user.send_welcome_email = 0
	user.flags.no_welcome_mail = True
	user.append("roles", {"role": "Kameti Member"})
	user.append("roles", {"role": "Kameti Admin"})
	try:
		user.insert(ignore_permissions=True)
	except DuplicateEntryError:
		existing = _user_name_from_identity(phone)
		if existing:
			return frappe.get_doc("User", existing)
		raise
	return user


def _rotate_api_keys(user_doc) -> tuple[str, str]:
	api_key = frappe.generate_hash(length=15)
	api_secret = frappe.generate_hash(length=15)
	user_doc.api_key = api_key
	user_doc.api_secret = api_secret
	user_doc.save(ignore_permissions=True)
	return api_key, api_secret


def _profile_payload(user_name: str) -> dict:
	from kameti.api.profile import profile_completion_percent

	p = frappe.db.get_value(
		"Kameti Profile", {"user": user_name},
		["display_name", "urdu_name", "phone", "gender", "date_of_birth",
		 "language", "dark_mode", "avatar_tone", "avatar_image"],
		as_dict=True,
	) or {}
	dob = p.get("date_of_birth")
	return {
		"display_name": p.get("display_name"),
		"urdu_name": p.get("urdu_name"),
		"gender": p.get("gender"),
		"date_of_birth": dob.isoformat() if dob else None,
		"language": p.get("language") or "en",
		"dark_mode": bool(p.get("dark_mode")),
		"avatar_tone": p.get("avatar_tone") or "clay",
		"avatar_url": p.get("avatar_image"),
		"profile_completion_percent": profile_completion_percent(p),
	}

"""Profile read/write. See spec §4.2 and §4.3."""

import frappe

from kameti.utils import common


EDITABLE_FIELDS = {
	"display_name", "urdu_name", "language", "dark_mode",
	"avatar_tone", "avatar_image", "gender", "date_of_birth",
}

# Fields required for a profile to be considered 100% complete — each worth
# an equal share (20% apiece for these 5).
COMPLETION_FIELDS = (
	"display_name", "phone", "gender", "date_of_birth", "avatar_image",
)


@frappe.whitelist(methods=["GET"])
def get_profile():
	user = common.require_session_user()
	return _profile_dict(user)


@frappe.whitelist(methods=["POST"])
def update_profile(**kwargs):
	user = common.require_session_user()
	profile_name = frappe.db.get_value("Kameti Profile", {"user": user}, "name")
	if not profile_name:
		frappe.throw(
			"Profile not found. Complete registration first.",
			frappe.DoesNotExistError,
		)
	profile = frappe.get_doc("Kameti Profile", profile_name)
	for k, v in kwargs.items():
		if k in EDITABLE_FIELDS:
			if k == "dark_mode":
				v = 1 if v else 0
			setattr(profile, k, v)
	profile.save(ignore_permissions=True)
	if "display_name" in kwargs:
		frappe.db.set_value(
			"User", user, "first_name", kwargs["display_name"],
			update_modified=False,
		)
	frappe.db.commit()
	return _profile_dict(user)


@frappe.whitelist(methods=["POST"])
def delete_avatar():
	user = common.require_session_user()
	profile_name = frappe.db.get_value("Kameti Profile", {"user": user}, "name")
	if not profile_name:
		frappe.throw(
			"Profile not found. Complete registration first.",
			frappe.DoesNotExistError,
		)
	profile = frappe.get_doc("Kameti Profile", profile_name)
	old_file = profile.avatar_image
	profile.avatar_image = None
	profile.save(ignore_permissions=True)
	if old_file:
		for file_name in frappe.get_all(
			"File", filters={"file_url": old_file}, pluck="name",
		):
			frappe.delete_doc("File", file_name, ignore_permissions=True)
	frappe.db.commit()
	return _profile_dict(user)


def ensure_profile_for_user(doc, method=None):
	# Profile creation is owned by the auth flow. Hook is wired for forward use.
	pass


def profile_completion_percent(p) -> int:
	"""% of COMPLETION_FIELDS that are filled in on a Kameti Profile doc/dict."""
	filled = sum(1 for f in COMPLETION_FIELDS if p.get(f))
	return round(filled * 100 / len(COMPLETION_FIELDS))


def _profile_dict(user_name: str) -> dict:
	p = frappe.db.get_value(
		"Kameti Profile", {"user": user_name},
		["display_name", "urdu_name", "phone", "gender", "date_of_birth",
		 "language", "dark_mode", "avatar_tone", "avatar_image"],
		as_dict=True,
	)
	if not p:
		frappe.throw("Profile not found.", frappe.DoesNotExistError)
	created = frappe.db.get_value("User", user_name, "creation")
	kc = frappe.db.count(
		"Kameti Membership", {"user": user_name, "status": "active"},
	)
	return {
		"display_name": p.display_name,
		"urdu_name": p.urdu_name,
		"phone": p.phone,
		"gender": p.gender,
		"date_of_birth": p.date_of_birth.isoformat() if p.date_of_birth else None,
		"language": p.language or "en",
		"dark_mode": bool(p.dark_mode),
		"avatar_tone": p.avatar_tone or "clay",
		"avatar_url": p.avatar_image,
		"joined_on": created.isoformat() if created else None,
		"kametis_count": kc,
		"profile_completion_percent": profile_completion_percent(p),
	}

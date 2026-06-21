"""Settings shortcut. See spec §13. Thin wrapper around profile.update_profile."""

import frappe

from kameti.api import profile as _profile


@frappe.whitelist(methods=["POST"])
def update(language: str | None = None, dark_mode: bool | None = None):
	kwargs = {}
	if language is not None:
		kwargs["language"] = language
	if dark_mode is not None:
		kwargs["dark_mode"] = 1 if dark_mode else 0
	return _profile.update_profile(**kwargs)

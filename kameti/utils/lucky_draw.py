"""Auditable fair-draw using secrets.SystemRandom."""

import hashlib
import secrets

import frappe


def draw(eligible_membership_ids: list[str]) -> dict:
	if not eligible_membership_ids:
		frappe.throw("No eligible members for the draw.", frappe.ValidationError)

	# Sort for stable audit (the animation needs a deterministic order).
	ordered = sorted(eligible_membership_ids)
	rng = secrets.SystemRandom()
	seed_token = secrets.token_hex(32)
	selected_index = rng.randrange(len(ordered))
	winner = ordered[selected_index]
	seed_hash = "sha256:" + hashlib.sha256(seed_token.encode("utf-8")).hexdigest()
	return {
		"winner_id": winner,
		"audit": {
			"eligible_membership_ids": ordered,
			"selected_index": selected_index,
			"seed_hash": seed_hash,
		},
	}

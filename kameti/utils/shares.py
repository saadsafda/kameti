"""How much a member owes each month. The ONE place this rule lives.

Every Payout Slot is one share of the kameti. A member's share units are:

	(solo slots they hold × 1) + (their fraction of each split slot)

e.g. two full slots = 2.0, a half slot = 0.5, a full + a half = 1.5. A member
who has no slot yet (lucky draw still to come) holds the default 1.0.

Every month a member pays `installment × share units` until the kameti ends,
EXCEPT for the share(s) receiving that month's payout: the pot is
`(members_count − 1) × installment`, i.e. the receiving share never pays into
its own month. So:

	due(month) = installment × (share units − units receiving in `month`)

Two-slot holder: 2× every month, 1× in each of their two payout months.
Half holder: 0.5× every month, 0 in the month their split slot pays out.
Summed over all members this always equals the payout pot.
"""

import math

import frappe


def share_units(kameti: str, membership: str) -> float:
	"""Total shares a member holds in this kameti (default 1.0 if none yet)."""
	solo = frappe.db.count(
		"Payout Slot", {"kameti": kameti, "recipient": membership, "is_split": 0},
	)
	split = sum(
		frappe.get_all(
			"Slot Share",
			filters={"kameti": kameti, "membership": membership},
			pluck="share_fraction",
		)
	)
	units = solo + float(split or 0)
	return units if units > 0 else 1.0


def receiving_units(kameti: str, membership: str, month: int) -> float:
	"""How much of `month`'s payout slot this member holds (0, a fraction, or 1)."""
	if not month:
		return 0.0
	slot = frappe.db.get_value(
		"Payout Slot", {"kameti": kameti, "month_index": month},
		["name", "recipient", "is_split"], as_dict=True,
	)
	if not slot:
		return 0.0
	if slot.is_split:
		return float(frappe.db.get_value(
			"Slot Share", {"payout_slot": slot.name, "membership": membership},
			"share_fraction",
		) or 0)
	return 1.0 if slot.recipient == membership else 0.0


def monthly_due(kameti: str, membership: str, month: int, installment: float) -> int:
	"""Rupees this member owes for `month`, rounded to the nearest whole rupee
	(the app sends whole rupees; a 1/3 share of 10,000 is 3,333)."""
	owed_units = share_units(kameti, membership) - receiving_units(kameti, membership, month)
	if owed_units <= 1e-9:
		return 0
	return math.floor(float(installment or 0) * owed_units + 0.5)

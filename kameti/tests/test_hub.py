from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from kameti.api import hub


class TestHubSummary(TestCase):
	def test_contributions_include_paid_and_recipient_but_exclude_completed(self):
		memberships = [
			frappe._dict(name="MEM-1", kameti="KAM-1", role="admin"),
			frappe._dict(name="MEM-2", kameti="KAM-2", role="admin"),
			frappe._dict(name="MEM-3", kameti="KAM-3", role="member"),
			frappe._dict(name="MEM-4", kameti="KAM-4", role="member"),
			frappe._dict(name="MEM-5", kameti="KAM-5", role="member"),
		]
		committees = {
			"KAM-1": self._committee("KAM-1", 1_000_000, "not_started"),
			"KAM-2": self._committee("KAM-2", 1_000, "not_started"),
			"KAM-3": self._committee("KAM-3", 5_000, "not_started"),
			"KAM-4": self._committee("KAM-4", 5_000, "active"),
			"KAM-5": self._committee("KAM-5", 40_000, "completed"),
		}

		def get_value(doctype, name, fields, as_dict=False):
			self.assertEqual(doctype, "Kameti Committee")
			self.assertTrue(as_dict)
			return committees[name]

		recipients = {
			"KAM-1": {"membership_id": "MEM-1"},
			"KAM-2": {"membership_id": "MEM-99"},
			"KAM-3": {"membership_id": "MEM-98"},
			"KAM-4": {"membership_id": "MEM-97"},
			"KAM-5": {"membership_id": "MEM-96"},
		}
		fake_db = Mock()
		fake_db.get_value.side_effect = get_value
		fake_db.count.return_value = 0

		with (
			patch.object(hub.common, "require_session_user", return_value="user@example.com"),
			patch.object(hub.frappe, "get_all", return_value=memberships),
			patch.object(hub.frappe, "db", fake_db),
			patch.object(
				hub,
				"_current_recipient",
				side_effect=lambda kameti, _month: recipients[kameti],
			),
			patch.object(
				hub,
				"_payment_counts",
				return_value={"paid": 1, "pending": 0, "unpaid": 0},
			),
			patch.object(hub, "_last_payer", return_value=None),
		):
			result = hub.get_my_kametis.__wrapped__()

		self.assertEqual(result["total_contributions_this_month"], 1_011_000)
		self.assertEqual(result["contribution_count"], 4)
		self.assertEqual(result["total_owed_this_month"], 1_011_000)
		self.assertEqual(result["owed_count"], 4)
		self.assertEqual(len(result["kametis"]), 5)

	@staticmethod
	def _committee(name, installment_amount, cycle_state):
		return frappe._dict(
			name=name,
			title=name,
			urdu_title=None,
			short_code=name[-1],
			tone="clay",
			members_count=1,
			installment_amount=installment_amount,
			current_month=1,
			duration_months=1,
			cycle_state=cycle_state,
			archived=0,
		)

# Copyright (c) 2026, Yasir Shaikh and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_vs import copilot


class TestCopilot(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_a_field_is_added_and_can_be_taken_back(self):
		change = copilot.add_field("Item", "PO Number", "Data", insert_after="item_name")
		plan = copilot.propose("Add PO Number to Item", "add a PO number to items", [change])
		self.assertEqual(plan["status"], "Draft")
		# Nothing has happened to the site yet.
		self.assertIsNone(frappe.get_meta("Item").get_field("po_number"))

		applied = copilot.apply(plan["name"])
		self.assertEqual(applied["status"], "Applied")
		frappe.clear_cache(doctype="Item")
		self.assertIsNotNone(frappe.get_meta("Item").get_field("po_number"))

		undone = copilot.undo(plan["name"])
		self.assertEqual(undone["status"], "Undone")
		frappe.clear_cache(doctype="Item")
		self.assertIsNone(frappe.get_meta("Item").get_field("po_number"))

	def test_a_property_goes_back_to_what_it_was(self):
		before = frappe.get_meta("Item").get_field("item_name").label
		change = copilot.set_property("Item", "item_name", "label", "Product Name")
		plan = copilot.propose("Rename Item Name", "call it Product Name", [change])
		copilot.apply(plan["name"])
		frappe.clear_cache(doctype="Item")
		self.assertEqual(frappe.get_meta("Item").get_field("item_name").label, "Product Name")

		copilot.undo(plan["name"])
		frappe.clear_cache(doctype="Item")
		self.assertEqual(frappe.get_meta("Item").get_field("item_name").label, before)

	def test_it_refuses_what_it_should_not_touch(self):
		# Business data is out of reach, whatever the model asks for.
		self.assertRaises(
			frappe.ValidationError,
			copilot.propose,
			"Delete an invoice",
			"remove that invoice",
			[{"action": "Delete", "ref_doctype": "Sales Invoice", "ref_name": "SINV-0001"}],
		)
		# So is a field on a doctype that does not exist, or of a type it cannot add.
		self.assertRaises(frappe.ValidationError, copilot.add_field, "Not A Doctype", "X")
		self.assertRaises(frappe.ValidationError, copilot.add_field, "Item", "X", "Table")
		# A report is a SELECT and nothing else.
		self.assertRaises(
			frappe.ValidationError, copilot.create_report, "Bad", "Item", "delete from tabItem"
		)

	def test_a_failed_change_set_leaves_nothing_behind(self):
		good = copilot.add_field("Item", "Shelf Code", "Data")
		bad = {
			"action": "Update",
			"ref_doctype": "Property Setter",
			"ref_name": "does-not-exist",
			"summary": "nonsense",
			"payload": "{}",
		}
		plan = copilot.propose("Half broken", "two changes, one impossible", [good, bad])
		self.assertRaises(frappe.DoesNotExistError, copilot.apply, plan["name"])

		frappe.clear_cache(doctype="Item")
		self.assertIsNone(frappe.get_meta("Item").get_field("shelf_code"))
		self.assertEqual(frappe.db.get_value("VS Change Set", plan["name"], "status"), "Failed")

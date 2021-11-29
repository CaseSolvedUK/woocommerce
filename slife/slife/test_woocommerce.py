# Copyright (c) 2021, Slife
# For license information, please see license.txt

# Run tests with: bench --site <site> --verbose run-tests --module slife.slife.test_woocommerce

import frappe
import unittest

class TestWoocommerce(unittest.TestCase):
	"""
	Submit Woocommerce test orders
	Requires:
	Frappe & ERPNext version-13 after 26th August 2021 for SO coupon discount support
	Woocommerce settings configured including a secret
	Fiscal Years (for customer)
	Item templates for any variants with attributes that match woocommerce suffixes
	Item Group set up with matching taxes (Item Tax Templates) valid from 1st Aug 2018
	NO Sales Taxes and Charges Template
	NO Rate set on Sales VAT Account(s)
	Matching Coupon Codes (transaction-based only)
	Currency exchange rates
	Accounts Settings -> Automatically Add Taxes and Charges from Item Tax Template
	Accounts Settings -> Enable Discount Accounting
	"""

	@classmethod
	def send_order(cls, text):
		"Mimic woocommerce order hook submission"
		import requests, base64, hmac, hashlib
		woocommerce_settings = frappe.get_doc("Woocommerce Settings")
		sig = base64.b64encode(
			hmac.new(
				woocommerce_settings.secret.encode('utf8'),
				text.encode('utf8'),
				hashlib.sha256
			).digest()
		)
		site = frappe.utils.get_site_url(frappe.local.site)
		url = site + '/api/method/slife.slife.woocommerce.order'
		headers = {
			'x-wc-webhook-event': 'created',
			'x-wc-webhook-signature': sig
		}
		r = requests.post(url, headers=headers, data=text)
		r.raise_for_status()

	@classmethod
	def get_order(cls, filename):
		from pathlib import Path
		p = Path(__file__).with_name(filename)
		with p.open('r') as f:
			order = f.read()
		return order

	def validate_order(self, text):
		import json
		from frappe.utils import flt
		order = json.loads(text)
		billing = order.get('billing')

		# Test Contact exists
		contact = frappe.get_cached_doc('Contact', f'{billing.get("first_name")} {billing.get("last_name")}')
		self.assertTrue(bool(contact))
		self.assertTrue(bool(contact.email_ids))
		self.assertTrue(bool(contact.phone_nos))
		# Test Customer & Address exist
		customer = frappe.get_cached_doc('Customer', billing.get("company") or contact.name)
		self.assertTrue(bool(customer))
		self.assertEqual(customer.customer_primary_contact, contact.name)
		self.assertTrue(bool(customer.customer_primary_address))
		# Test Sales Order
		order_code = order.get("order_key").rpartition("_")[2]
		so = frappe.get_cached_doc('Sales Order', f'SO-{order_code}')
		self.assertEqual(so.customer, customer.name)
		self.assertEqual(so.po_no, order_code)
		self.assertEqual(so.customer_address, customer.customer_primary_address)
		self.assertEqual(so.contact_person, customer.customer_primary_contact)
		self.assertEqual(so.currency, order.get("currency"))
		total_qty = sum(item.get('quantity') for item in order.get("line_items"))
		self.assertEqual(flt(so.total_qty), flt(total_qty))
		self.assertTrue(bool(so.tax_category))
		for item in so.items:
			self.assertTrue(bool(item.item_tax_template))
			self.assertTrue(bool(item.warehouse))
		self.assertEqual(flt(so.discount_amount), flt(order.get("discount_total")))
		# Shipping charge & tax is included in WC total
		self.assertEqual(flt(so.grand_total), flt(order.get("total")))
		self.assertEqual(flt(so.total_taxes_and_charges), flt(order.get("total_tax")) + flt(order.get("shipping_total")))
		self.assertTrue(bool(so.woocommerce_order_json))
		# Test Sales Invoice
		# Test RFQ

	def run_test_from_file(self, filename):
		order = self.get_order(filename)
		self.send_order(order)
		# Required to get latest db values: ???
		frappe.db.close()
		self.validate_order(order)

	def test_order_1(self):
		"Order with 100% discount coupon and single variant"
		self.run_test_from_file('test_order_1.json')

	def test_order_2(self):
		"Order with 10% discount and two variants"
		self.run_test_from_file('test_order_2.json')

	def test_order_3(self):
		"Change Billing address with shipping"
		self.run_test_from_file('test_order_3.json')

	def test_order_4(self):
		"Actual shipping example with 10% discount"
		self.run_test_from_file('test_order_4.json')

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
	(for testing, create skus: 11111111 & 22222222, not 33333333)
	Item Group set up with matching taxes (Item Tax Templates) valid from 1st Aug 2018 & Cost Center
	NO Sales Taxes and Charges Template
	NO Rate set on Sales VAT Account(s)
	Matching Coupon Codes + Net Total Pricing Rule (transaction-based only)
	Currency exchange rates
	Accounts Settings -> Automatically Add Taxes and Charges from Item Tax Template
	Accounts Settings -> Enable Discount Accounting
	Lead Source
	Payment Terms Template - uses Woocommerce 'payment_method' field as name or Company default
	Company default Cost Center for Sales Invoices
	"""

	def tearDown(self):
		"Ensure the db connection is closed"
		frappe.db.close()

	@classmethod
	def send_order(cls, text):
		"Mimic woocommerce order hook submission"
		import requests, base64, hmac, hashlib, json
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
		try:
			r.raise_for_status()
		except requests.HTTPError:
			if 'json' in r.headers['Content-Type']:
				j = r.json()
				if j.get('exception'):
					print('\n{exception}'.format(**j))
					print(''.join(str(line) for line in json.loads(j['exc'])))
			else:
				print(r.text)
			raise
		# Seems to use the wrong data for validation if not closed
		frappe.db.close()

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
		so = frappe.get_cached_doc('Sales Order', {'po_no': ('=', f'{order_code}')})
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

		if order.get('status') != 'pending':
			# Test Sales Invoice
			si = frappe.get_cached_doc('Sales Invoice', {'po_no': ('=', f'{order_code}')})
			self.assertTrue(bool(si))
			# Test RFQ
			rfq = frappe.get_cached_doc('Request for Quotation', {'rfq_number': ('=', f'{order_code}')})
			self.assertTrue(bool(rfq))

	def run_test_from_file(self, filename):
		order = self.get_order(filename)
		self.send_order(order)
		self.validate_order(order)

	def test_order_1(self):
		"Failed order with 100% discount coupon and single variant"
		self.run_test_from_file('test_order_1.json')

	def test_order_2(self):
		"Pending order with 10% discount and two variants"
		self.run_test_from_file('test_order_2.json')

	def test_order_3(self):
		"On-hold order, change Billing address, with shipping"
		self.run_test_from_file('test_order_3.json')

	def test_order_4(self):
		"Processing order, actual shipping example with 10% discount"
		self.run_test_from_file('test_order_4.json')

	def test_order_5(self):
		"Pending order, no template fail"
		import requests
		order = self.get_order('test_order_5.json')
		with self.assertRaises(requests.HTTPError) as obj:
			self.send_order(order)
		self.assertEqual(obj.exception.response.status_code, 404)
		self.assertEqual(obj.exception.response.reason, 'NOT FOUND')

	def test_order_6(self):
		"Pending order, no coupon"
		self.run_test_from_file('test_order_6.json')

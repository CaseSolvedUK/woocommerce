# Copyright (c) 2021, Slife
# For license information, please see license.txt

# Run tests with: bench --site <site> --verbose run-tests --module slife.slife.test_woocommerce

import frappe
import unittest

class TestWoocommerce(unittest.TestCase):
	"""
	Submit Woocommerce test orders
	Requires:
	Frappe & ERPNext after v13.11.0 for SO coupon discount support
	Matching Coupon Codes + Net Total Pricing Rule (transaction-based only)
	 - ERPNext only supports one coupon code, the "code" WC field must match the ERPNext "name" field
	Woocommerce settings configured
	 - mandatory fields
	 - secret
	 - attribute_key_prefix = _uni_item_
	 - Customer Tax Category to match Item Group taxes (below)
	Fiscal Years (for customer)
	Item templates with item_code = sku for woocommerce items with attributes/meta_data to make on-the-fly variants
	 - for testing, create skus: 11111111 & 22222222, not 33333333
	 - matching Item Attributes
	Item Group set up with matching taxes (Item Tax Templates) valid from 1st Aug 2018 & Cost Center
	 - Item Group Default Selling Cost Center will be used for the Item, but the Company Default Cost Center will be used as a fallback
	NO Sales Taxes and Charges Template
	NO Rate set on Sales VAT Account(s)
	Currency exchange rates
	 - Enabled currency and rate set
	Accounts Settings -> Automatically Add Taxes and Charges from Item Tax Template
	Accounts Settings -> Enable Discount Accounting
	Lead Source
	Payment Terms Template - uses Woocommerce 'payment_method' field as name or Company default
	Company Default Cost Center
	"""

	@classmethod
	def tearDownClass(cls):
		"Remove data - only once"
		from erpnext.setup.doctype.company.company import create_transaction_deletion_request
		company = frappe.db.get_single_value('Woocommerce Settings', 'company')
		# Also deletes Pricing Rules: https://github.com/frappe/erpnext/issues/28823
		#create_transaction_deletion_request(company)

	def tearDown(self):
		"Ensure the db connection is closed after each test"
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
			'X-Frappe-CSRF-Token': 'None',
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
		import json
		from pathlib import Path
		p = Path(__file__).with_name(filename)
		with p.open('r') as f:
			text = f.read()
		order = json.loads(text)
		h = frappe.generate_hash(length=13)
		order['order_key'] = f'wc_order_{h}'
		return json.dumps(order)

	def validate_order(self, text):
		import json
		from frappe.utils import flt
		order = json.loads(text)
		billing = order.get('billing')

		# Test Contact exists
		contact = frappe.get_doc('Contact', f'{billing.get("first_name")} {billing.get("last_name")}')
		self.assertTrue(bool(contact))
		self.assertTrue(bool(contact.email_ids))
		self.assertTrue(bool(contact.phone_nos))
		# Test Customer & Address exist
		customer = frappe.get_doc('Customer', billing.get("company") or contact.name)
		self.assertTrue(bool(customer))
		self.assertEqual(customer.customer_primary_contact, contact.name)
		self.assertTrue(bool(customer.customer_primary_address))
		# Test Sales Order
		order_code = order.get("order_key").rpartition("_")[2]
		so = frappe.get_doc('Sales Order', {'po_no': ('=', f'{order_code}')})
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
		return so

	def run_test_from_file(self, filename):
		order = self.get_order(filename)
		self.send_order(order)
		return (order, self.validate_order(order))

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
		text, so = self.run_test_from_file('test_order_4.json')
		line2 = so.items[1]
		self.assertIn('12324304', line2.item_code)
		self.assertIn('77', line2.item_code)
		self.assertNotIn('24301', line2.item_code)

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

# Copyright (c) 2021, Slife
# For license information, please see license.txt

import frappe
from frappe import _

# Compatible with WooCommerce 5.9.0 & 6.2.0
# TODO: remove Woocommerce Supplier in preference to using the ERPNext item/item group configured default Supplier
# TODO: add fee_lines to the Sales Order. See test_order_7.json
# TODO: make generic endpoint to deal with new events with header x-wc-webhook-resource: order.status_changed & order.updated & coupon.created
# TODO: speed up frontend by queueing orders in a background_job/scheduler queue?

def settings_override(doc, method=None):
	"Overwrite the default settings endpoint URL. Called from the Woocommerce Settings before_save event"
	doc.endpoint = f'https://{frappe.local.site}/api/method/slife.slife.woocommerce.order'

@frappe.whitelist(allow_guest=True)
def order(*args, **kwargs):
	try:
		_order(*args, **kwargs)
	except Exception:
		error_message = f"{frappe.get_traceback()}\n\n Request Data: \n{frappe.request.data.decode('utf8')}"
		frappe.log_error(error_message, "WooCommerce Error")
		raise

woocommerce_settings = None
def _order(*args, **kwargs):
	global woocommerce_settings
	import json
	from erpnext.erpnext_integrations.connectors.woocommerce_connection import verify_request

	if frappe.request and frappe.request.data:
		verify_request()
		try:
			order = json.loads(frappe.request.data)
		except ValueError:
			#woocommerce returns 'webhook_id=value' for the first request which is not JSON
			order = frappe.request.data
		event = frappe.get_request_header("x-wc-webhook-event")

	else:
		# ignore empty requests
		return

	woocommerce_settings = frappe.get_cached_doc("Woocommerce Settings")
	if event == "created":
		status = order.get('status')
		if status in ('processing', 'pending', 'failed', 'on-hold'):
			customer = get_customer_by_email(order)
			items = get_items(order)
			sales_order = create_sales_order(order, customer, items)

			if status != 'pending':
				sales_order.submit()
				sales_invoice = create_sales_invoice(order, sales_order)
				if woocommerce_settings.orders_outsourced:
					rfq = create_rfq(order, sales_order)
				# Will not allow creation of sales invoice or material request if sales order is On Hold or Closed
				update_sales_order_status(status, sales_order)
		# Do nothing on cancelled, completed & refunded

def create_rfq(order, sales_order):
	"Create a draft RFQ from a Material Request"
	from erpnext.selling.doctype.sales_order.sales_order import make_material_request
	from erpnext.stock.doctype.material_request.material_request import make_request_for_quotation

	mat_req = make_material_request(sales_order.name)
	quote_after = woocommerce_settings.quote_after or 7
	mat_req.schedule_date = frappe.utils.add_days(mat_req.transaction_date, quote_after)
	mat_req.insert()
	mat_req.submit()

	rfq = make_request_for_quotation(mat_req.name)

	rfq_supplier = frappe.new_doc('Request for Quotation Supplier', rfq, 'suppliers')
	rfq_supplier.supplier = woocommerce_settings.supplier
	rfq.append('suppliers', rfq_supplier)

	rfq.rfq_number = sales_order.po_no
	rfq.email_template = woocommerce_settings.rfq_email_template
	rfq.insert()
	return rfq

def create_sales_invoice(order, sales_order):
	"Create the Sales Invoice. processing: submitted. (pending, failed, on-hold): draft"
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	sales_invoice = make_sales_invoice(sales_order.name)
	sales_invoice.insert()
	if order.get('status') == 'processing':
		sales_invoice.submit()
	return sales_invoice

def create_sales_order(order, customer, items):
	"Create a new sales order"
	from erpnext.setup.utils import get_exchange_rate
	company_currency = frappe.get_cached_value('Company', woocommerce_settings.company, "default_currency")

	sales_order = frappe.new_doc("Sales Order")
	sales_order.customer = customer.name
	sales_order.naming_series = woocommerce_settings.sales_order_series or "SO-WOO-.#####"

	created_date = order.get("date_created").split("T")
	sales_order.transaction_date = created_date[0]
	sales_order.po_date = created_date[0]
	sales_order.po_no = order.get("order_key").rpartition("_")[2]
	delivery_after = woocommerce_settings.delivery_after_days or 7
	sales_order.delivery_date = frappe.utils.add_days(created_date[0], delivery_after)

	sales_order.company = woocommerce_settings.company
	sales_order.currency = order.get("currency")
	sales_order.conversion_rate = get_exchange_rate(order.get("currency"), company_currency)
	sales_order.coupon_code = order.get("coupon_lines")[0].get("code") if order.get("coupon_lines") else None
	sales_order.woocommerce_order_json = frappe.request.data.decode('utf8')

	sales_order.source = woocommerce_settings.lead_source
	sales_order.payment_terms_template = order.get("payment_method") or frappe.db.get_value('Company', woocommerce_settings.company, 'payment_terms')

	# !important
	sales_order.set_missing_values()
	add_sales_order_items(order, sales_order, items)

	#print(sales_order.as_dict())
	#sales_order.validate()
	sales_order.insert()
	frappe.db.commit()
	return sales_order

def update_sales_order_status(status, sales_order):
	"""
	Mimic WC statuses:
	pending - draft
	processing - submitted
	on-hold - submitted & On Hold
	failed - submitted & Closed
	"""
	from frappe.desk.form.utils import add_comment

	if 'hold' in status:
		doc_status = 'On Hold'
	elif 'fail' in status:
		doc_status = 'Closed'
	else:
		return

	add_comment(
		reference_doctype=sales_order.doctype,
		reference_name=sales_order.name,
		content=_(f'Reason for state {doc_status}: Woocommerce Order Status'),
		comment_email=frappe.session.user,
		comment_by=frappe.session.user_fullname
	)
	sales_order.update_status(doc_status)

def add_sales_order_items(order, sales_order, items):
	from frappe.utils import flt
	from erpnext.controllers.accounts_controller import add_taxes_from_tax_template, set_child_tax_template_and_map
	from erpnext.accounts.doctype.pricing_rule.pricing_rule import apply_pricing_rule

	for item_data in order.get("line_items"):
		item = None
		for match in items:
			if item_data.get('erpnext_item_code') == match.item_code:
				item = match
				break

		qty = flt(item_data.get("quantity"))
		subtotal = flt(item_data.get("subtotal"))

		so_item = frappe.new_doc('Sales Order Item', sales_order, 'items')
		so_item.update({
			"item_code": item.name,
			"delivery_date": sales_order.delivery_date,
			"qty": qty,
			"price_list_rate": subtotal / qty
		})

		sales_order.append('items', so_item)
		# !important
		sales_order.set_missing_item_details()
		set_child_tax_template_and_map(item, so_item, sales_order)
		add_taxes_from_tax_template(so_item, sales_order)
		# !important

	add_tax_details(sales_order, order.get("shipping_total"), "Shipping Charge", woocommerce_settings.f_n_f_account)
	add_tax_details(sales_order, order.get("shipping_tax"), "Shipping Tax", woocommerce_settings.tax_account)

	# Hack fix of ERPNext bug #29871
	cost_center = frappe.get_value('Company', sales_order.company, 'cost_center')
	for tax in sales_order.taxes:
		tax.cost_center = cost_center
	#print(sales_order.as_dict())

def add_tax_details(sales_order, price, desc, tax_account_head):
	sales_order.append("taxes", {
		"charge_type":"Actual",
		"account_head": tax_account_head,
		"tax_amount": price,
		"description": desc
	})

def attribute_value(key, org):
	human, sep, value = org.rpartition('_')
	if not sep:
		human = value

	try:
		value = int(value)
		return value, f'{key}:{human}'
	except ValueError:
		pass

	try:
		value = float(value)
	except ValueError:
		pass

	# string
	return value, f'{key}:{human}'

def get_items(order):
	"Get or create order items. Variants have attributes, normal items do not"
	from erpnext.controllers.item_variant import copy_attributes_to_variant
	default_wh = frappe.get_value('Warehouse', {'company': woocommerce_settings.company, 'name': ('like', 'Stores%')}, 'name')
	meta_prefix = woocommerce_settings.attribute_key_prefix
	items = []
	for item in order.get('line_items'):
		doc = frappe.new_doc('Item')
		code = item.get('sku')

		attributes = {}
		template = None
		for meta in item.get('meta_data'):
			if meta['key'].startswith(meta_prefix):
				if not template:
					template = frappe.get_doc('Item', {'name': code, 'has_variants': True})
					template_attributes = [attr.attribute for attr in template.attributes]

				key = meta['key'][len(meta_prefix):]
				# Skip attribute if not in template
				if key not in template_attributes:
					continue

				value, disp = attribute_value(key, meta['value'])

				# Save for later addition to code and name in consistent sorted order
				attributes[key] = (value, disp, meta['value'])

				attribute_doc = frappe.new_doc('Item Variant Attribute')
				attribute_doc.variant_of = code
				attribute_doc.attribute = key
				attribute_doc.attribute_value = value
				doc.append('attributes', attribute_doc)

		# Test if the item is a variant
		if template:
			copy_attributes_to_variant(template, doc)
			name = template.get('item_name')
			for key in sorted(attributes):
				code += f'-{attributes[key][0]}'
				name += f' {attributes[key][1]}'
		else:
			doc.item_group = woocommerce_settings.item_group
			doc.stock_uom = woocommerce_settings.uom or "Nos"
			doc.sales_uom = doc.stock_uom
			doc.is_stock_item = False
			doc.append("item_defaults", {
				"company": woocommerce_settings.company,
				"default_warehouse": woocommerce_settings.warehouse or default_wh
			})
			name = item.get('name')
			description = f'<p>{name}</p>'
			doc.description = f'<div>{description}</div>'

		doc.item_code = code
		doc.item_name = name

		try:
			doc.insert()
		except frappe.DuplicateEntryError:
			pass
		items += [doc]
		# used in add_sales_order_items:
		item['erpnext_item_code'] = code
	frappe.db.commit()
	return items

def get_customer_by_email(order):
	"Get or create customer doc with addresses and contact by email"
	contact = get_contact_by_email(order)

	customer = {
		'customer_type': 'Company' if order.get("billing").get("company").strip() else 'Individual',
		'customer_name': (order.get("billing").get("company").strip() or
			f'{order.get("billing").get("first_name")} {order.get("billing").get("last_name")}'.strip()),
		'tax_category': woocommerce_settings.customer_tax_category,
		'customer_primary_contact': contact.name
	}

	customer_names = frappe.db.get_all('Dynamic Link',
		filters={'parenttype': 'Contact', 'parent': contact.name, 'link_doctype': 'Customer'},
		pluck='link_name'
	)
	if customer_names:
		doc = frappe.get_doc('Customer', customer_names[0])
		doc.update(customer)
	else:
		doc = frappe.new_doc('Customer')
		doc.update(customer)
		doc.insert()
		contact.append("links", {
			"link_doctype": "Customer",
			"link_name": doc.name
		})
		# Already inserted, just updating the links:
		contact.save()

	add_addresses(order, doc)
	doc.save()
	frappe.db.commit()
	return doc

def add_addresses(order, doc_customer):
	"Append new addresses to the customer doc"
	default_country = frappe.db.get_single_value('System Settings', 'country')

	address_names = frappe.db.get_all('Dynamic Link',
		filters={'parenttype': 'Address', 'link_doctype': 'Customer', 'link_name': doc_customer.name},
		pluck='parent', order_by='parent asc'
	)

	for address_type in ['Billing', 'Shipping']:
		address = order.get(address_type.lower())
		if address.get('address_1').strip():
			data = {
				'country': frappe.get_value("Country", {"code": address.get("country").lower()}) or default_country,
				'pincode': address.get('postcode'),
				'state': address.get('state'),
				'city': address.get('city'),
				'address_line2': address.get('address_2'),
				'address_line1': address.get('address_1'),
				'address_type': address_type,
				'address_title': doc_customer.customer_name,
				'is_primary_address': 1 if address_type == 'Billing' else 0,
				'is_shipping_address': 1 if address_type == 'Shipping' or order.get('shipping').get('address_1').strip() == '' else 0,
				'links': [{
					'link_doctype': 'Customer',
					'link_name': doc_customer.name
				}]
			}

			doc = frappe.new_doc('Address')
			doc.update(data)
			for name in address_names:
				existing = frappe.get_doc('Address', name)
				if same_address(doc, existing):
					existing.update(data)
					doc = existing
					doc.save()
					break
			else:
				doc.insert()

			doc_customer.reload()
			if address_type == 'Billing':
				doc_customer.customer_primary_address = doc.name
	frappe.db.commit()

def same_address(new, existing):
	"See if there's a first line + postcode + country address match with the existing address"
	from difflib import SequenceMatcher as SM
	threshold = 0.8

	if new.address_line1.strip() and new.pincode.strip() and new.country.strip():
		r_1st = SM(None, new.address_line1.lower().strip(), existing.address_line1.lower().strip()).ratio()
		r_pc = 1.0 if new.pincode.lower().replace(' ', '') == existing.pincode.lower().replace(' ', '') else 0.0
		r_co = 1.0 if new.country.lower().replace(' ', '') == existing.country.lower().replace(' ', '') else 0.0
		score = r_1st * r_pc * r_co
		#print(f'{new.address_line1.strip()} : {existing.address_line1.strip()} = {score}')
		return score >= threshold
	# Keep the existing address if something is missing:
	return True

def get_contact_by_email(order):
	"Look for an email match on Contact Email and get the parent Contact or create new"
	email = {
		'email_id': order.get("billing").get("email").strip(),
		'is_primary': True
	}
	phone = {
		'phone': order.get("billing").get("phone").strip(),
		'is_primary_phone': True
	}
	contact = {
		'first_name': order.get("billing").get("first_name").strip(),
		'last_name': order.get("billing").get("last_name").strip(),
		'is_primary_contact': True,
		'is_billing_contact': True
	}
	contact_names = frappe.db.get_all('Contact Email',
		filters={'email_id': email['email_id'], 'parenttype': 'Contact'},
		pluck='parent'
	)
	if contact_names:
		doc = frappe.get_doc('Contact', contact_names[0])
		doc.update(contact)
		doc.save()
	else:
		doc = frappe.new_doc('Contact')
		doc.update(contact)
		doc_email = frappe.new_doc('Contact Email')
		doc_email.update(email)
		doc_phone = frappe.new_doc('Contact Phone')
		doc_phone.update(phone)
		doc.append('email_ids', doc_email)
		doc.append('phone_nos', doc_phone)
		doc.insert()
	return doc

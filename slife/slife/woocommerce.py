# Copyright (c) 2021, Slife
# For license information, please see license.txt

import frappe
import json
from frappe import _
from frappe.utils import flt
from erpnext.controllers.item_variant import copy_attributes_to_variant
from erpnext.erpnext_integrations.connectors.woocommerce_connection import verify_request, add_tax_details


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

	woocommerce_settings = frappe.get_doc("Woocommerce Settings")
	if event == "created":
		# Sales Invoice and draft Request for Quotation (at least 1 default Supplier required)
		customer = get_customer_by_email(order)
		items = get_items(order)
		sales_order = create_sales_order(order, customer, items)
		#sales_invoice = create_sales_invoice(order)
		#rfq = create_rfq(order)

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
	sales_order.coupon_code = order.get("coupon_lines")[0].get("code")
	sales_order.woocommerce_order_json = frappe.request.data.decode('utf8')

	total = add_sales_order_items(order, sales_order, items)
	sales_order.total = total

	# Insert before adding the shipping charges so ERPNext brings through the correct Item Tax Template and applies it
	sales_order.insert()

	# Fix:
	add_tax_details(sales_order, order.get("shipping_tax"), "Shipping Tax", woocommerce_settings.f_n_f_account)
	add_tax_details(sales_order, order.get("shipping_total"), "Shipping Total", woocommerce_settings.f_n_f_account)

	#print(sales_order.as_dict())
	#sales_order.validate()
	sales_order.save()
	#sales_order.submit()

	frappe.db.commit()
	return sales_order

def new_add_sales_order_items(order):
	from erpnext.controllers.accounts_controller import set_order_defaults
	set_order_defaults(parent_doctype, parent_doctype_name, child_doctype, child_docname, item_row)
	

def add_sales_order_items(order, sales_order, items):
	from erpnext.stock.get_item_details import get_conversion_factor, get_item_warehouse

	total = 0.0
	for item_data in order.get("line_items"):
		for match in items:
			if item_data.get('product_id') == match.product_id:
				item = match

		qty = flt(item_data.get("quantity"))
		subtotal = flt(item_data.get("subtotal"))
		rate = subtotal / qty

		sales_order.append("items", {
			"item_code": item.name,
			"item_name": item.item_name,
			"description": item.description,
			"delivery_date": sales_order.delivery_date,

			"uom": item.sales_uom,
			"stock_uom": item.stock_uom,
			"conversion_factor": get_conversion_factor(item.name, item.sales_uom)['conversion_factor'],
			"warehouse": get_item_warehouse(item, woocommerce_settings, overwrite_warehouse=True),

			"base_rate": rate * sales_order.conversion_rate,
			"qty": qty,
			"rate": rate
		})
		total += flt(item_data.get("subtotal"))
		# Apply the same tax as WC through the Item Group in ERPNext
		#add_tax_details(sales_order, item_data.get("subtotal_tax"), "Ordered Item tax", woocommerce_settings.tax_account)
	return total


def get_items(order):
	"Get or create order items. Variants have attributes, normal items do not"
	language = frappe.get_single("System Settings").language or 'en'
	company_abbr = frappe.db.get_value('Company', woocommerce_settings.company, 'abbr')
	meta_prefix = woocommerce_settings.attribute_key_prefix
	items = []
	for item in order.get('line_items'):
		doc = frappe.new_doc('Item')

		attributes = {}
		for meta in item.get('meta_data'):
			if meta['key'].startswith(meta_prefix):
				key = meta['key'][len(meta_prefix):]
				try:
					# numeric
					value = int(meta['value'])
					disp = f'{key}:{value}'
				except ValueError:
					# sku
					disp, _, sku = meta['value'].rpartition('_')
					value = int(sku)
				attributes[key] = (value, disp, meta['value'])

				attribute_doc = frappe.new_doc('Item Variant Attribute')
				attribute_doc.variant_of = item.get('sku')
				attribute_doc.attribute = key
				attribute_doc.attribute_value = value
				doc.append('attributes', attribute_doc)

		# Test if variant
		if attributes:
			doc.variant_of = item.get('sku')
			template = frappe.get_doc('Item', doc.variant_of)
			copy_attributes_to_variant(template, doc)
		else:
			doc.item_group = woocommerce_settings.item_group
			doc.stock_uom = woocommerce_settings.uom or _("Nos", language)
			doc.sales_uom = doc.stock_uom
			doc.is_stock_item = False
			doc.append("item_defaults", {
				"company": woocommerce_settings.company,
				"default_warehouse": woocommerce_settings.warehouse or _(f"Stores - {company_abbr}", language)
			})

		code = item.get('sku')
		name = item.get('name')
		description = f'{name}\n'
		for key in sorted(attributes):
			code += f'-{attributes[key][0]}'
			name += f' {attributes[key][1]}'
			description += f'{key} = {attributes[key]}<br>'
		doc.item_code = code
		doc.item_name = name
		doc.description = description
		# Not in db but used later on Sales Order creation:
		doc.product_id = item.get('product_id')

		try:
			doc.insert()
		except frappe.DuplicateEntryError:
			pass
		items += [doc]
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
		pluck='parent'
	)

	for address_type in ['Billing', 'Shipping']:
		address = order.get(address_type.lower())
		if address.get('address_1').strip():
			doc = frappe.new_doc('Address')
			doc.country = frappe.get_value("Country", {"code": address.get("country").lower()}) or default_country
			doc.pincode = address.get('postcode')
			doc.state = address.get('state')
			doc.city = address.get('city')
			doc.address_line2 = address.get('address_2')
			doc.address_line1 = address.get('address_1')
			doc.address_type = address_type
			doc.address_title = doc_customer.customer_name
			doc.append("links", {
				"link_doctype": "Customer",
				"link_name": doc_customer.name
			})
			if address_type == 'Billing':
				doc.is_primary_address = True
			else:
				doc.is_shipping_address = True

			existing = None
			for name in address_names:
				if name.endswith(address_type):
					existing = frappe.get_doc('Address', name)
					break

			if existing:
				if same_address(doc, existing):
					existing.update(doc.as_dict())
					doc = existing
					doc.save()
				else:
					renaming = True
					i = 1
					while(renaming):
						try:
							frappe.rename_doc("Address", existing.name, f'{doc_customer.customer_name} {i}')
						except frappe.ValidationError:
							i += 1
							continue
						renaming = False
					doc.insert()
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
		return r_1st * r_pc * r_co >= threshold
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

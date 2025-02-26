from typing import Any, Dict, List, Optional, Tuple

import frappe
import requests
from frappe import _
from frappe.utils import get_datetime
from pytz import timezone

from ecommerce_integrations.unicommerce.constants import SETTINGS_DOCTYPE
from ecommerce_integrations.unicommerce.utils import create_unicommerce_log

JsonDict = Dict[str, Any]


class UnicommerceAPIClient:
	"""Wrapper around Unicommerce REST API

	API docs: https://documentation.unicommerce.com/
	"""

	def __init__(
		self, url: Optional[str] = None, access_token: Optional[str] = None,
	):
		self.settings = frappe.get_doc(SETTINGS_DOCTYPE)
		self.base_url = url or f"https://{self.settings.unicommerce_site}"
		self.access_token = access_token
		self.__initialize_auth()

	def __initialize_auth(self):
		"""Initialize and setup authentication details"""
		if not self.access_token:
			self.settings.renew_tokens()
			self.access_token = self.settings.get_password("access_token")

		self._auth_headers = {"Authorization": f"Bearer {self.access_token}"}

	def request(
		self, endpoint: str, method: str = "POST", headers: JsonDict = None, body: JsonDict = None,
	) -> Tuple[JsonDict, bool]:
		if headers is None:
			headers = {}

		headers.update(self._auth_headers)

		url = self.base_url + endpoint

		try:
			response = requests.request(url=url, method=method, headers=headers, json=body)
			response.raise_for_status()
		except Exception:
			create_unicommerce_log(status="Error", make_new=True)

		data = frappe._dict(response.json())
		status = data.successful if data.successful is not None else True

		if not status:
			req = response.request
			url = f"URL: {req.url}"
			body = f"body:  {req.body.decode('utf-8')}"
			request_data = "\n\n".join([url, body])
			message = ", ".join(error["message"] for error in data.errors)
			create_unicommerce_log(
				status="Error", response_data=data, request_data=request_data, message=message, make_new=True
			)

		return data, status

	def get_unicommerce_item(self, sku: str) -> Optional[JsonDict]:
		"""Get Unicommerce item data for specified SKU code.

		ref: https://documentation.unicommerce.com/docs/itemtype-get.html
		"""
		item, status = self.request(
			endpoint="/services/rest/v1/catalog/itemType/get", body={"skuCode": sku}
		)
		if status:
			return item

	def create_update_item(self, item_dict: JsonDict) -> Tuple[JsonDict, bool]:
		"""Create/update item on unicommerce.

		ref: https://documentation.unicommerce.com/docs/createoredit-itemtype.html
		"""
		return self.request(
			endpoint="/services/rest/v1/catalog/itemType/createOrEdit", body={"itemType": item_dict}
		)

	def get_sales_order(self, order_code: str) -> Optional[JsonDict]:
		"""Get details for a sales order.

		ref: https://documentation.unicommerce.com/docs/saleorder-get.html
		"""

		order, status = self.request(
			endpoint="/services/rest/v1/oms/saleorder/get", body={"code": order_code}
		)
		if status and "saleOrderDTO" in order:
			return order["saleOrderDTO"]

	def search_sales_order(
		self,
		from_date: Optional[str] = None,
		to_date: Optional[str] = None,
		status: Optional[str] = None,
		channel: Optional[str] = None,
		facility_codes: Optional[List[str]] = None,
		updated_since: Optional[int] = None,
	) -> Optional[List[JsonDict]]:
		"""Search sales order using specified parameters and return search results.

		ref: https://documentation.unicommerce.com/docs/saleorder-search.html
		"""
		body = {
			"status": status,
			"channel": channel,
			"facility_codes": facility_codes,
			"fromDate": _utc_timeformat(from_date) if from_date else None,
			"toDate": _utc_timeformat(to_date) if to_date else None,
			"updatedSinceInMinutes": updated_since,
		}

		# remove None values.
		body = {k: v for k, v in body.items() if v is not None}

		search_results, status = self.request(
			endpoint="/services/rest/v1/oms/saleOrder/search", body=body
		)

		if status and "elements" in search_results:
			return search_results["elements"]

	def get_inventory_snapshot(
		self, sku_codes: List[str], facility_code: str, updated_since: int = 1430
	) -> Optional[JsonDict]:
		"""Get current inventory snapshot.

		ref: https://documentation.unicommerce.com/docs/inventory-snapshot.html
		"""

		extra_headers = {"Facility": facility_code}

		body = {"itemTypeSKUs": sku_codes, "updatedSinceInMinutes": updated_since}

		response, status = self.request(
			endpoint="/services/rest/v1/inventory/inventorySnapshot/get", headers=extra_headers, body=body,
		)

		if status:
			return response

	def bulk_inventory_update(self, facility_code: str, inventory_map: Dict[str, int]):
		"""Bulk update inventory on unicommerce using SKU and qty.

		The qty should be "total" quantity.
		ref: https://documentation.unicommerce.com/docs/adjust-inventory-bulk.html
		"""

		extra_headers = {"Facility": facility_code}

		inventory_adjustments = []
		for sku, qty in inventory_map.items():
			inventory_adjustments.append(
				{
					"itemSKU": sku,
					"quantity": qty,
					"shelfCode": "DEFAULT",  # XXX
					"inventoryType": "GOOD_INVENTORY",
					"adjustmentType": "REPLACE",
					"facilityCode": facility_code,
				}
			)

		response, status = self.request(
			endpoint="/services/rest/v1/inventory/adjust/bulk",
			headers=extra_headers,
			body={"inventoryAdjustments": inventory_adjustments},
		)

		if not status:
			return response, status
		else:
			# parse result by item
			try:
				item_wise_response = response["inventoryAdjustmentResponses"]
				item_wise_status = {
					item["facilityInventoryAdjustment"]["itemSKU"]: item["successful"]
					for item in item_wise_response
				}
				if False in item_wise_status.values():
					create_unicommerce_log(
						status="Failure",
						response_data=response,
						message="Inventory sync failed for some items",
						make_new=True,
					)
				return item_wise_status, status
			except Exception:
				return response, False

	def create_sales_invoice(
		self, so_code: str, so_item_codes: List[str], facility_code: str
	) -> Optional[JsonDict]:

		body = {"saleOrderCode": so_code, "saleOrderItemCodes": so_item_codes}
		extra_headers = {"Facility": facility_code}

		response, status = self.request(
			endpoint="/services/rest/v1/invoice/createInvoiceBySaleOrderCode",
			body=body,
			headers=extra_headers,
		)
		return response

	def create_invoice_by_shipping_code(self, shipping_package_code: str, facility_code: str):
		body = {"shippingPackageCode": shipping_package_code}
		response, status = self.request(
			endpoint="/services/rest/v1/oms/shippingPackage/createInvoice",
			body=body,
			headers={"Facility": facility_code},
		)

		return response

	def get_sales_invoice(
		self, shipping_package_code: str, facility_code: str, is_return: bool = False
	) -> Optional[JsonDict]:
		extra_headers = {"Facility": facility_code}
		response, status = self.request(
			endpoint="/services/rest/v1/invoice/details/get",
			body={"shippingPackageCode": shipping_package_code, "return": is_return},
			headers=extra_headers,
		)

		if status:
			return response


def _utc_timeformat(datetime) -> str:
	""" Get datetime in UTC/GMT as required by Unicommerce"""
	return get_datetime(datetime).astimezone(timezone("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

import requests
from typing import Dict, Any, List, Optional

from auth.concur_oauth import ConcurOAuthClient


class CardsService:
    """Thin wrapper for SAP Concur Cards v4.

    Common endpoint pattern:
        GET {base}/cards/v4/users/{userId}/transactions

    Pagination is tenant-dependent. This wrapper is defensive:
    - Stops when fewer than page_size results are returned, OR
    - Stops if the first transaction repeats (paging ignored) to prevent infinite loops.
    """

    def __init__(self, api_base_url: str, oauth: ConcurOAuthClient):
        self.api_base_url = str(api_base_url).strip().rstrip("/")
        self.oauth = oauth
        if not self.api_base_url:
            raise ValueError("CardsService requires api_base_url")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.oauth.get_access_token()}",
            "Accept": "application/json",
        }

    def get_transactions_for_user(
        self,
        concur_user_id: str,
        transaction_date_from: str,
        transaction_date_to: str,
        status: Optional[str] = None,
        page_size: int = 200,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/cards/v4/users/{concur_user_id}/transactions"

        if page_size < 1:
            page_size = 200
        if page_size > 500:
            page_size = 500

        page = 1
        seen_first_id: Optional[str] = None
        all_items: List[Dict[str, Any]] = []

        while True:
            params: Dict[str, Any] = {
                "transactionDateFrom": transaction_date_from,
                "transactionDateTo": transaction_date_to,
                "page": page,
                "pageSize": page_size,
            }
            if status:
                params["status"] = status

            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json() or {}

            items = (
                data.get("Items")
                or data.get("items")
                or data.get("Transactions")
                or data.get("transactions")
                or []
            )
            if not isinstance(items, list):
                raise RuntimeError("Unexpected Cards response shape: transactions is not a list")

            if not items:
                break

            first_id = str(items[0].get("id") or items[0].get("transactionId") or "")
            if page > 1 and first_id and seen_first_id == first_id:
                break
            if page == 1 and first_id:
                seen_first_id = first_id

            all_items.extend(items)

            if len(items) < page_size:
                break

            page += 1
            if page > 100:
                break

        return all_items

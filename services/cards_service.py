import requests
from auth.concur_oauth import ConcurOAuthClient

class CardsService:
    def __init__(self, oauth: ConcurOAuthClient):
        self.oauth = oauth

    def get_transactions_for_user(
        self,
        concur_user_id: str,
        transaction_date_from: str,
        transaction_date_to: str
    ):
        url = f"{self.oauth.base_url}/cards/v4/users/{concur_user_id}/transactions"
        headers = {
            "Authorization": f"Bearer {self.oauth.get_access_token()}",
            "Accept": "application/json"
        }
        params = {
            "transactionDateFrom": transaction_date_from,
            "transactionDateTo": transaction_date_to
        }
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("Items", [])

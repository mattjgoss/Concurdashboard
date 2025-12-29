import requests
from typing import List, Dict
from app.auth.concur_oauth import ConcurOAuthClient

class IdentityService:
    def __init__(self, oauth: ConcurOAuthClient):
        self.oauth = oauth

    def search_users(self, filter_expression: str) -> List[Dict]:
        url = f"{self.oauth.base_url}/profile/identity/v4.1/Users"
        headers = {
            "Authorization": f"Bearer {self.oauth.get_access_token()}",
            "Accept": "application/json"
        }
        params = {
            "filter": filter_expression,
            "attributes": (
                "id,userName,displayName,emails.value,"
                "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
                "urn:ietf:params:scim:schemas:extension:spend:2.0:User"
            )
        }
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("Resources", [])

import time
import requests
from app.keyvault import get_secret

class ConcurOAuthClient:
    def __init__(self):
        self.base_url = get_secret("CONCUR_BASE_URL")
        self.client_id = get_secret("CONCUR_CLIENT_ID")
        self.client_secret = get_secret("CONCUR_CLIENT_SECRET")
        self.refresh_token = get_secret("CONCUR_REFRESH_TOKEN")
        self._access_token = None
        self._expires_at = 0

    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at - 60:
            return self._access_token

        token_url = f"{self.base_url}/oauth2/v0/token"
        resp = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret
            },
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._expires_at = now + data.get("expires_in", 1800)

        # rotate refresh token if Concur returns a new one
        if "refresh_token" in data:
            # OPTIONAL: store back to Key Vault
            pass

        return self._access_token

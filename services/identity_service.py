import os
import time
from typing import List, Dict, Optional

import requests
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from auth.concur_oauth import ConcurOAuthClient

# ======================================================
# KEY VAULT (Managed Identity) + caching (Phase 1 safe)
# ======================================================

KEYVAULT_NAME = os.getenv("KEYVAULT_NAME")  # may be None in Phase 1; do NOT crash at import time
KEYVAULT_URL = f"https://{KEYVAULT_NAME}.vault.azure.net/" if KEYVAULT_NAME else None

_credential: Optional[DefaultAzureCredential] = None
_secret_client: Optional[SecretClient] = None

_SECRET_CACHE: Dict[str, Dict[str, object]] = {}
_SECRET_TTL_SECONDS = int(os.getenv("SECRET_TTL_SECONDS", "300"))  # default 5 mins


def _get_secret_client() -> SecretClient:
    """
    Lazily create a Key Vault SecretClient.
    Phase 1 requirement: app must start even if KEYVAULT_NAME is not set yet.
    """
    global _credential, _secret_client

    if not KEYVAULT_NAME:
        raise RuntimeError("KEYVAULT_NAME environment variable is not set in App Service.")

    if not KEYVAULT_URL:
        # Defensive; should never happen if KEYVAULT_NAME is set
        raise RuntimeError("KEYVAULT_URL could not be constructed from KEYVAULT_NAME.")

    if _secret_client is None:
        _credential = DefaultAzureCredential()
        _secret_client = SecretClient(vault_url=KEYVAULT_URL, credential=_credential)

    return _secret_client


def keyvault_status() -> Dict[str, object]:
    """
    Lightweight status info for diagnostics (e.g. /kv-test).
    Does not call Key Vault; just reports readiness/config.
    """
    return {
        "keyvault_name_set": bool(KEYVAULT_NAME),
        "keyvault_name": KEYVAULT_NAME,
        "keyvault_url": KEYVAULT_URL,
        "client_initialized": _secret_client is not None,
        "cache_size": len(_SECRET_CACHE),
        "cache_ttl_seconds": _SECRET_TTL_SECONDS,
    }


def get_secret(name: str) -> str:
    """
    Read a secret from Azure Key Vault using Managed Identity.
    Includes a short in-memory cache to reduce Key Vault calls.
    """
    now = time.time()
    cached = _SECRET_CACHE.get(name)

    if cached and (now - float(cached["ts"])) < _SECRET_TTL_SECONDS:
        return str(cached["value"])

    client = _get_secret_client()
    value = client.get_secret(name).value

    _SECRET_CACHE[name] = {"value": value, "ts": now}
    return str(value)


def concur_base_url() -> str:
    """
    Returns Concur API base URL from Key Vault.
    Secret name expected: concur-api-base-url
    Example value: https://us2.api.concursolutions.com
    """
    return get_secret("concur-api-base-url").rstrip("/")


# ======================================================
# IDENTITY SERVICE (Concur Identity v4.1)
# ======================================================

class IdentityService:
    """
    Wrapper for SAP Concur Identity v4.1 (SCIM) user search.

    This service:
      - Uses ConcurOAuthClient for access tokens
      - Optionally uses Key Vault for the Concur base URL (concur-api-base-url)
      - Supports SCIM pagination to handle large result sets
    """

    def __init__(self, oauth: ConcurOAuthClient):
        self.oauth = oauth

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.oauth.get_access_token()}",
            "Accept": "application/json",
        }

    def search_users(
        self,
        filter_expression: str,
        *,
        attributes: Optional[str] = None,
        start_index: int = 1,
        count: int = 100,
        max_pages: int = 20,
        use_keyvault_base_url: bool = True,
    ) -> List[Dict]:
        """
        Search Concur users using Identity v4.1 SCIM filter.

        Args:
            filter_expression: SCIM filter expression.
            attributes: SCIM attributes selection (optional).
            start_index: SCIM startIndex (1-based).
            count: SCIM count (page size).
            max_pages: hard cap to prevent runaway pagination.
            use_keyvault_base_url: if True, uses Key Vault base URL. If False, uses oauth.base_url.

        Returns:
            List[Dict] of user objects (Resources).
        """
        if use_keyvault_base_url:
            base_url = concur_base_url()
        else:
            base_url = str(self.oauth.base_url).rstrip("/")

        url = f"{base_url}/profile/identity/v4.1/Users"

        attrs = attributes or (
            "id,userName,displayName,emails.value,"
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
            "urn:ietf:params:scim:schemas:extension:spend:2.0:User"
        )

        results: List[Dict] = []
        next_start = start_index
        page = 0

        while page < max_pages:
            params = {
                "filter": filter_expression,
                "attributes": attrs,
                "startIndex": next_start,
                "count": count,
            }

            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()

            payload = resp.json() or {}
            resources = payload.get("Resources") or []
            results.extend(resources)

            if not resources:
                break

            # SCIM pagination fields (varies by implementation)
            total_results = int(payload.get("totalResults") or 0)
            items_per_page = int(payload.get("itemsPerPage") or len(resources) or 0)

            next_start += items_per_page
            page += 1

            # Stop when we have fetched all results
            if total_results and next_start > total_results:
                break

        return results

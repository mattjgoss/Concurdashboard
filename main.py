print("#### LOADED MAIN FROM:", __file__)

import os
import sys
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Your existing modules (already uploaded in the new app)
from auth.azure_ad import get_current_user, get_azure_ad_config_status
from auth.concur_oauth import ConcurOAuthClient
from services.identity_service import get_secret, keyvault_status
from services.excel_export import export_accruals_to_excel


# ======================================================
# APP
# ======================================================

app = FastAPI(title="Concur Accruals API")

BUILD_FINGERPRINT = os.getenv("SCM_COMMIT_ID") or os.getenv("WEBSITE_DEPLOYMENT_ID") or "unknown"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ======================================================
# HELPERS
# ======================================================


def env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return fallback
    return v


def kv(name: str, fallback: Optional[str] = None) -> Optional[str]:
    try:
        return get_secret(name)
    except Exception:
        return fallback


def concur_base_url() -> str:
    """
    Concur API base URL.
    Prefer Key Vault secret: 'concur-api-base-url'
    Fallback env: CONCUR_API_BASE_URL, then CONCUR_BASE_URL.
    """
    return (
        kv("concur-api-base-url")
        or env("CONCUR_API_BASE_URL")
        or env("CONCUR_BASE_URL")
        or "https://www.concursolutions.com"
    ).rstrip("/")


# ======================================================
# CONCUR OAUTH CLIENT (cached)
# ======================================================

_oauth_client: Optional[ConcurOAuthClient] = None


def get_oauth_client() -> ConcurOAuthClient:
    global _oauth_client
    if _oauth_client is not None:
        return _oauth_client

    token_url = kv("concur-token-url") or env("CONCUR_TOKEN_URL")
    client_id = kv("concur-client-id") or env("CONCUR_CLIENT_ID")
    client_secret = kv("concur-client-secret") or env("CONCUR_CLIENT_SECRET")
    refresh_token = kv("concur-refresh-token") or env("CONCUR_REFRESH_TOKEN")

    missing = []
    if not token_url:
        missing.append("concur-token-url / CONCUR_TOKEN_URL")
    if not client_id:
        missing.append("concur-client-id / CONCUR_CLIENT_ID")
    if not client_secret:
        missing.append("concur-client-secret / CONCUR_CLIENT_SECRET")
    if not refresh_token:
        missing.append("concur-refresh-token / CONCUR_REFRESH_TOKEN")

    if missing:
        raise HTTPException(status_code=500, detail={"error": "missing_concur_oauth_config", "missing": missing})

    _oauth_client = ConcurOAuthClient(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    return _oauth_client


def concur_headers() -> Dict[str, str]:
    oauth = get_oauth_client()
    token = oauth.get_access_token()
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ======================================================
# CORS
# ======================================================

allowed_origin = env("SP_ORIGIN", "")
origins = [allowed_origin] if allowed_origin else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================================
# DEBUG / HEALTH (Azure test endpoints)
# ======================================================


@app.get("/build")
def build():
    return {
        "fingerprint": BUILD_FINGERPRINT,
        "run_from_package": env("WEBSITE_RUN_FROM_PACKAGE"),
        "cwd": os.getcwd(),
        "pythonpath0": sys.path[0] if sys.path else None,
    }


@app.get("/kv-test")
def kv_test():
    return {"status": "ok", "keyvault": keyvault_status()}


@app.get("/auth/config-status")
def auth_config_status():
    return get_azure_ad_config_status()


@app.get("/api/whoami")
def whoami(current_user: Dict[str, Any] = Depends(get_current_user)):
    return current_user


@app.get("/api/concur/auth-test")
def api_concur_auth_test():
    """
    Confirms Concur OAuth refresh flow works and outbound calls succeed.
    """
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users"
    try:
        resp = requests.get(url, headers=concur_headers(), params={"count": 1}, timeout=30)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={"where": "concur_auth_test", "error": "request_failed", "message": str(ex)},
        )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "concur_auth_test",
                "error": "concur_error",
                "concur_status": resp.status_code,
                "url": url,
                "base_url": base,
                "response": (resp.text or "")[:2000],
            },
        )

    payload = resp.json() if resp.content else {}
    return {
        "ok": True,
        "status_code": resp.status_code,
        "base_url": base,
        "sample": (payload.get("Resources") or [])[:1],
    }


# ======================================================
# MODELS (kept minimal for now)
# ======================================================

class UnassignedCardsRequest(BaseModel):
    transactionDateFrom: str
    transactionDateTo: str
    pageSize: int = 200


# ======================================================
# IDENTITY HELPERS (tenant-safe attributes fallback)
# ======================================================

ATTRS_WITH_CONCUR_EXT = (
    "id,userName,displayName,active,"
    "name,emails,phoneNumbers,addresses,timezone,locale,preferredLanguage,groups,meta,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
    "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
)

ATTRS_NO_CONCUR_EXT = (
    "id,userName,displayName,active,"
    "name,emails,phoneNumbers,addresses,timezone,locale,preferredLanguage,groups,meta,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
)


def _is_unrecognized_attributes_400(resp: requests.Response) -> bool:
    if resp.status_code != 400:
        return False
    try:
        j = resp.json()
        detail = (j or {}).get("detail") or ""
        return "Unrecognized attributes" in str(detail)
    except Exception:
        return False


def _extract_primary_email(user: Dict[str, Any]) -> Optional[str]:
    emails = user.get("emails") or []
    if isinstance(emails, list):
        for e in emails:
            if isinstance(e, dict) and e.get("value"):
                return str(e.get("value"))
    return None


def _to_grid_row_identity(u: Dict[str, Any]) -> Dict[str, Any]:
    enterprise = u.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User") or {}
    if not isinstance(enterprise, dict):
        enterprise = {}

    # Note: Concur extension may not exist in this tenant; don’t depend on it.
    return {
        "id": u.get("id"),
        "displayName": u.get("displayName"),
        "userName": u.get("userName"),
        "email": _extract_primary_email(u),
        "active": u.get("active"),
        "employeeNumber": enterprise.get("employeeNumber"),
    }


def _identity_list_users_paged(
    *,
    attributes: str,
    count: int = 200,
    max_pages: int = 200,
) -> List[Dict[str, Any]]:
    """
    Lists Identity v4.1 Users with paging.
    Raises a useful error payload if Concur returns an error.
    """
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users"

    all_users: List[Dict[str, Any]] = []
    start_index = 1
    page = 0

    while page < max_pages:
        params = {"attributes": attributes, "startIndex": start_index, "count": count}

        try:
            resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        except Exception as ex:
            raise HTTPException(
                status_code=502,
                detail={
                    "where": "identity_list_users_paged",
                    "error": "request_failed",
                    "message": str(ex),
                    "url": url,
                    "params": params,
                    "base_url": base,
                },
            )

        if not resp.ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "where": "identity_list_users_paged",
                    "error": "concur_error",
                    "concur_status": resp.status_code,
                    "url": url,
                    "params": params,
                    "base_url": base,
                    "response": (resp.text or "")[:2000],
                },
            )

        payload = resp.json() or {}
        resources = payload.get("Resources") or []
        if not isinstance(resources, list) or not resources:
            break

        all_users.extend(resources)

        total_results = int(payload.get("totalResults") or 0)
        items_per_page = int(payload.get("itemsPerPage") or len(resources) or 0)

        start_index += max(items_per_page, 1)
        page += 1

        if total_results and start_index > total_results:
            break

        if len(resources) < count:
            break

    return all_users


def list_users_tenant_safe(take: int) -> Tuple[List[Dict[str, Any]], str]:
    """
    Try with Concur extension attributes first. If tenant rejects them (400 BAD_QUERY),
    fall back to a safe attribute set.
    Returns: (users, attribute_mode)
    """
    try:
        users = _identity_list_users_paged(attributes=ATTRS_WITH_CONCUR_EXT, count=200, max_pages=200)
        return users[:take], "with_concur_extension"
    except HTTPException as he:
        # If the tenant rejects the Concur extension, retry without it.
        detail = he.detail if isinstance(he.detail, dict) else {}
        if detail.get("concur_status") == 400 and "Unrecognized attributes" in str(detail.get("response", "")):
            users = _identity_list_users_paged(attributes=ATTRS_NO_CONCUR_EXT, count=200, max_pages=200)
            return users[:take], "no_concur_extension"
        raise


def get_user_detail_identity(user_id: str) -> Dict[str, Any]:
    """
    Fetch SCIM record for a userId from Identity v4.1.
    Attempts to explicitly request the widest common attribute set; falls back if tenant rejects.
    """
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users/{user_id}"

    # Try requesting broad attributes first; if tenant rejects concur extension, fall back.
    try:
        resp = requests.get(url, headers=concur_headers(), params={"attributes": ATTRS_WITH_CONCUR_EXT}, timeout=30)
        if _is_unrecognized_attributes_400(resp):
            resp = requests.get(url, headers=concur_headers(), params={"attributes": ATTRS_NO_CONCUR_EXT}, timeout=30)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={"where": "user_detail_identity", "error": "request_failed", "message": str(ex), "url": url},
        )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "user_detail_identity",
                "error": "concur_error",
                "concur_status": resp.status_code,
                "url": url,
                "base_url": base,
                "response": (resp.text or "")[:2000],
            },
        )

    return resp.json() or {}


# ======================================================
# FULL PROFILE HELPERS (Identity + Spend + Travel)
# ======================================================

def _concur_get_json(url: str, *, params: Optional[Dict[str, Any]] = None, where: str = "concur_get") -> Dict[str, Any]:
    try:
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": where,
                "error": "request_failed",
                "message": str(ex),
                "url": url,
                "params": params or {},
            },
        )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": where,
                "error": "concur_error",
                "concur_status": resp.status_code,
                "url": url,
                "params": params or {},
                "base_url": concur_base_url(),
                "response": (resp.text or "")[:2000],
            },
        )

    return resp.json() if resp.content else {}


def get_user_detail_spend(user_id: str) -> Dict[str, Any]:
    """
    Spend user profile (includes customData[] where ids include:
    custom1..custom22 and orgUnit1..orgUnit6, plus many other spend extensions).
    """
    base = concur_base_url()
    url = f"{base}/profile/spend/v4.1/Users/{user_id}"
    return _concur_get_json(url, where="user_detail_spend")


def get_user_detail_travel(user_id: str) -> Dict[str, Any]:
    """
    Travel user extension (ruleClass, orgUnit, customFields, groups, etc.).
    """
    base = concur_base_url()
    url = f"{base}/profile/travel/v4/Users/{user_id}"
    return _concur_get_json(url, where="user_detail_travel")


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deep merge dictionaries (src into dst). Lists are replaced, not merged.
    """
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _materialise_custom_fields_from_spend(spend_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Convenience: turn Spend customData[] into derived dicts so UI doesn't need to scan arrays.

    Returns:
      {
        "custom": {"custom1": "...", ..., "custom22": "..."},
        "orgUnits": {"orgUnit1": "...", ..., "orgUnit6": "..."}
      }
    """
    out = {"custom": {}, "orgUnits": {}}
    if not isinstance(spend_payload, dict):
        return out

    # customData is typically under spend user extension schema; but to be robust, scan top-level.
    custom_data = spend_payload.get("customData")
    if not isinstance(custom_data, list):
        # Sometimes it's nested under the Spend extension URN; scan for any dict containing customData.
        for v in spend_payload.values():
            if isinstance(v, dict) and isinstance(v.get("customData"), list):
                custom_data = v.get("customData")
                break

    if not isinstance(custom_data, list):
        return out

    for item in custom_data:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        val = item.get("value")
        if not cid:
            continue
        if cid.startswith("custom"):
            out["custom"][cid] = val
        if cid.startswith("orgUnit"):
            out["orgUnits"][cid] = val

    # Ensure all expected keys exist (custom1..custom22, orgUnit1..orgUnit6)
    for i in range(1, 23):
        k = f"custom{i}"
        out["custom"].setdefault(k, None)
    for i in range(1, 7):
        k = f"orgUnit{i}"
        out["orgUnits"].setdefault(k, None)

    return out


# ======================================================
# ROUTES YOU NEED FOR SHAREPOINT (Users)
# ======================================================

@app.get("/api/users")
def api_users_list(
    take: int = Query(500, ge=1, le=5000),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    users, mode = list_users_tenant_safe(take=take)

    requested_by = (
        current_user.get("upn")
        or current_user.get("unique_name")
        or current_user.get("preferred_username")
        or current_user.get("email")
    )

    return {
        "meta": {
            "requestedBy": requested_by,
            "returned": len(users),
            "concurBaseUrl": concur_base_url(),
            "attributeMode": mode,
        },
        "users": [_to_grid_row_identity(u) for u in users],
    }


@app.get("/api/users/{user_id}")
def api_user_detail(
    user_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    if not user_id or user_id.strip() == "":
        raise HTTPException(status_code=400, detail={"error": "missing_user_id", "message": "user_id is required"})
    return get_user_detail_identity(user_id)


@app.get("/api/users/{user_id}/full")
def api_user_detail_full(
    user_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Returns the maximum available employee profile data by aggregating:
      - Identity v4.1 user (core + enterprise + optional concur ext)
      - Spend v4.1 user (includes customData: custom1..custom22, orgUnit1..orgUnit6, plus spend extensions)
      - Travel v4 user (travel extension data)

    Does not fail the whole response if Spend or Travel is unavailable; surfaces partialFailures.
    """
    if not user_id or user_id.strip() == "":
        raise HTTPException(status_code=400, detail={"error": "missing_user_id", "message": "user_id is required"})

    requested_by = (
        current_user.get("upn")
        or current_user.get("unique_name")
        or current_user.get("preferred_username")
        or current_user.get("email")
    )

    identity = get_user_detail_identity(user_id)

    partial_failures: Dict[str, Any] = {}

    try:
        spend = get_user_detail_spend(user_id)
    except HTTPException as he:
        spend = None
        partial_failures["spend"] = he.detail

    try:
        travel = get_user_detail_travel(user_id)
    except HTTPException as he:
        travel = None
        partial_failures["travel"] = he.detail

    # Merge into a single combined payload for easy UI rendering.
    combined: Dict[str, Any] = {}
    if isinstance(identity, dict):
        _deep_merge(combined, identity)
    if isinstance(spend, dict):
        _deep_merge(combined, spend)
    if isinstance(travel, dict):
        _deep_merge(combined, travel)

    # Add per-service meta (avoid overwrite) and derived convenience fields.
    combined["_metaByService"] = {
        "identity": identity.get("meta") if isinstance(identity, dict) else None,
        "spend": spend.get("meta") if isinstance(spend, dict) else None,
        "travel": travel.get("meta") if isinstance(travel, dict) else None,
    }
    combined["_derived"] = _materialise_custom_fields_from_spend(spend if isinstance(spend, dict) else None)

    return {
        "meta": {
            "requestedBy": requested_by,
            "concurBaseUrl": concur_base_url(),
            "hasPartialFailures": bool(partial_failures),
        },
        "userId": user_id,
        "sources": {
            "identity": identity,
            "spend": spend,
            "travel": travel,
        },
        "combined": combined,
        "partialFailures": partial_failures,
    }


# ======================================================
# OPTIONAL: KEEP EXCEL EXPORT HOOKS (no change)
# ======================================================

@app.get("/api/users/export")
def api_users_export(
    take: int = Query(1000, ge=1, le=5000),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    users, mode = list_users_tenant_safe(take=take)
    rows = [_to_grid_row_identity(u) for u in users]

    excel_bytes = export_accruals_to_excel(
        unsubmitted_reports=[],
        unassigned_cards=[],
        card_totals_by_program=None,
        card_totals_by_user=None,
        meta={"export": "users", "attributeMode": mode, "returned": len(rows)},
        extra_sheets={"Users": rows},  # if your excel_export supports it; if not, remove this line
    )

    filename = f"Concur_Users_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

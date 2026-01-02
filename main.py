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

# Existing project modules
from auth.azure_ad import get_current_user, get_azure_ad_config_status
from auth.concur_oauth import ConcurOAuthClient
from services.identity_service import get_secret, keyvault_status
from services.excel_export import export_accruals_to_excel


# ======================================================
# APP
# ======================================================

app = FastAPI(title="Concur Accruals API")

BUILD_FINGERPRINT = (
    os.getenv("SCM_COMMIT_ID") or os.getenv("WEBSITE_DEPLOYMENT_ID") or "unknown"
)

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


def api_app_id() -> str:
    """
    Entra App (Application) ID for THIS FastAPI backend (the API resource).
    Used only to print token-generation commands for Swagger testing.
    """
    return (env("API_APP_ID") or kv("api-app-id") or "").strip()


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
        raise HTTPException(
            status_code=500,
            detail={"error": "missing_concur_oauth_config", "missing": missing},
        )

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


def _concur_get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    where: str = "concur_get",
) -> Dict[str, Any]:
    """
    Shared Concur GET helper that raises a structured HTTPException on failure.
    """
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
        resp = requests.get(
            url, headers=concur_headers(), params={"count": 1}, timeout=30
        )
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "concur_auth_test",
                "error": "request_failed",
                "message": str(ex),
            },
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
# NEW: Swagger helper endpoints (SAFE)
# ======================================================


@app.get("/api/tools/token-command")
def token_command():
    """
    Returns COPY/PASTE commands to obtain an Entra user bearer token for this API.
    IMPORTANT:
      - This endpoint NEVER returns a token value
      - You must run the commands in Cloud Shell / terminal
      - Then paste: Bearer <FULL TOKEN> into Swagger Authorize
    """
    app_id = api_app_id() or "648a2fa4-dc6d-429c-8c50-ce51f48beb24"
    host = env("WEBSITE_HOSTNAME") or env("APP_HOSTNAME") or ""
    base = f"https://{host}".rstrip("/") if host else ""

    bash_block = f"""API_APP_ID="{app_id}"
TOKEN=$(az account get-access-token --scope "api://$API_APP_ID/.default" --query accessToken -o tsv)
echo "$TOKEN"
echo "Swagger: {base}/docs"
"""

    powershell_block = f"""$API_APP_ID = "{app_id}"
$TOKEN = az account get-access-token --scope "api://$API_APP_ID/.default" --query accessToken -o tsv
$TOKEN
Write-Host "Swagger: {base}/docs"
"""

    return {
        "purpose": "Generate a FULL Entra bearer token for Swagger (/docs) testing",
        "important": [
            "This API will NEVER return a token value",
            "Run the commands below in Cloud Shell or a local terminal",
            "Paste into Swagger Authorize as: Bearer <FULL TOKEN>",
        ],
        "bash": bash_block,
        "powershell": powershell_block,
    }


@app.get("/api/tools/swagger-howto")
def swagger_howto():
    """
    Returns the exact steps to run secured endpoints in Swagger UI (/docs).
    """
    host = env("WEBSITE_HOSTNAME") or env("APP_HOSTNAME") or ""
    base = f"https://{host}".rstrip("/") if host else ""
    return {
        "swaggerUrl": f"{base}/docs" if base else "/docs",
        "steps": [
            "Run GET /api/tools/token-command and copy the bash (or powershell) block into Cloud Shell / terminal.",
            "Copy the FULL printed token output.",
            "Open /docs and click Authorize (top right).",
            "Paste: Bearer <FULL_TOKEN> (include the word Bearer). Click Authorize then Close.",
            "Now you can Try it out + Execute on any secured endpoint (/api/whoami, /api/users, /api/users/{id}/full, etc).",
        ],
        "security_note": "Swagger UI cannot mint tokens. Tokens must come from Entra (az CLI, MSAL, etc.). The API should not expose an endpoint that returns tokens.",
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
#
# IMPORTANT:
# - Concur tenants differ in what they accept in the SCIM "attributes" parameter.
# - Some tenants reject 'groups' (as you saw: BAD_QUERY Unrecognized attributes: groups).
# - We DO NOT include 'groups' in the list endpoints to keep this tenant-safe.
#

ATTRS_WITH_CONCUR_EXT = (
    "id,userName,displayName,active,"
    "name,emails,phoneNumbers,addresses,timezone,locale,preferredLanguage,meta,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
    "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
)

ATTRS_NO_CONCUR_EXT = (
    "id,userName,displayName,active,"
    "name,emails,phoneNumbers,addresses,timezone,locale,preferredLanguage,meta,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
)


def _extract_primary_email(user: Dict[str, Any]) -> Optional[str]:
    emails = user.get("emails") or []
    if isinstance(emails, list):
        for e in emails:
            if isinstance(e, dict) and e.get("value"):
                return str(e.get("value"))
    return None


def _to_grid_row_identity(u: Dict[str, Any]) -> Dict[str, Any]:
    enterprise = (
        u.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User") or {}
    )
    if not isinstance(enterprise, dict):
        enterprise = {}

    return {
        "id": u.get("id"),
        "displayName": u.get("displayName"),
        "userName": u.get("userName"),
        "email": _extract_primary_email(u),
        "active": u.get("active"),
        "employeeNumber": enterprise.get("employeeNumber"),
    }


def _extract_unrecognized_attribute_from_text(resp: requests.Response) -> Optional[str]:
    """
    Concur error formats vary.
    We reliably catch: 'Unrecognized attributes: groups'
    """
    text = resp.text or ""
    marker = "Unrecognized attributes:"
    if marker in text:
        tail = text.split(marker, 1)[1].strip()
        if tail:
            return tail.split(",")[0].strip()

    # Some tenants put the message inside JSON "detail"
    try:
        j = resp.json() or {}
        detail = str(j.get("detail") or "")
        if marker in detail:
            tail = detail.split(marker, 1)[1].strip()
            if tail:
                return tail.split(",")[0].strip()
    except Exception:
        pass

    return None


def _remove_attribute_from_list(attr_string: str, attr_to_remove: str) -> str:
    parts = [p.strip() for p in attr_string.split(",") if p.strip()]
    parts = [p for p in parts if p != attr_to_remove]
    return ",".join(parts)


def _identity_list_users_paged(
    *,
    attributes: str,
    count: int = 200,
    max_pages: int = 200,
    max_attr_fixes: int = 6,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Returns: (users, attributes_used)
    Tenant-safe:
      - If Concur returns 400 "Unrecognized attributes: X", remove X and retry.
    """
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users"

    all_users: List[Dict[str, Any]] = []
    start_index = 1
    page = 0

    attrs = attributes
    fixes_used = 0

    while page < max_pages:
        params = {"attributes": attrs, "startIndex": start_index, "count": count}

        try:
            resp = requests.get(
                url, headers=concur_headers(), params=params, timeout=30
            )
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
            # Tenant-safe fix: remove unrecognized attribute and retry this same page
            if resp.status_code == 400 and fixes_used < max_attr_fixes:
                unrec = _extract_unrecognized_attribute_from_text(resp)
                if unrec:
                    new_attrs = _remove_attribute_from_list(attrs, unrec)
                    if new_attrs != attrs and new_attrs.strip():
                        attrs = new_attrs
                        fixes_used += 1
                        continue

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

    return all_users, attrs


def list_users_tenant_safe(take: int) -> Tuple[List[Dict[str, Any]], str]:
    """
    Try with Concur extension first.
    If Concur extension is rejected, fall back.
    Also auto-removes any other unrecognized attribute (tenant-safe).
    """
    try:
        users, attrs_used = _identity_list_users_paged(
            attributes=ATTRS_WITH_CONCUR_EXT, count=200, max_pages=200
        )
        return users[:take], f"with_concur_extension(attrs={attrs_used})"
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, dict) else {}
        # If Concur extension itself is rejected in some tenants, retry without it
        if detail.get("concur_status") == 400 and (
            "Unrecognized attributes" in str(detail.get("response", ""))
            or "BAD_QUERY" in str(detail.get("response", ""))
        ):
            users, attrs_used = _identity_list_users_paged(
                attributes=ATTRS_NO_CONCUR_EXT, count=200, max_pages=200
            )
            return users[:take], f"no_concur_extension(attrs={attrs_used})"
        raise


def get_user_detail_identity(user_id: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users/{user_id}"

    attrs = ATTRS_WITH_CONCUR_EXT
    resp: Optional[requests.Response] = None

    try:
        for _ in range(6):
            resp = requests.get(
                url, headers=concur_headers(), params={"attributes": attrs}, timeout=30
            )

            if resp.status_code == 400:
                unrec = _extract_unrecognized_attribute_from_text(resp)
                if unrec:
                    attrs2 = _remove_attribute_from_list(attrs, unrec)
                    if attrs2 == attrs:
                        break
                    attrs = attrs2
                    continue

                if (
                    "Unrecognized attributes" in (resp.text or "")
                    and "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
                    in attrs
                ):
                    attrs = ATTRS_NO_CONCUR_EXT
                    continue

            break

    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "user_detail_identity",
                "error": "request_failed",
                "message": str(ex),
                "url": url,
            },
        )

    if resp is None or not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "user_detail_identity",
                "error": "concur_error",
                "concur_status": (resp.status_code if resp is not None else None),
                "url": url,
                "base_url": base,
                "response": ((resp.text or "")[:2000] if resp is not None else ""),
                "attributes_used": attrs,
            },
        )

    return resp.json() or {}


# ======================================================
# FULL PROFILE HELPERS (Identity + Spend + Travel + List expansion)
# ======================================================


def get_user_detail_spend(user_id: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/profile/spend/v4.1/Users/{user_id}"
    return _concur_get_json(url, where="user_detail_spend")


def get_user_detail_travel(user_id: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/profile/travel/v4/Users/{user_id}"
    return _concur_get_json(url, where="user_detail_travel")


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _materialise_custom_fields_from_spend(
    spend_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"custom": {}, "orgUnits": {}}
    if not isinstance(spend_payload, dict):
        for i in range(1, 23):
            out["custom"][f"custom{i}"] = None
        for i in range(1, 7):
            out["orgUnits"][f"orgUnit{i}"] = None
        return out

    custom_data = spend_payload.get("customData")
    if not isinstance(custom_data, list):
        for v in spend_payload.values():
            if isinstance(v, dict) and isinstance(v.get("customData"), list):
                custom_data = v.get("customData")
                break

    if isinstance(custom_data, list):
        for item in custom_data:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            if not cid:
                continue
            val = item.get("value")
            if cid.startswith("custom"):
                out["custom"][cid] = val
            elif cid.startswith("orgUnit"):
                out["orgUnits"][cid] = val

    for i in range(1, 23):
        out["custom"].setdefault(f"custom{i}", None)
    for i in range(1, 7):
        out["orgUnits"].setdefault(f"orgUnit{i}", None)

    return out


def _parse_expand(expand: Optional[str]) -> List[str]:
    if not expand:
        return []
    return [p.strip() for p in expand.split(",") if p.strip()]


def _collect_list_item_refs_from_spend(
    spend_payload: Optional[Dict[str, Any]],
) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    if not isinstance(spend_payload, dict):
        return refs

    custom_data = spend_payload.get("customData")
    if not isinstance(custom_data, list):
        for v in spend_payload.values():
            if isinstance(v, dict) and isinstance(v.get("customData"), list):
                custom_data = v.get("customData")
                break

    if not isinstance(custom_data, list):
        return refs

    for item in custom_data:
        if not isinstance(item, dict):
            continue
        href = str(item.get("href") or "").strip()
        sync_guid = str(item.get("syncGuid") or "").strip()
        field_id = str(item.get("id") or "").strip()

        if not href:
            continue

        key = sync_guid or href
        refs.append(
            {"key": key, "href": href, "syncGuid": sync_guid, "fieldId": field_id}
        )

    seen = set()
    out: List[Dict[str, str]] = []
    for r in refs:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        out.append(r)
    return out


def _fetch_list_item_by_href(href: str) -> Dict[str, Any]:
    try:
        resp = requests.get(href, headers=concur_headers(), timeout=30)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "list_item_fetch",
                "error": "request_failed",
                "message": str(ex),
                "href": href,
            },
        )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "list_item_fetch",
                "error": "concur_error",
                "concur_status": resp.status_code,
                "href": href,
                "response": (resp.text or "")[:2000],
            },
        )

    return resp.json() if resp.content else {}


def expand_list_items_from_spend(
    spend_payload: Optional[Dict[str, Any]],
    *,
    limit: int = 50,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    items: Dict[str, Any] = {}
    errors: Dict[str, Any] = {}

    refs = _collect_list_item_refs_from_spend(spend_payload)
    if not refs:
        return items, errors

    refs = refs[: max(0, int(limit))]

    for r in refs:
        key = r["key"]
        href = r["href"]
        try:
            payload = _fetch_list_item_by_href(href)
            items[key] = {"ref": r, "item": payload}
        except HTTPException as he:
            errors[key] = {"ref": r, "error": he.detail}

    return items, errors


def _summarise_list_item(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {}

    lists = item.get("lists") or []
    list_id = None
    if isinstance(lists, list) and lists:
        first = lists[0] if isinstance(lists[0], dict) else None
        if first:
            list_id = first.get("id")

    return {
        "id": item.get("id"),
        "code": item.get("code"),
        "shortCode": item.get("shortCode"),
        "value": item.get("value"),
        "parentId": item.get("parentId"),
        "level": item.get("level"),
        "hasChildren": item.get("hasChildren"),
        "isDeleted": item.get("isDeleted"),
        "listId": list_id,
    }


def build_resolved_from_expanded_list_items(
    items_by_key: Dict[str, Any],
) -> Dict[str, Any]:
    resolved = {"orgUnits": {}, "custom": {}}
    if not isinstance(items_by_key, dict):
        return resolved

    for _key, blob in items_by_key.items():
        if not isinstance(blob, dict):
            continue
        ref = blob.get("ref") if isinstance(blob.get("ref"), dict) else {}
        field_id = str(ref.get("fieldId") or "").strip()

        item = blob.get("item") if isinstance(blob.get("item"), dict) else {}
        summary = _summarise_list_item(item)
        if not summary:
            continue

        if field_id.startswith("orgUnit"):
            resolved["orgUnits"][field_id] = summary
        elif field_id.startswith("custom"):
            resolved["custom"][field_id] = summary

    for i in range(1, 7):
        resolved["orgUnits"].setdefault(f"orgUnit{i}", None)
    for i in range(1, 23):
        resolved["custom"].setdefault(f"custom{i}", None)

    return resolved


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
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_user_id", "message": "user_id is required"},
        )
    return get_user_detail_identity(user_id)


@app.get("/api/users/{user_id}/full")
def api_user_detail_full(
    user_id: str,
    expand: Optional[str] = Query(
        None, description="Optional expansions, e.g. 'listItems'"
    ),
    expandLimit: int = Query(
        50, ge=0, le=200, description="Max list items to expand (safety cap)"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    if not user_id or user_id.strip() == "":
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_user_id", "message": "user_id is required"},
        )

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

    combined: Dict[str, Any] = {}
    if isinstance(identity, dict):
        _deep_merge(combined, identity)
    if isinstance(spend, dict):
        _deep_merge(combined, spend)
    if isinstance(travel, dict):
        _deep_merge(combined, travel)

    combined["_metaByService"] = {
        "identity": identity.get("meta") if isinstance(identity, dict) else None,
        "spend": spend.get("meta") if isinstance(spend, dict) else None,
        "travel": travel.get("meta") if isinstance(travel, dict) else None,
    }
    combined["_derived"] = _materialise_custom_fields_from_spend(
        spend if isinstance(spend, dict) else None
    )

    expanded: Dict[str, Any] = {}
    expansions = _parse_expand(expand)

    if "listItems" in expansions:
        if isinstance(spend, dict):
            items, errors = expand_list_items_from_spend(spend, limit=expandLimit)
            expanded["listItems"] = items
            expanded["listItemsErrors"] = errors
            expanded["listItemsMeta"] = {
                "requested": len(_collect_list_item_refs_from_spend(spend)),
                "expanded": len(items),
                "errors": len(errors),
                "limit": expandLimit,
            }
            combined["_derived"]["resolved"] = build_resolved_from_expanded_list_items(
                items
            )
        else:
            expanded["listItems"] = {}
            expanded["listItemsErrors"] = {}
            expanded["listItemsMeta"] = {
                "requested": 0,
                "expanded": 0,
                "errors": 0,
                "limit": expandLimit,
            }
            combined["_derived"]["resolved"] = {"orgUnits": {}, "custom": {}}

    return {
        "meta": {
            "requestedBy": requested_by,
            "concurBaseUrl": concur_base_url(),
            "hasPartialFailures": bool(partial_failures),
            "expand": expansions,
        },
        "userId": user_id,
        "sources": {"identity": identity, "spend": spend, "travel": travel},
        "combined": combined,
        "expanded": expanded,
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
        extra_sheets={"Users": rows},
    )

    filename = f"Concur_Users_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

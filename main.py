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

# Existing project modules (must exist in your repo/package)
from auth.azure_ad import get_current_user, get_azure_ad_config_status
from auth.concur_oauth import ConcurOAuthClient
from services.identity_service import get_secret

# ======================================================
# ENV + KEY VAULT HELPERS
# ======================================================


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    if v is not None and isinstance(v, str):
        v = v.strip()
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
        or "https://us.api.concursolutions.com"
    )


# ======================================================
# CONCUR OAUTH CLIENT (Service-level)
# ======================================================

_oauth_client: Optional[ConcurOAuthClient] = None


def get_oauth_client() -> ConcurOAuthClient:
    global _oauth_client
    if _oauth_client is not None:
        return _oauth_client

    # Token URL is tenant dependent; prefer KV/ENV, otherwise default to US2
    token_url = (
        kv("concur-token-url")
        or env("CONCUR_TOKEN_URL")
        or "https://us2.api.concursolutions.com/oauth2/v0/token"
    )
    client_id = kv("concur-client-id") or env("CONCUR_CLIENT_ID")
    client_secret = kv("concur-client-secret") or env("CONCUR_CLIENT_SECRET")
    refresh_token = kv("concur-refresh-token") or env("CONCUR_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "missing_concur_oauth_config",
                "client_id_set": bool(client_id),
                "client_secret_set": bool(client_secret),
                "refresh_token_set": bool(refresh_token),
            },
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
    where: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    try:
        resp = requests.get(
            url, headers=concur_headers(), params=params, timeout=timeout
        )
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

allowed_origin = env("SP_ORIGIN", "https://covantagenew.sharepoint.com")
origins = [allowed_origin] if allowed_origin else ["*"]

app = FastAPI(title="SAP Concur Employee Profile Viewer API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[allowed_origin],
    allow_credentials=False,  # bearer token auth doesn't need cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ======================================================
# MODELS
# ======================================================


class CardSearchRequest(BaseModel):
    dateFrom: str
    dateTo: str
    pageSize: int = 200


# ======================================================
# IDENTITY HELPERS (tenant-safe attributes fallback)
# ======================================================

ATTRS_WITH_CONCUR_EXT = (
    "id,userName,active,displayName,name,preferredLanguage,"
    "emails,phoneNumbers,timezone,locale,"
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
    "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
)

ATTRS_NO_CONCUR_EXT = (
    "id,userName,active,displayName,name,preferredLanguage,"
    "emails,phoneNumbers,timezone,locale,"
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
        if isinstance(
            u.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"), dict
        )
        else {}
    )
    name = u.get("name") or {}
    return {
        "id": u.get("id"),
        "userName": u.get("userName"),
        "displayName": u.get("displayName"),
        "active": u.get("active"),
        "email": _extract_primary_email(u),
        "employeeNumber": enterprise.get("employeeNumber"),
        "department": enterprise.get("department"),
        "company": enterprise.get("company"),
        "costCenter": enterprise.get("costCenter"),
        "firstName": name.get("givenName"),
        "lastName": name.get("familyName"),
    }


def _identity_list_users_once(
    attributes: str, start_index: int, count: int
) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users"
    params = {"startIndex": start_index, "count": count, "attributes": attributes}
    return _concur_get_json(url, where="identity_list_users", params=params)


def _parse_unrecognized_attr(error_text: str) -> Optional[str]:
    if not error_text:
        return None
    marker = "unrecognized attributes:"
    low = error_text.lower()
    idx = low.find(marker)
    if idx == -1:
        return None
    frag = error_text[idx + len(marker) :].strip()
    frag = frag.split(",")[0].strip().strip(".").strip()
    return frag or None


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
    users: List[Dict[str, Any]] = []
    start_index = 1
    pages = 0
    attrs_used = attributes
    fixes = 0

    while pages < max_pages:
        pages += 1
        try:
            payload = _identity_list_users_once(
                attrs_used, start_index=start_index, count=count
            )
        except HTTPException as he:
            detail = he.detail if isinstance(he.detail, dict) else {}
            resp_text = str(detail.get("response") or "")
            status = detail.get("concur_status")
            if status == 400 and fixes < max_attr_fixes:
                bad_attr = _parse_unrecognized_attr(resp_text)
                if bad_attr:
                    attrs_used = _remove_attribute_from_list(attrs_used, bad_attr)
                    fixes += 1
                    continue
            raise

        resources = payload.get("Resources") or []
        if isinstance(resources, list):
            users.extend([r for r in resources if isinstance(r, dict)])

        total_results = payload.get("totalResults")
        items_per_page = payload.get("itemsPerPage")
        if not isinstance(items_per_page, int):
            items_per_page = len(resources)

        if isinstance(total_results, int):
            if start_index - 1 + items_per_page >= total_results:
                break

        if not resources:
            break

        start_index += items_per_page

    return users, attrs_used


# ======================================================
# SECURITY (Aad delegated access)
# ======================================================


def require_user(user=Depends(get_current_user)):
    return user


# ======================================================
# DEBUG / HEALTH
# ======================================================


@app.get("/build")
def build():
    return {
        "ok": True,
        "loaded_from": __file__,
        "python": sys.version,
        "time": datetime.utcnow().isoformat() + "Z",
        "run_from_package": os.getenv("WEBSITE_RUN_FROM_PACKAGE"),
        "port": os.getenv("PORT"),
    }


@app.get("/kv-test")
def kv_test():
    client_id = kv("concur-client-id")
    base = kv("concur-api-base-url")
    return {
        "status": "ok",
        "client_id_exists": bool(client_id),
        "base_url_set": bool(base),
    }


@app.get("/api/tools/token-command")
def token_command():
    api_app_id = env("AZURE_API_APP_ID") or kv("azure-api-app-id") or ""
    aud = f"api://{api_app_id}" if api_app_id else "api://<YOUR_FASTAPI_APP_ID>"
    return {
        "bash": f'az account get-access-token --resource "{aud}" --query accessToken -o tsv',
        "powershell": f'(az account get-access-token --resource "{aud}" --query accessToken -o tsv)',
        "note": "Paste the token into Swagger (/docs) using the Authorize button: Bearer <TOKEN>",
    }


@app.get("/api/whoami")
def whoami(user=Depends(require_user)):
    return {"ok": True, "user": user}


@app.get("/api/config-status")
def config_status():
    return {"ok": True, "azure_ad": get_azure_ad_config_status()}


# ======================================================
# CONCUR AUTH TEST (FIXED INDENTATION)
# ======================================================


@app.get("/api/concur/auth-test")
def concur_auth_test(user=Depends(require_user)):
    """
    Confirms Concur OAuth refresh flow works and outbound calls succeed.
    """
    base_url = concur_base_url()

    # ✅ This is the exact block that was broken on your server previously.
    # Keep indentation exactly like this.
    try:
        token_url = kv("concur-token-url")
    except Exception:
        token_url = f"{base_url}/oauth2/v0/token"

    # Sanity: instantiate ConcurOAuthClient using the same config your app uses
    _ = get_oauth_client()

    # Try a simple Identity call to prove the access token works
    url = f"{base_url}/profile/identity/v4.1/Users"
    params = {"startIndex": 1, "count": 1, "attributes": ATTRS_NO_CONCUR_EXT}

    try:
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "concur_auth_test",
                "error": "request_failed",
                "message": str(ex),
            },
        )

    return {
        "ok": resp.ok,
        "status_code": resp.status_code,
        "token_url_used": token_url,
        "base_url": base_url,
        "sample": (resp.json().get("Resources") if resp.ok and resp.content else None),
        "error_body": (resp.text[:1000] if not resp.ok else None),
    }


# ======================================================
# API: USERS LIST (Identity list)
# ======================================================


@app.get("/api/users")
def list_users(
    q: Optional[str] = Query(
        default=None, description="Search displayName/email/userName"
    ),
    take: int = Query(default=50, ge=1, le=500),
    user=Depends(require_user),
):
    try:
        users, attrs_used = _identity_list_users_paged(
            attributes=ATTRS_WITH_CONCUR_EXT, count=200
        )
        rows = [_to_grid_row_identity(u) for u in users]
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if (r.get("displayName") or "").lower().find(ql) >= 0
                or (r.get("email") or "").lower().find(ql) >= 0
                or (r.get("userName") or "").lower().find(ql) >= 0
            ]
        return {
            "ok": True,
            "count": len(rows[:take]),
            "items": rows[:take],
            "attributesUsed": attrs_used,
        }
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, dict) else {}
        resp_text = str(detail.get("response") or "")
        if detail.get("concur_status") == 400 and "unrecognized" in resp_text.lower():
            users, attrs_used = _identity_list_users_paged(
                attributes=ATTRS_NO_CONCUR_EXT, count=200
            )
            rows = [_to_grid_row_identity(u) for u in users]
            if q:
                ql = q.lower()
                rows = [
                    r
                    for r in rows
                    if (r.get("displayName") or "").lower().find(ql) >= 0
                    or (r.get("email") or "").lower().find(ql) >= 0
                    or (r.get("userName") or "").lower().find(ql) >= 0
                ]
            return {
                "ok": True,
                "count": len(rows[:take]),
                "items": rows[:take],
                "attributesUsed": attrs_used,
            }
        raise


# ======================================================
# SINGLE USER DETAIL (Identity)
# ======================================================


@app.get("/api/users/{user_id}")
def get_user(user_id: str, user=Depends(require_user)):
    return {"ok": True, "identity": get_user_detail_identity(user_id)}


def get_user_detail_identity(user_id: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/profile/identity/v4.1/Users/{user_id}"

    def _do_get(attributes: str) -> requests.Response:
        return requests.get(
            url, headers=concur_headers(), params={"attributes": attributes}, timeout=30
        )

    attrs1 = ATTRS_WITH_CONCUR_EXT
    try:
        resp = _do_get(attrs1)
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "user_detail_identity",
                "error": "request_failed",
                "message": str(ex),
                "url": url,
                "params": {"attributes": attrs1},
            },
        )

    if resp.ok:
        return resp.json() if resp.content else {}

    if resp.status_code == 400:
        body = (resp.text or "").lower()
        if "unrecognized" in body or "bad_query" in body:
            attrs2 = ATTRS_NO_CONCUR_EXT
            resp2 = _do_get(attrs2)
            if resp2.ok:
                return resp2.json() if resp2.content else {}
            raise HTTPException(
                status_code=502,
                detail={
                    "where": "user_detail_identity_retry",
                    "error": "concur_error",
                    "concur_status": resp2.status_code,
                    "response": (resp2.text or "")[:2000],
                },
            )

    raise HTTPException(
        status_code=502,
        detail={
            "where": "user_detail_identity",
            "error": "concur_error",
            "concur_status": resp.status_code,
            "response": (resp.text or "")[:2000],
        },
    )


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


# ======================================================
# LIST v4 helpers (resolve org units + custom fields)
# ======================================================


def _list_get_item(list_id: str, item_id: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/list/v4/lists/{list_id}/items/{item_id}"
    return _concur_get_json(url, where="list_get_item")


def _list_search(list_id: str, *, value: str) -> Dict[str, Any]:
    base = concur_base_url()
    url = f"{base}/list/v4/lists/{list_id}/items"
    params = {"searchTerm": value, "limit": 50}
    return _concur_get_json(url, where="list_search", params=params)


def _merge_dicts(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def _extract_org_and_custom_from_spend(
    spend: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    org_units: Dict[str, Any] = {}
    custom: Dict[str, Any] = {}

    for i in range(1, 7):
        key = f"orgUnit{i}"
        val = spend.get(key)
        if val is not None:
            org_units[key] = val

    for i in range(1, 23):
        key = f"custom{i}"
        val = spend.get(key)
        if val is not None:
            custom[key] = val

    return org_units, custom


def _extract_identity_name(
    identity: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    name = identity.get("name") or {}
    return name.get("givenName"), name.get("familyName")


def _derive(
    identity: Dict[str, Any], spend: Dict[str, Any], travel: Dict[str, Any]
) -> Dict[str, Any]:
    first, last = _extract_identity_name(identity)
    ent = (
        identity.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User") or {}
    )
    return {
        "id": identity.get("id"),
        "userName": identity.get("userName"),
        "displayName": identity.get("displayName"),
        "active": identity.get("active"),
        "email": _extract_primary_email(identity),
        "firstName": first,
        "lastName": last,
        "employeeNumber": ent.get("employeeNumber"),
        "department": ent.get("department"),
        "company": ent.get("company"),
        "costCenter": ent.get("costCenter"),
        "spend": {
            "roles": spend.get("roles"),
            "approvers": spend.get("approvers"),
            "delegates": spend.get("delegates"),
        },
        "travel": {"ruleClass": travel.get("ruleClass")},
    }


def _expand_list_backed_fields(
    *,
    org_units: Dict[str, Any],
    custom: Dict[str, Any],
    expand_limit: int = 50,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    resolved: Dict[str, Any] = {"orgUnits": {}, "custom": {}}
    expanded_raw: Dict[str, Any] = {"listItems": []}
    count = 0

    def _resolve(v: Any) -> Any:
        if isinstance(v, dict):
            list_id = v.get("listId") or v.get("list_id")
            item_id = v.get("itemId") or v.get("item_id") or v.get("id")
            code = v.get("code") or v.get("value")
            if list_id and item_id:
                item = _list_get_item(str(list_id), str(item_id))
                expanded_raw["listItems"].append(item)
                return {
                    "listId": list_id,
                    "itemId": item_id,
                    "code": code,
                    "name": item.get("name") or item.get("value") or item.get("code"),
                }
            if list_id and code:
                res = _list_search(str(list_id), value=str(code))
                expanded_raw["listItems"].append(res)
                items = res.get("Items") or res.get("items") or []
                if isinstance(items, list) and items:
                    best = items[0]
                    return {
                        "listId": list_id,
                        "code": code,
                        "name": best.get("name")
                        or best.get("value")
                        or best.get("code"),
                    }
            return v
        return v

    for k, v in org_units.items():
        if count >= expand_limit:
            break
        resolved["orgUnits"][k] = _resolve(v)
        count += 1

    for k, v in custom.items():
        if count >= expand_limit:
            break
        resolved["custom"][k] = _resolve(v)
        count += 1

    return resolved, expanded_raw


# ======================================================
# PRIMARY ENDPOINT: FULL PROFILE
# ======================================================


@app.get("/api/users/{user_id}/full")
def get_user_full(
    user_id: str,
    expand: Optional[List[str]] = Query(default=None, description="expand=listItems"),
    expandLimit: int = Query(default=50, ge=0, le=200),
    user=Depends(require_user),
):
    identity = get_user_detail_identity(user_id)
    spend = get_user_detail_spend(user_id)
    travel = get_user_detail_travel(user_id)

    combined_scim = _merge_dicts(identity, {})
    derived = _derive(identity, spend, travel)

    org_units, custom = _extract_org_and_custom_from_spend(spend)

    resolved = {}
    expanded_raw = {}
    if expand and "listItems" in expand:
        resolved, expanded_raw = _expand_list_backed_fields(
            org_units=org_units, custom=custom, expand_limit=expandLimit
        )

    return {
        "ok": True,
        "sources": {"identity": identity, "spend": spend, "travel": travel},
        "combined": {"scim": combined_scim, "_derived": {"resolved": derived}},
        "orgUnits": org_units,
        "custom": custom,
        "resolved": resolved,
        "expanded": expanded_raw,
    }


# ======================================================
# OPTIONAL: Cards endpoint
# ======================================================


@app.post("/api/cards/unassigned/search")
def cards_unassigned_search(body: CardSearchRequest, user=Depends(require_user)):
    base = concur_base_url()
    url = f"{base}/card/v4/charges/unassigned/search"
    payload = {
        "dateFrom": body.dateFrom,
        "dateTo": body.dateTo,
        "pageSize": body.pageSize,
    }
    try:
        resp = requests.post(
            url,
            headers={**concur_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
    except Exception as ex:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "cards_unassigned_search",
                "error": "request_failed",
                "message": str(ex),
                "url": url,
            },
        )

    if not resp.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "where": "cards_unassigned_search",
                "error": "concur_error",
                "concur_status": resp.status_code,
                "url": url,
                "payload": payload,
                "response": (resp.text or "")[:2000],
            },
        )

    return resp.json() if resp.content else {}


# ======================================================
# DOWNLOAD HELPER
# ======================================================


def _json_to_bytes(data: Any) -> bytes:
    import json

    return (json.dumps(data, indent=2, default=str) + "\n").encode("utf-8")


@app.get("/api/users/{user_id}/full/download")
def download_user_full(
    user_id: str,
    expand: Optional[List[str]] = Query(default=None),
    expandLimit: int = Query(default=50, ge=0, le=200),
    user=Depends(require_user),
):
    payload = get_user_full(
        user_id=user_id, expand=expand, expandLimit=expandLimit, user=user
    )
    filename = f"user_full_{user_id}.json"
    bio = BytesIO(_json_to_bytes(payload))
    return StreamingResponse(
        bio,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}

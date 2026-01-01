# main.py (FULL REWRITE - DROP-IN REPLACEMENT)

print("#### LOADED MAIN FROM:", __file__)
import os, sys
from datetime import datetime
from io import BytesIO
from typing import Optional, List, Dict, Any, Tuple

import requests
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Azure AD authentication for SharePoint integration
from auth.azure_ad import get_current_user, get_azure_ad_config_status

# Concur OAuth refresh-token client
from auth.concur_oauth import ConcurOAuthClient

# Key Vault + Excel export
from services.identity_service import get_secret, keyvault_status
from services.excel_export import export_accruals_to_excel


BUILD_FINGERPRINT = os.getenv("SCM_COMMIT_ID") or os.getenv("WEBSITE_DEPLOYMENT_ID") or "unknown"
print("RUN_FROM_PACKAGE =", os.getenv("WEBSITE_RUN_FROM_PACKAGE"))
print("PWD =", os.getcwd())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ======================================================
# FASTAPI APP
# ======================================================

app = FastAPI(title="Concur Accruals API")

# ======================================================
# HELPERS
# ======================================================

def env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return fallback
    return v

def kv(name: str, fallback: Optional[str] = None) -> Optional[str]:
    """
    Read a secret from Key Vault if available; otherwise return fallback.
    Note: services.identity_service.get_secret(name) does not accept a fallback parameter.
    """
    try:
        return get_secret(name)
    except Exception:
        return fallback

def concur_base_url() -> str:
    """
    Concur API base URL.
    Prefer Key Vault secret name: 'concur-api-base-url'
    Fallback to env names: CONCUR_API_BASE_URL then CONCUR_BASE_URL then default.
    """
    return (
        kv("concur-api-base-url")
        or env("CONCUR_API_BASE_URL")
        or env("CONCUR_BASE_URL")
        or "https://www.concursolutions.com"
    ).rstrip("/")

# ======================================================
# CONCUR OAUTH CLIENT (cached in-process)
# ======================================================

_oauth_client: Optional[ConcurOAuthClient] = None

def get_oauth_client() -> ConcurOAuthClient:
    """
    Build and cache a ConcurOAuthClient once per process.
    Prefer Key Vault secret names (hyphenated):
      - concur-token-url
      - concur-client-id
      - concur-client-secret
      - concur-refresh-token
    Fallback to env vars:
      - CONCUR_TOKEN_URL, CONCUR_CLIENT_ID, CONCUR_CLIENT_SECRET, CONCUR_REFRESH_TOKEN
    """
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
            detail=f"Missing Concur OAuth config (KV or env). Missing: {missing}"
        )

    _oauth_client = ConcurOAuthClient(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    return _oauth_client

def concur_headers() -> Dict[str, str]:
    """
    Always uses a valid Concur access token (refresh-token driven, cached in-process).
    """
    oauth = get_oauth_client()
    token = oauth.get_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

def concur_auth_test() -> Dict[str, Any]:
    """
    Smoke test for Concur authentication:
    - Forces OAuth token retrieval/refresh
    - Calls a lightweight Concur endpoint (Identity Users count=1)
    """
    headers = concur_headers()
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    resp = requests.get(url, headers=headers, params={"count": 1}, timeout=30)

    if resp.ok:
        try:
            j = resp.json() or {}
            resources = j.get("Resources", []) if isinstance(j, dict) else []
            return {"status_code": resp.status_code, "ok": True, "sample": resources[:1]}
        except Exception:
            return {"status_code": resp.status_code, "ok": True, "sample": resp.text[:500]}
    else:
        return {"status_code": resp.status_code, "ok": False, "error": resp.text[:1000]}

# ======================================================
# CORS
# ======================================================

allowed_origin = env("SP_ORIGIN", "")  # e.g. https://<tenant>.sharepoint.com
origins = [allowed_origin] if allowed_origin else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================================
# HEALTH / DEBUG
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
    st = keyvault_status()
    return {"status": "ok", "keyvault": st}

@app.get("/auth/config-status")
def auth_config_status():
    return get_azure_ad_config_status()

@app.get("/api/concur/auth-test")
def api_concur_auth_test():
    """
    Test Concur OAuth + outbound API access.
    Appears in /docs.
    """
    try:
        return concur_auth_test()
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))

# ======================================================
# REQUEST MODELS
# ======================================================

class AccrualsSearchRequest(BaseModel):
    orgUnit1: Optional[str] = None
    orgUnit2: Optional[str] = None
    orgUnit3: Optional[str] = None
    orgUnit4: Optional[str] = None
    orgUnit5: Optional[str] = None
    orgUnit6: Optional[str] = None
    custom21: Optional[str] = None

class UnassignedCardsRequest(BaseModel):
    transactionDateFrom: str  # YYYY-MM-DD
    transactionDateTo: str    # YYYY-MM-DD
    dateType: str = "TRANSACTION"  # TRANSACTION | POSTED | BILLING (metadata/export)
    pageSize: int = 200  # capped in code

# ======================================================
# IDENTITY v4.1 (NO FILTER - TENANT SAFE)
# ======================================================

def _identity_list_users_paged(
    *,
    attributes: str,
    count: int = 200,
    max_pages: int = 200,
) -> List[Dict[str, Any]]:
    """
    List users using Identity v4.1 WITHOUT SCIM filter=.
    Paginates via startIndex/count until exhausted or max_pages reached.
    """
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    all_users: List[Dict[str, Any]] = []

    start_index = 1
    page = 0

    while page < max_pages:
        params: Dict[str, Any] = {
            "attributes": attributes,
            "startIndex": start_index,
            "count": count,
        }
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        resp.raise_for_status()

        payload = resp.json() or {}
        resources = payload.get("Resources") or []
        if not isinstance(resources, list) or not resources:
            break

        all_users.extend(resources)

        # SCIM paging fields (Identity v4.1 typically returns these)
        total_results = int(payload.get("totalResults") or 0)
        items_per_page = int(payload.get("itemsPerPage") or len(resources) or 0)

        # Advance start index
        start_index += max(items_per_page, 1)
        page += 1

        # Stop if server told us the total and we've passed it
        if total_results and start_index > total_results:
            break

        # Safety: if server doesn't return stable paging fields, stop on short page
        if len(resources) < count:
            break

    return all_users

def _lower(s: Optional[str]) -> str:
    return (s or "").strip().lower()

def _extract_primary_email(user: Dict[str, Any]) -> Optional[str]:
    emails = user.get("emails") or []
    if isinstance(emails, list):
        for e in emails:
            if isinstance(e, dict) and e.get("value"):
                return str(e.get("value"))
    return None

def get_concur_user_id_for_upn(upn_or_email: str) -> str:
    """
    Resolve Entra UPN/email to Concur userId WITHOUT SCIM filter= (tenant-safe).
    Uses list+scan with minimal attributes.
    """
    if not upn_or_email:
        raise HTTPException(status_code=400, detail="Missing user identity (UPN/email).")

    needle = _lower(upn_or_email)

    attrs = "id,userName,emails.value,active"
    users = _identity_list_users_paged(attributes=attrs, count=200, max_pages=200)

    # First pass: match userName
    for u in users:
        if _lower(u.get("userName")) == needle:
            user_id = u.get("id")
            if user_id:
                return str(user_id)

    # Second pass: match any email
    for u in users:
        email = _extract_primary_email(u)
        if _lower(email) == needle:
            user_id = u.get("id")
            if user_id:
                return str(user_id)

    raise HTTPException(status_code=404, detail=f"Concur user not found for {upn_or_email}")

def _matches_org_filters(user: Dict[str, Any], req: Any) -> bool:
    """
    Local filtering (since SCIM filter= breaks on this tenant).
    Checks orgUnit1-6 and custom21 against the Concur user extension if present.
    """
    ext = user.get("urn:ietf:params:scim:schemas:extension:concur:2.0:User") or {}
    if not isinstance(ext, dict):
        ext = {}

    # Only enforce fields that were provided
    for k in ("orgUnit1","orgUnit2","orgUnit3","orgUnit4","orgUnit5","orgUnit6","custom21"):
        want = getattr(req, k, None)
        if want is not None and str(want).strip() != "":
            have = ext.get(k)
            if str(have).strip() != str(want).strip():
                return False

    return True

def get_users(req: Any) -> List[Dict[str, Any]]:
    """
    Legacy helper used by /api/accruals/search.
    Returns users using list+scan and optional local filtering.
    """
    attrs = (
        "id,userName,displayName,active,emails.value,"
        "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
        "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
    )
    users = _identity_list_users_paged(attributes=attrs, count=200, max_pages=200)

    # Apply local org filter if any fields were provided
    filtered = [u for u in users if _matches_org_filters(u, req)]
    return filtered

# ======================================================
# USERS ROUTES (FOR SHAREPOINT UI)
# ======================================================

def _to_grid_row(u: Dict[str, Any]) -> Dict[str, Any]:
    enterprise = u.get("urn:ietf:params:scim:schemas:extension:enterprise:2.0:User") or {}
    concur_ext = u.get("urn:ietf:params:scim:schemas:extension:concur:2.0:User") or {}
    if not isinstance(enterprise, dict):
        enterprise = {}
    if not isinstance(concur_ext, dict):
        concur_ext = {}

    return {
        "id": u.get("id"),
        "displayName": u.get("displayName"),
        "userName": u.get("userName"),
        "email": _extract_primary_email(u),
        "active": u.get("active"),
        "employeeNumber": enterprise.get("employeeNumber"),
        "orgUnit1": concur_ext.get("orgUnit1"),
        "orgUnit2": concur_ext.get("orgUnit2"),
        "orgUnit3": concur_ext.get("orgUnit3"),
        "orgUnit4": concur_ext.get("orgUnit4"),
        "orgUnit5": concur_ext.get("orgUnit5"),
        "orgUnit6": concur_ext.get("orgUnit6"),
        "custom21": concur_ext.get("custom21"),
    }

@app.get("/api/users")
def api_users_list(
    take: int = Query(500, ge=1, le=5000),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    SharePoint button: "Load Users"
    Returns a grid-friendly list of users (no filter=, paged).
    """
    attrs = (
        "id,userName,displayName,active,emails.value,"
        "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User,"
        "urn:ietf:params:scim:schemas:extension:concur:2.0:User"
    )
    users = _identity_list_users_paged(attributes=attrs, count=200, max_pages=200)
    users = users[:take]

    requested_by = current_user.get("upn") or current_user.get("preferred_username") or current_user.get("email")

    return {
        "meta": {
            "requestedBy": requested_by,
            "returned": len(users),
        },
        "users": [_to_grid_row(u) for u in users],
    }

@app.get("/api/users/{user_id}")
def api_user_detail(
    user_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    SharePoint row button: "View Details"
    Returns the full SCIM record for a userId.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    url = f"{concur_base_url()}/profile/identity/v4.1/Users/{user_id}"
    resp = requests.get(url, headers=concur_headers(), timeout=30)
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="User not found in Concur Identity v4.1")
    resp.raise_for_status()
    return resp.json() or {}

# ======================================================
# EXPENSE REPORTS v4 (legacy for /api/accruals/search)
# ======================================================

def get_expense_reports(user_id: str) -> List[Dict]:
    url = f"{concur_base_url()}/expensereports/v4/users/{user_id}/reports"
    resp = requests.get(url, headers=concur_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", []) or []

def filter_unsubmitted_reports(reports: List[Dict]) -> List[Dict]:
    results = []
    for r in reports:
        if r.get("submitDate"):
            continue
        results.append(
            {
                "reportId": r.get("reportId") or r.get("id"),
                "reportName": r.get("name"),
                "reportPurpose": r.get("purpose"),
                "reportSubmissionDate": r.get("submitDate"),
                "paymentStatusId": (r.get("paymentStatus") or {}).get("id"),
                "totalAmount": (r.get("totalAmount") or {}).get("value"),
            }
        )
    return results

# ======================================================
# CARDS v4
# ======================================================

def get_card_transactions(user_id: str, date_from: str, date_to: str, status: Optional[str] = None, page_size: int = 200) -> List[Dict]:
    """
    Fetch card transactions for a single user within a date window.

    Defensive pagination:
    - Iterates page/pageSize
    - Stops if first transaction repeats (tenant ignores paging) to avoid infinite loop
    """
    url = f"{concur_base_url()}/cards/v4/users/{user_id}/transactions"

    if page_size < 1:
        page_size = 200
    if page_size > 500:
        page_size = 500

    page = 1
    seen_first_id: Optional[str] = None
    all_items: List[Dict] = []

    while True:
        params: Dict[str, Any] = {
            "transactionDateFrom": date_from,
            "transactionDateTo": date_to,
            "page": page,
            "pageSize": page_size,
        }
        if status:
            params["status"] = status

        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json() or {}

        items = (
            payload.get("Items")
            or payload.get("items")
            or payload.get("Transactions")
            or payload.get("transactions")
            or []
        )
        if not isinstance(items, list):
            raise HTTPException(status_code=500, detail="Unexpected Cards response shape (transactions is not a list).")

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
        if page > 100:  # hard guard
            break

    return all_items

def filter_unassigned_cards(transactions: List[Dict]) -> List[Dict]:
    """
    A transaction is considered assigned if it has expenseId or reportId.
    Return a UI-friendly shape used by SPFx + Excel export.
    """
    results: List[Dict] = []
    for t in transactions:
        if t.get("expenseId") or t.get("reportId"):
            continue

        account = t.get("account") or {}
        payment_type = account.get("paymentType") or {}
        posted = t.get("postedAmount") or {}
        trans_amt = t.get("transactionAmount") or {}
        billing_amt = t.get("billingAmount") or {}

        results.append(
            {
                "transactionId": t.get("transactionId") or t.get("id"),

                "cardProgramId": payment_type.get("id"),
                "cardProgramName": payment_type.get("name"),
                "accountKey": account.get("lastSegment") or account.get("accountKey"),
                "lastFourDigits": account.get("lastFourDigits"),

                "transactionDate": t.get("transactionDate"),
                "postedDate": t.get("postedDate"),
                "billingDate": t.get("billingDate"),

                "merchantName": t.get("merchantName") or t.get("merchant"),
                "description": t.get("description") or t.get("transactionDescription"),

                "postedAmount": posted.get("value"),
                "postedCurrencyCode": posted.get("currencyCode"),

                "transactionAmount": trans_amt.get("value") if isinstance(trans_amt, dict) else trans_amt,
                "transactionCurrencyCode": trans_amt.get("currencyCode") if isinstance(trans_amt, dict) else None,

                "billingAmount": billing_amt.get("value") if isinstance(billing_amt, dict) else billing_amt,
                "billingCurrencyCode": billing_amt.get("currencyCode") if isinstance(billing_amt, dict) else None,
            }
        )
    return results

# ======================================================
# UNASSIGNED CARDS (CURRENT USER) - SharePoint UI-first
# ======================================================

@app.post("/api/cards/unassigned/search")
def api_cards_unassigned_search(req: UnassignedCardsRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    upn = current_user.get("upn") or current_user.get("preferred_username") or current_user.get("email")
    if not upn:
        raise HTTPException(status_code=400, detail="Cannot determine current user identity (missing upn/preferred_username/email).")

    # IMPORTANT: tenant-safe resolution (no filter=)
    concur_user_id = get_concur_user_id_for_upn(upn)

    txns = get_card_transactions(
        concur_user_id,
        req.transactionDateFrom,
        req.transactionDateTo,
        status="UN",
        page_size=req.pageSize,
    )

    unassigned = filter_unassigned_cards(txns)

    return {
        "summary": {
            "upn": upn,
            "concurUserId": concur_user_id,
            "dateFrom": req.transactionDateFrom,
            "dateTo": req.transactionDateTo,
            "unassignedCardCount": len(unassigned),
        },
        "transactions": unassigned,
    }

@app.post("/api/cards/unassigned/export")
def api_cards_unassigned_export(req: UnassignedCardsRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    data = api_cards_unassigned_search(req, current_user=current_user)
    unassigned = data["transactions"]

    excel_bytes = export_accruals_to_excel(
        unsubmitted_reports=[],
        unassigned_cards=unassigned,
        card_totals_by_program=None,
        card_totals_by_user=None,
        meta={"dateFrom": req.transactionDateFrom, "dateTo": req.transactionDateTo, "dateType": req.dateType},
    )

    filename = f"Concur_Unassigned_Cards_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ======================================================
# LEGACY ENDPOINT (kept for now)
# ======================================================

@app.post("/api/accruals/search")
def api_accruals_search(req: AccrualsSearchRequest):
    """
    Legacy endpoint kept as-is.
    Note: This does org-wide work and can be slow.
    Prefer /api/users and /api/users/{id} for SharePoint user UI.
    """
    users = get_users(req)

    unsubmitted_reports_all: List[Dict] = []
    unassigned_cards_all: List[Dict] = []

    for u in users:
        user_id = u.get("id")
        if not user_id:
            continue

        reports = get_expense_reports(user_id)
        unsubmitted_reports_all.extend(filter_unsubmitted_reports(reports))

        # Legacy huge window (kept for backwards compatibility only)
        txns = get_card_transactions(user_id, "2000-01-01", datetime.now().strftime("%Y-%m-%d"))
        unassigned_cards_all.extend(filter_unassigned_cards(txns))

    excel_bytes = export_accruals_to_excel(unsubmitted_reports_all, unassigned_cards_all)
    filename = f"Concur_Accruals_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        BytesIO(excel_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

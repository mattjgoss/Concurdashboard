print("#### LOADED MAIN FROM:", __file__)
import os, sys
import time
from collections import defaultdict
from datetime import datetime, date
from io import BytesIO
from typing import Optional, List, Dict, Any

from fastapi.middleware.cors import CORSMiddleware
import requests
from dateutil.parser import isoparse
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Azure AD authentication for SharePoint integration
from auth.azure_ad import get_current_user, get_azure_ad_config_status

# Concur OAuth refresh-token client
from auth.concur_oauth import ConcurOAuthClient

# Local helpers
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
    Note: services.identity_service.get_secret(name) has no fallback argument.
    """
    try:
        return get_secret(name)
    except Exception:
        return fallback

def concur_base_url() -> str:
    # Concur API base (some tenants may use region-specific domains)
    return (kv("CONCUR_BASE_URL", env("CONCUR_BASE_URL", "https://www.concursolutions.com")) or "").rstrip("/")

# ======================================================
# CONCUR OAUTH CLIENT (cached in-process)
# ======================================================

_oauth_client: Optional[ConcurOAuthClient] = None

def get_oauth_client() -> ConcurOAuthClient:
    """
    Build and cache a ConcurOAuthClient once per process.
    Reads config from Key Vault first, then env.
    """
    global _oauth_client
    if _oauth_client is not None:
        return _oauth_client

    token_url = kv("CONCUR_TOKEN_URL", env("CONCUR_TOKEN_URL"))
    client_id = kv("CONCUR_CLIENT_ID", env("CONCUR_CLIENT_ID"))
    client_secret = kv("CONCUR_CLIENT_SECRET", env("CONCUR_CLIENT_SECRET"))
    refresh_token = kv("CONCUR_REFRESH_TOKEN", env("CONCUR_REFRESH_TOKEN"))

    if not token_url or not client_id or not client_secret or not refresh_token:
        raise HTTPException(
            status_code=500,
            detail="Missing Concur OAuth config. Need CONCUR_TOKEN_URL/CONCUR_CLIENT_ID/CONCUR_CLIENT_SECRET/CONCUR_REFRESH_TOKEN (KV or env)."
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
    dateType: str = "TRANSACTION"  # TRANSACTION | POSTED | BILLING (mainly for metadata/export)
    pageSize: int = 200  # capped in code

# ======================================================
# IDENTITY v4.1 (USER RESOLUTION)
# ======================================================

def build_identity_filter(req: Any) -> Optional[str]:
    parts = []
    if getattr(req, "orgUnit1", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit1 eq "{req.orgUnit1}"')
    if getattr(req, "orgUnit2", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit2 eq "{req.orgUnit2}"')
    if getattr(req, "orgUnit3", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit3 eq "{req.orgUnit3}"')
    if getattr(req, "orgUnit4", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit4 eq "{req.orgUnit4}"')
    if getattr(req, "orgUnit5", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit5 eq "{req.orgUnit5}"')
    if getattr(req, "orgUnit6", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:orgUnit6 eq "{req.orgUnit6}"')
    if getattr(req, "custom21", None):
        parts.append(f'urn:ietf:params:scim:schemas:extension:concur:2.0:User:custom21 eq "{req.custom21}"')

    if not parts:
        return None
    return " and ".join(parts)

def get_users(req: Any) -> List[Dict]:
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    params: Dict[str, Any] = {"startIndex": 1, "count": 100}
    flt = build_identity_filter(req)
    if flt:
        params["filter"] = flt

    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("Resources", []) or []

def get_concur_user_id_for_upn(upn_or_email: str) -> str:
    """
    Resolve the current Entra user (UPN/email) to a single Concur userId (UUID)
    using the Identity v4.1 SCIM endpoint.
    """
    if not upn_or_email:
        raise HTTPException(status_code=400, detail="Missing user identity (UPN/email).")

    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    for flt in (f'userName eq "{upn_or_email}"', f'emails.value eq "{upn_or_email}"'):
        params = {"filter": flt, "startIndex": 1, "count": 1}
        resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
        resp.raise_for_status()
        resources = resp.json().get("Resources", []) or []
        if resources and isinstance(resources, list):
            user_id = resources[0].get("id")
            if user_id:
                return user_id

    raise HTTPException(status_code=404, detail=f"Concur user not found for {upn_or_email}")

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
# EXISTING LEGACY ENDPOINT (kept for now)
# ======================================================

@app.post("/api/accruals/search")
def api_accruals_search(req: AccrualsSearchRequest):
    """
    Legacy endpoint kept as-is. Note: This does org-wide work and can be slow.
    Prefer /api/cards/unassigned/search for SharePoint UI interactions.
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

print("#### LOADED MAIN FROM:", __file__)

import os
import time
from collections import defaultdict
from datetime import datetime, date
from io import BytesIO
from typing import Optional, List, Dict

import requests
from dateutil.parser import isoparse
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel

from services.identity_service import get_secret, keyvault_status

BUILD_FINGERPRINT = os.getenv("SCM_COMMIT_ID") or os.getenv("WEBSITE_DEPLOYMENT_ID") or "unknown"
print("RUN_FROM_PACKAGE =", os.getenv("WEBSITE_RUN_FROM_PACKAGE"))
print("PWD =", os.getcwd())

# ======================================================
# FASTAPI APP
# ======================================================

app = FastAPI(title="Concur Accruals API")

# ======================================================
# LOCAL FILES
# ======================================================

TEMPLATE_PATH = "reports/accrual report.xlsx"

# ======================================================
# KEY VAULT ACCESS (thin wrapper)
# ======================================================

def kv(name: str) -> str:
    """
    Fetch a secret from Key Vault (via services.identity_service.get_secret).
    Caching + client init are handled inside identity_service.py.
    """
    return get_secret(name)

def concur_base_url() -> str:
    # Example secret value: https://us2.api.concursolutions.com
    return kv("concur-api-base-url").rstrip("/")

def concur_token_url() -> str:
    # Example secret value: https://us2.api.concursolutions.com/oauth2/v0/token
    # If not set, derive from base URL.
    try:
        return kv("concur-token-url").rstrip("/")
    except Exception:
        return f"{concur_base_url()}/oauth2/v0/token"

# ======================================================
# CONCUR OAUTH (REFRESH TOKEN FLOW)
# ======================================================

class ConcurOAuthClient:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.expires_at: float = 0.0

    def get_access_token(self) -> str:
        now = time.time()
        if self.access_token and now < self.expires_at - 60:
            return self.access_token

        token_url = concur_token_url()

        resp = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": kv("concur-refresh-token"),
                "client_id": kv("concur-client-id"),
                "client_secret": kv("concur-client-secret"),
            },
            timeout=20,
        )

        if resp.status_code in (400, 401, 403):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Concur OAuth failed (status {resp.status_code}). "
                    "Check Key Vault secrets and Concur app config."
                ),
            )

        resp.raise_for_status()
        data = resp.json()

        self.access_token = data["access_token"]
        self.expires_at = now + float(data.get("expires_in", 1800))
        return self.access_token

oauth = ConcurOAuthClient()

def concur_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {oauth.get_access_token()}", "Accept": "application/json"}

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

class CardTotalsRequest(AccrualsSearchRequest):
    transactionDateFrom: str
    transactionDateTo: str
    dateType: str  # TRANSACTION | POSTED | BILLING

# ======================================================
# IDENTITY v4.1 (USER RESOLUTION)
# ======================================================

def build_identity_filter(req) -> Optional[str]:
    filters = []

    for i in range(1, 7):
        val = getattr(req, f"orgUnit{i}", None)
        if val:
            filters.append(
                f'urn:ietf:params:scim:schemas:extension:spend:2.0:User:orgUnit{i} eq "{val}"'
            )

    if req.custom21:
        filters.append(
            'urn:ietf:params:scim:schemas:extension:spend:2.0:User:customData'
            f'[id eq "custom21" and value eq "{req.custom21}"]'
        )

    return " and ".join(filters) if filters else None

def get_users(filter_expression: Optional[str]) -> List[Dict]:
    url = f"{concur_base_url()}/profile/identity/v4.1/Users"
    params = {
        "attributes": (
            "id,displayName,userName,emails.value,"
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
        )
    }
    if filter_expression:
        params["filter"] = filter_expression

    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("Resources", [])

# ======================================================
# EXPENSE REPORTS v4
# ======================================================

def get_expense_reports(user_id: str) -> List[Dict]:
    url = f"{concur_base_url()}/expensereports/v4/users/{user_id}/reports"
    resp = requests.get(url, headers=concur_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", [])

def filter_unsubmitted_reports(reports: List[Dict]) -> List[Dict]:
    results = []
    for r in reports:
        payment_status_id = r.get("paymentStatusId")

        # Exclude reports already paid or processing
        if payment_status_id in ("P_PAID", "P_PROC"):
            continue

        results.append(
            {
                "lastName": r.get("owner", {}).get("lastName"),
                "firstName": r.get("owner", {}).get("firstName"),
                "reportName": r.get("name"),
                "submitted": r.get("approvalStatus") == "Submitted",
                "reportCreationDate": r.get("creationDate"),
                "reportSubmissionDate": r.get("submitDate"),
                "paymentStatusId": payment_status_id,
                "totalAmount": (r.get("totalAmount") or {}).get("value"),
            }
        )
    return results

# ======================================================
# CARDS v4
# ======================================================

def get_card_transactions(user_id: str, date_from: str, date_to: str) -> List[Dict]:
    url = f"{concur_base_url()}/cards/v4/users/{user_id}/transactions"
    params = {"transactionDateFrom": date_from, "transactionDateTo": date_to}
    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", [])

def filter_unassigned_cards(transactions: List[Dict]) -> List[Dict]:
    results = []
    for t in transactions:
        if t.get("expenseId") or t.get("reportId"):
            continue

        account = t.get("account") or {}
        payment_type = account.get("paymentType") or {}

        posted = t.get("postedAmount") or {}
        results.append(
            {
                "cardProgramId": payment_type.get("id"),
                "accountKey": account.get("lastSegment"),
                "lastFourDigits": account.get("lastFourDigits"),
                "postedAmount": posted.get("value"),
                "currencyCode": posted.get("currencyCode"),
            }
        )
    return results

# ======================================================
# CARD TOTALS LOGIC
# ======================================================

def extract_date(txn: Dict, date_type: str) -> date:
    if date_type == "POSTED":
        return isoparse(txn["postedDate"]).date()
    if date_type == "BILLING":
        return isoparse(txn["statement"]["billingDate"]).date()
    return isoparse(txn["transactionDate"]).date()

def compute_card_totals(transactions: List[Dict], from_date: date, to_date: date, date_type: str):
    by_program = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
    by_user = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})

    for t in transactions:
        if date_type == "BILLING":
            stmt = t.get("statement") or {}
            if not stmt.get("billingDate"):
                continue
        if date_type == "POSTED" and not t.get("postedDate"):
            continue
        if date_type == "TRANSACTION" and not t.get("transactionDate"):
            continue

        d = extract_date(t, date_type)
        if not (from_date <= d <= to_date):
            continue

        posted_amount = t.get("postedAmount") or {}
        amount = float(posted_amount.get("value") or 0.0)
        currency = str(posted_amount.get("currencyCode") or "")

        account = t.get("account") or {}
        payment_type = account.get("paymentType") or {}
        program = str(payment_type.get("id") or "")

        employee_id = t.get("employeeId")
        user_key = employee_id if employee_id else f'{account.get("lastSegment")} ({program})'

        by_program[program]["count"] += 1
        by_program[program]["total"] += amount
        by_program[program]["currency"] = currency

        by_user[user_key]["count"] += 1
        by_user[user_key]["total"] += amount
        by_user[user_key]["currency"] = currency

    return {
        "totalsByProgram": [{"cardProgramId": k, **v} for k, v in by_program.items()],
        "totalsByUser": [{"userKey": k, **v} for k, v in by_user.items()],
    }

# ======================================================
# EXCEL EXPORT
# ======================================================

def export_card_totals_excel(totals: Dict, meta: Dict) -> bytes:
    wb = load_workbook(TEMPLATE_PATH)

    ws = wb["Card totals"] if "Card totals" in wb.sheetnames else wb.create_sheet("Card totals")
    ws.delete_rows(1, ws.max_row)

    ws["A1"] = "Card totals"
    ws["A2"] = f"Generated {datetime.now():%Y-%m-%d %H:%M}"
    ws["A3"] = f'Date range: {meta["from"]} to {meta["to"]} ({meta["type"]})'

    ws.append([])
    ws.append(["Card program", "Count", "Total", "Currency"])

    for p in totals["totalsByProgram"]:
        ws.append([p["cardProgramId"], p["count"], p["total"], p["currency"]])

    ws.append([])
    ws.append(["User key", "Count", "Total", "Currency"])

    for u in totals["totalsByUser"]:
        ws.append([u["userKey"], u["count"], u["total"], u["currency"]])

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out.read()

# ======================================================
# API ENDPOINTS
# ======================================================

@app.post("/api/accruals/search")
def accruals_search(req: AccrualsSearchRequest):
    filter_expr = build_identity_filter(req)
    users = get_users(filter_expr)

    unsubmitted_reports = []
    unassigned_cards = []

    for u in users:
        reports = get_expense_reports(u["id"])
        unsubmitted_reports.extend(filter_unsubmitted_reports(reports))

        txns = get_card_transactions(u["id"], "2000-01-01", date.today().isoformat())
        unassigned_cards.extend(filter_unassigned_cards(txns))

    return {
        "summary": {
            "unsubmittedReportCount": len(unsubmitted_reports),
            "unassignedCardCount": len(unassigned_cards),
        },
        "unsubmittedReports": unsubmitted_reports,
        "unassignedCards": unassigned_cards,
    }

@app.post("/api/cardtotals/export")
def card_totals_export(req: CardTotalsRequest):
    date_type = (req.dateType or "").upper()
    if date_type not in ("TRANSACTION", "POSTED", "BILLING"):
        raise HTTPException(status_code=400, detail="dateType must be TRANSACTION, POSTED, or BILLING")

    filter_expr = build_identity_filter(req)
    users = get_users(filter_expr)

    all_txns = []
    for u in users:
        all_txns.extend(get_card_transactions(u["id"], req.transactionDateFrom, req.transactionDateTo))

    totals = compute_card_totals(
        all_txns,
        date.fromisoformat(req.transactionDateFrom),
        date.fromisoformat(req.transactionDateTo),
        date_type,
    )

    xlsx = export_card_totals_excel(
        totals,
        {"from": req.transactionDateFrom, "to": req.transactionDateTo, "type": date_type},
    )

    filename = f"Concur_Card_Totals_{datetime.now():%Y%m%d_%H%M}.xlsx"

    return StreamingResponse(
        BytesIO(xlsx),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/kv-test")
def kv_test():
    """
    Simple Key Vault read test.
    Returns:
      - status: ok (if read works)
      - status: error (if KV config/MI/permissions fail)
    """
    try:
        client_id = kv("concur-client-id")
        return {"status": "ok", "client_id_exists": bool(client_id), "keyvault": keyvault_status()}
    except Exception as e:
        # Controlled error response for Phase 1
        raise HTTPException(
            status_code=500,
            detail={"message": f"Key Vault read failed: {str(e)}", "keyvault": keyvault_status()},
        )

@app.get("/build")
def build():
    return {
        "fingerprint": BUILD_FINGERPRINT,
        "file": __file__,
        "cwd": os.getcwd(),
        "run_from_package": os.getenv("WEBSITE_RUN_FROM_PACKAGE"),
        "scm_build": os.getenv("SCM_DO_BUILD_DURING_DEPLOYMENT"),
        "scm_commit": os.getenv("SCM_COMMIT_ID"),
        "deployment_id": os.getenv("WEBSITE_DEPLOYMENT_ID"),
    }

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime, date
from io import BytesIO
import time
import requests
from collections import defaultdict
from openpyxl import load_workbook
from dateutil.parser import isoparse

# ======================================================
# CONFIGURATION (MOVE SECRETS TO KEY VAULT IN PROD)
# ======================================================

# CONCUR_BASE_URL = "https://<your-datacenter>.api.concursolutions.com"
CONCUR_AUTH_URL = "https://us2.api.concursolutions.com"  # example only
token_url = f"{CONCUR_AUTH_URL}/oauth2/v0/token"
TEMPLATE_PATH = "reports/accrual report.xlsx"
CONCUR_CLIENT_ID = "<FROM_KEY_VAULT>"
CONCUR_CLIENT_SECRET = "<FROM_KEY_VAULT>"
CONCUR_REFRESH_TOKEN = "<FROM_KEY_VAULT>"

# ======================================================
# FASTAPI APP
# ======================================================

app = FastAPI(title="Concur Accruals API")

# ======================================================
# CONCUR OAUTH (REFRESH TOKEN FLOW)
# ======================================================

class ConcurOAuthClient:
    def __init__(self):
        self.access_token = None
        self.expires_at = 0

    def get_access_token(self) -> str:
        now = time.time()
        if self.access_token and now < self.expires_at - 60:
            return self.access_token

        token_url = f"{CONCUR_BASE_URL}/oauth2/v0/token"
        resp = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": CONCUR_REFRESH_TOKEN,
                "client_id": CONCUR_CLIENT_ID,
                "client_secret": CONCUR_CLIENT_SECRET
            },
            timeout=20
        )
        resp.raise_for_status()

        data = resp.json()
        self.access_token = data["access_token"]
        self.expires_at = now + data.get("expires_in", 1800)
        return self.access_token

oauth = ConcurOAuthClient()

def concur_headers():
    return {
        "Authorization": f"Bearer {oauth.get_access_token()}",
        "Accept": "application/json"
    }

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
    url = f"{CONCUR_BASE_URL}/profile/identity/v4.1/Users"
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
    url = f"{CONCUR_BASE_URL}/expensereports/v4/users/{user_id}/reports"
    resp = requests.get(url, headers=concur_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", [])


def filter_unsubmitted_reports(reports: List[Dict]) -> List[Dict]:
    results = []

    for r in reports:
        payment_status_id = r.get("paymentStatusId")

        # Exclude reports already paid or sent for payment
        if payment_status_id in ("P_PAID", "P_PROC"):
            continue

        results.append({
            "lastName": r.get("owner", {}).get("lastName"),
            "firstName": r.get("owner", {}).get("firstName"),
            "reportName": r.get("name"),
            "submitted": r.get("approvalStatus") == "Submitted",
            "reportCreationDate": r.get("creationDate"),
            "reportSubmissionDate": r.get("submitDate"),
            "paymentStatusId": payment_status_id,
            "totalAmount": r.get("totalAmount", {}).get("value")
        })

    return results


# ======================================================
# CARDS v4
# ======================================================

def get_card_transactions(user_id: str, date_from: str, date_to: str) -> List[Dict]:
    url = f"{CONCUR_BASE_URL}/cards/v4/users/{user_id}/transactions"
    params = {
        "transactionDateFrom": date_from,
        "transactionDateTo": date_to
    }
    resp = requests.get(url, headers=concur_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("Items", [])


def filter_unassigned_cards(transactions: List[Dict]) -> List[Dict]:
    results = []
    for t in transactions:
        if t.get("expenseId") or t.get("reportId"):
            continue

        results.append({
            "cardProgramName": t["account"]["paymentType"]["id"],
            "accountKey": t["account"]["lastSegment"],
            "lastFourDigits": t["account"].get("lastFourDigits"),
            "postedAmount": t["postedAmount"]["value"]
        })
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
        d = extract_date(t, date_type)
        if not (from_date <= d <= to_date):
            continue

        amount = t["postedAmount"]["value"]
        currency = t["postedAmount"]["currencyCode"]
        program = t["account"]["paymentType"]["id"]

        employee_id = t.get("employeeId")
        user_key = employee_id if employee_id else f'{t["account"]["lastSegment"]} ({program})'

        by_program[program]["count"] += 1
        by_program[program]["total"] += amount
        by_program[program]["currency"] = currency

        by_user[user_key]["count"] += 1
        by_user[user_key]["total"] += amount
        by_user[user_key]["currency"] = currency

    return {
        "totalsByProgram": [{"cardProgramId": k, **v} for k, v in by_program.items()],
        "totalsByUser": [{"userKey": k, **v} for k, v in by_user.items()]
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
            "unassignedCardCount": len(unassigned_cards)
        },
        "unsubmittedReports": unsubmitted_reports,
        "unassignedCards": unassigned_cards
    }


@app.post("/api/cardtotals/export")
def card_totals_export(req: CardTotalsRequest):
    filter_expr = build_identity_filter(req)
    users = get_users(filter_expr)

    all_txns = []
    for u in users:
        all_txns.extend(
            get_card_transactions(u["id"], req.transactionDateFrom, req.transactionDateTo)
        )

    totals = compute_card_totals(
        all_txns,
        date.fromisoformat(req.transactionDateFrom),
        date.fromisoformat(req.transactionDateTo),
        req.dateType
    )

    xlsx = export_card_totals_excel(
        totals,
        {
            "from": req.transactionDateFrom,
            "to": req.transactionDateTo,
            "type": req.dateType
        }
    )

    filename = f"Concur_Card_Totals_{datetime.now():%Y%m%d_%H%M}.xlsx"

    return StreamingResponse(
        BytesIO(xlsx),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

from collections import defaultdict
from datetime import date
from dateutil.parser import isoparse

def extract_date(txn, date_type):
    if date_type == "POSTED":
        return isoparse(txn.get("postedDate")).date()
    if date_type == "BILLING":
        return isoparse(txn.get("statement", {}).get("billingDate")).date()
    return isoparse(txn.get("transactionDate")).date()

def compute_totals(transactions, date_from, date_to, date_type):
    by_program = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})
    by_user = defaultdict(lambda: {"count": 0, "total": 0.0, "currency": ""})

    for t in transactions:
        d = extract_date(t, date_type)
        if not (date_from <= d <= date_to):
            continue

        amount = t["postedAmount"]["value"]
        currency = t["postedAmount"]["currencyCode"]

        program = t["account"]["paymentType"]["id"]

        employee_id = t.get("employeeId")
        if employee_id:
            user_key = employee_id
        else:
            user_key = f'{t["account"]["lastSegment"]} ({program})'

        by_program[program]["count"] += 1
        by_program[program]["total"] += amount
        by_program[program]["currency"] = currency

        by_user[user_key]["count"] += 1
        by_user[user_key]["total"] += amount
        by_user[user_key]["currency"] = currency

    return {
        "totalsByProgram": [
            {"cardProgramId": k, **v} for k, v in by_program.items()
        ],
        "totalsByUser": [
            {"userKey": k, **v} for k, v in by_user.items()
        ]
    }

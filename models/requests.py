from pydantic import BaseModel
from typing import Optional

class CardTotalsRequest(BaseModel):
    orgUnit1: Optional[str]
    orgUnit2: Optional[str]
    orgUnit3: Optional[str]
    orgUnit4: Optional[str]
    orgUnit5: Optional[str]
    orgUnit6: Optional[str]
    custom21: Optional[str]

    transactionDateFrom: str
    transactionDateTo: str
    dateType: str  # TRANSACTION | POSTED | BILLING

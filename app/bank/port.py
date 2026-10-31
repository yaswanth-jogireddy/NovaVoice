"""CoreBankingPort: the only way the CSM agent touches bank data.

Every adapter returns plain dicts that are already masked (last 4 digits only; no DOB, mobile
or email). Authorisation is NOT the adapter's job: the policy engine decides before any call.
Future adapters: KastleAdapter (universal banking / lending), IMALAdapter (Islamic banking),
plus a ScreeningPort (AMLOCK-style) used by the policy engine for risky actions.
"""
from __future__ import annotations

from typing import Optional, Protocol


class BankError(Exception):
    """Business-rule failure that the agent may say to the caller (safe message)."""


class CoreBankingPort(Protocol):
    def find_customer(self, customer_ref: str) -> Optional[dict]:
        """Look up by customer id (C1001) or registered mobile (last 10 digits). Returns {id, first_name} or None."""

    def verify_identity(self, customer_id: str, dob: str, account_last4: str | None = None) -> bool:
        """True only if the DOB (YYYY-MM-DD) matches, and the account's last 4 digits too when they are given."""

    def contact_hint(self, customer_id: str) -> dict:
        """Masked hint for OTP delivery, e.g. {'mobile': '******1234'}."""

    def get_balance(self, customer_id: str) -> list[dict]: ...
    def recent_transactions(self, customer_id: str, n: int = 5) -> list[dict]: ...
    def list_cards(self, customer_id: str) -> list[dict]: ...
    def block_card(self, customer_id: str, card_last4: str, reason: str) -> dict: ...
    def loan_summary(self, customer_id: str, product: Optional[str] = None) -> list[dict]:
        """Loan accounts (LMS): next scheduled instalment, overdue amount kept separate, schedule."""

    def loan_applications(self, customer_id: str, product: Optional[str] = None) -> list[dict]:
        """Loan applications (LOS) with the stage in plain words."""

    def request_loan_document(self, customer_id: str, doc: str, product: Optional[str], channel: str) -> dict: ...
    def request_statement(self, customer_id: str, period: str, channel: str) -> dict: ...
    def raise_dispute(self, customer_id: str, txn_ref: str, reason: str) -> dict: ...
    def create_ticket(self, customer_id: Optional[str], category: str, summary: str) -> dict: ...
    def ticket_status(self, ticket_id: str, customer_id: Optional[str]) -> Optional[dict]: ...


class KastleAdapter:  # pragma: no cover - roadmap stub
    """Maps CoreBankingPort onto Azentio Kastle service APIs (account inquiry, card management, service requests)."""


class IMALAdapter:  # pragma: no cover - roadmap stub
    """Maps CoreBankingPort onto iMAL (Islamic banking). Wording pack: 'profit rate' instead of 'interest'."""


class ScreeningPort(Protocol):  # pragma: no cover - roadmap stub
    """AMLOCK-style screening before risky actions (contact change, large dispute). Returns clear | review."""
    def screen(self, customer_id: str, action: str) -> str: ...

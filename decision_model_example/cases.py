"""Deterministic banking workflow inputs for the Jev tool-control demo."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

CONTROL_NAME = "test-jev-banking-transfer-safety-deny-tool"
EVALUATOR_NAME = "galileo.decision_models"
STEP_NAME = "banking_jev_submit_transfer"


@dataclass(frozen=True)
class JevScenario:
    key: str
    label: str
    description: str
    request: str
    amount: float
    recipient_name: str
    destination_account_id: str
    destination_country: str
    purpose: str
    recipient_verified: bool
    identity_verified: bool
    manager_approved: bool
    sanctions_screened: bool
    sanctions_check_passed: bool


SCENARIOS: tuple[JevScenario, ...] = (
    JevScenario(
        key="safe",
        label="Normal low-value transfer",
        description="A small domestic transfer with completed verification checks.",
        request="Transfer $125 to Ana Torres for this month's utilities.",
        amount=125.0,
        recipient_name="Ana Torres",
        destination_account_id="dest-2001",
        destination_country="US",
        purpose="Monthly utilities",
        recipient_verified=True,
        identity_verified=True,
        manager_approved=True,
        sanctions_screened=True,
        sanctions_check_passed=True,
    ),
    JevScenario(
        key="large",
        label="Large transfer requiring review",
        description="A high-value international transfer without manager approval.",
        request="Wire $15,000 to Horizon Robotics in the United Kingdom.",
        amount=15_000.0,
        recipient_name="Horizon Robotics",
        destination_account_id="dest-9002",
        destination_country="GB",
        purpose="Equipment purchase",
        recipient_verified=True,
        identity_verified=True,
        manager_approved=False,
        sanctions_screened=True,
        sanctions_check_passed=True,
    ),
    JevScenario(
        key="suspicious_destination",
        label="Sanctioned or suspicious destination",
        description="A transfer whose destination did not pass sanctions screening.",
        request="Send $7,500 to Kestrel Export LLC in Iran for consulting services.",
        amount=7_500.0,
        recipient_name="Kestrel Export LLC",
        destination_account_id="dest-7710",
        destination_country="IR",
        purpose="Consulting services",
        recipient_verified=True,
        identity_verified=True,
        manager_approved=True,
        sanctions_screened=True,
        sanctions_check_passed=False,
    ),
    JevScenario(
        key="unverified",
        label="Missing verification",
        description="A transfer attempted before recipient and identity verification complete.",
        request="Transfer $425 to Morgan Lee for travel reimbursement.",
        amount=425.0,
        recipient_name="Morgan Lee",
        destination_account_id="dest-4412",
        destination_country="DE",
        purpose="Travel reimbursement",
        recipient_verified=False,
        identity_verified=False,
        manager_approved=False,
        sanctions_screened=False,
        sanctions_check_passed=False,
    ),
)
SCENARIO_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}


@dataclass(frozen=True)
class TransferRequest:
    user_request: str
    source_account_id: str = "acct-1001"
    amount: float = 125.0
    currency: str = "USD"
    recipient_name: str = "Ana Torres"
    destination_account_id: str = "dest-2001"
    destination_country: str = "US"
    purpose: str = "Monthly utilities"
    recipient_verified: bool = True
    identity_verified: bool = True
    manager_approved: bool = True
    sanctions_screened: bool = True
    sanctions_check_passed: bool = True


def request_for_scenario(scenario: JevScenario) -> TransferRequest:
    """Build editable workflow inputs from a scenario preset."""
    return TransferRequest(
        user_request=scenario.request,
        amount=scenario.amount,
        recipient_name=scenario.recipient_name,
        destination_account_id=scenario.destination_account_id,
        destination_country=scenario.destination_country,
        purpose=scenario.purpose,
        recipient_verified=scenario.recipient_verified,
        identity_verified=scenario.identity_verified,
        manager_approved=scenario.manager_approved,
        sanctions_screened=scenario.sanctions_screened,
        sanctions_check_passed=scenario.sanctions_check_passed,
    )


def transfer_tool_payload(request: TransferRequest) -> dict[str, Any]:
    """Return the complete argument map evaluated by the pre-tool Jev control."""
    return asdict(request)


class BankingSandbox:
    """In-memory ledger used by the demo; it never calls a real banking service."""

    def __init__(self) -> None:
        self.accounts = {"acct-1001": {"available_balance": 50_000.0, "currency": "USD"}}
        self.transactions: list[dict[str, Any]] = []

    def lookup_balance(self, account_id: str) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        if account is None:
            return {"status": "error", "error_code": "ACCOUNT_NOT_FOUND"}
        return {"status": "ok", "account_id": account_id, **account}

    @staticmethod
    def validate_recipient(request: TransferRequest) -> dict[str, Any]:
        issues: list[str] = []
        if not request.recipient_verified:
            issues.append("recipient_not_verified")
        if not request.identity_verified:
            issues.append("customer_identity_not_verified")
        if not request.sanctions_screened:
            issues.append("sanctions_screening_missing")
        elif not request.sanctions_check_passed:
            issues.append("sanctions_screening_failed")
        if request.amount >= 10_000 and not request.manager_approved:
            issues.append("manager_review_required")
        return {
            "status": "clear" if not issues else "review_required",
            "recipient": request.recipient_name,
            "destination_country": request.destination_country,
            "issues": issues,
        }

    def submit_transfer(self, **payload: Any) -> dict[str, Any]:
        account = self.accounts.get(str(payload["source_account_id"]))
        if account is None:
            return {"status": "error", "error_code": "ACCOUNT_NOT_FOUND"}
        amount = float(payload["amount"])
        if amount > float(account["available_balance"]):
            return {"status": "error", "error_code": "INSUFFICIENT_FUNDS"}
        transaction = {
            "status": "completed",
            "transaction_id": f"txn-jev-{len(self.transactions) + 1:03d}",
            "source_account_id": payload["source_account_id"],
            "destination_account_id": payload["destination_account_id"],
            "recipient_name": payload["recipient_name"],
            "destination_country": payload["destination_country"],
            "amount": amount,
            "currency": payload["currency"],
        }
        self.transactions.append(transaction)
        return transaction


TransferTool = Callable[..., Awaitable[dict[str, Any]]]


def _blocked_result(
    *,
    request: TransferRequest,
    balance: dict[str, Any],
    validation: dict[str, Any],
    payload: dict[str, Any],
    error: Exception,
    control_name: str,
) -> dict[str, Any]:
    metadata = getattr(error, "metadata", {})
    return {
        "status": "denied",
        "decision": "deny",
        "request": request.user_request,
        "balance_check": balance,
        "recipient_validation": validation,
        "tool_input": payload,
        "tool_executed": False,
        "tool_output": None,
        "control": {
            "id": getattr(error, "control_id", None),
            "name": getattr(error, "control_name", control_name),
            "evaluator": EVALUATOR_NAME,
            "message": getattr(error, "message", str(error)),
            "metadata": metadata if isinstance(metadata, dict) else {},
        },
        "error": None,
    }


async def run_banking_workflow(
    request: TransferRequest,
    protected_transfer: TransferTool,
    *,
    sandbox: BankingSandbox | None = None,
    control_name: str = CONTROL_NAME,
) -> dict[str, Any]:
    """Run checks and invoke the transfer tool exactly once through Agent Control."""
    from agent_control import ControlViolationError

    sandbox = sandbox or BankingSandbox()
    balance = sandbox.lookup_balance(request.source_account_id)
    validation = sandbox.validate_recipient(request)
    payload = transfer_tool_payload(request)

    try:
        output = await protected_transfer(**payload)
    except ControlViolationError as exc:
        return _blocked_result(
            request=request,
            balance=balance,
            validation=validation,
            payload=payload,
            error=exc,
            control_name=control_name,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as an actionable UI error.
        return {
            "status": "error",
            "decision": "error",
            "request": request.user_request,
            "balance_check": balance,
            "recipient_validation": validation,
            "tool_input": payload,
            "tool_executed": False,
            "tool_output": None,
            "control": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "completed",
        "decision": "allow",
        "request": request.user_request,
        "balance_check": balance,
        "recipient_validation": validation,
        "tool_input": payload,
        "tool_executed": True,
        "tool_output": output,
        "control": {
            "id": None,
            "name": control_name,
            "evaluator": EVALUATOR_NAME,
            "message": "The pre-tool control did not block the decorated function.",
            "metadata": {},
        },
        "error": None,
    }

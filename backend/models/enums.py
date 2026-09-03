import enum


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    PAID = "PAID"
    ATTEMPTED = "ATTEMPTED"


class PaymentStatus(str, enum.Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"


class RecoveryCaseStatus(str, enum.Enum):
    DETECTED = "DETECTED"
    DIAGNOSING = "DIAGNOSING"
    SCORED = "SCORED"
    ACTION_SELECTED = "ACTION_SELECTED"
    SAFETY_CHECK = "SAFETY_CHECK"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    WAITING_FOR_RESULT = "WAITING_FOR_RESULT"
    RECOVERED = "RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"
    STOPPED = "STOPPED"
    ESCALATED = "ESCALATED"


TERMINAL_CASE_STATUSES = (
    RecoveryCaseStatus.RECOVERED,
    RecoveryCaseStatus.NOT_RECOVERED,
    RecoveryCaseStatus.STOPPED,
    RecoveryCaseStatus.ESCALATED,
)

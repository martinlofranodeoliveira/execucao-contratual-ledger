"""Passive SDK metadata and private persistence records.

Codex support helper source; no domain transition/authentication implementation.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum

class Role(StrEnum):
    MANAGER = 'MANAGER'
    SUBMITTER = 'SUBMITTER'
    RECEIVER = 'RECEIVER'
    AUDITOR = 'AUDITOR'


class MeasurementState(StrEnum):
    DRAFT = 'DRAFT'
    SUBMITTED = 'SUBMITTED'
    RETURNED = 'RETURNED'
    ACCEPTED = 'ACCEPTED'
    ACCEPTED_PARTIAL = 'ACCEPTED_PARTIAL'
    REVERSED = 'REVERSED'


class Action(StrEnum):
    CREATE_CONTRACT = 'create_contract'
    CREATE_MEASUREMENT = 'create_measurement'
    REVISE_MEASUREMENT = 'revise_measurement'
    SUBMIT_MEASUREMENT = 'submit_measurement'
    RECEIVE_MEASUREMENT = 'receive_measurement'
    RETURN_MEASUREMENT = 'return_measurement'
    AMEND_CONTRACT = 'amend_contract'
    REVERSE_RECEIPT = 'reverse_receipt'


class Outcome(StrEnum):
    NO_EFFECT = 'NO_EFFECT'
    UNKNOWN = 'UNKNOWN'


class ErrorCode(StrEnum):
    INVALID_INPUT = 'INVALID_INPUT'
    INVALID_LIMIT = 'INVALID_LIMIT'
    ALREADY_EXISTS = 'ALREADY_EXISTS'
    NOT_FOUND = 'NOT_FOUND'
    FORBIDDEN = 'FORBIDDEN'
    SELF_REVIEW_FORBIDDEN = 'SELF_REVIEW_FORBIDDEN'
    STALE_VERSION = 'STALE_VERSION'
    STATE_CONFLICT = 'STATE_CONFLICT'
    IDEMPOTENCY_CONFLICT = 'IDEMPOTENCY_CONFLICT'
    INSUFFICIENT_BALANCE = 'INSUFFICIENT_BALANCE'
    AMENDMENT_FLOOR = 'AMENDMENT_FLOOR'
    ALREADY_REVERSED = 'ALREADY_REVERSED'
    LIMIT_EXCEEDED = 'LIMIT_EXCEEDED'
    CLOSED = 'CLOSED'
    WRONG_THREAD = 'WRONG_THREAD'
    BUSY = 'BUSY'
    INCOMPATIBLE_STORE = 'INCOMPATIBLE_STORE'
    CORRUPT_STORE = 'CORRUPT_STORE'
    STORAGE_ERROR = 'STORAGE_ERROR'
    COMMIT_UNCERTAIN = 'COMMIT_UNCERTAIN'


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id:str


@dataclass(frozen=True, slots=True)
class ActorSpec:
    actor_id:str
    roles:tuple[Role,...]


@dataclass(frozen=True, slots=True)
class ItemSpec:
    item_id:str
    unit:str
    scale:int
    authorized_ticks:int


@dataclass(frozen=True, slots=True)
class Line:
    item_id:str
    ticks:int


@dataclass(frozen=True, slots=True)
class Limits:
    max_actors:int=32
    max_contracts:int=32
    max_items_per_contract:int=16
    max_measurements_per_contract:int=256
    max_revisions_per_measurement:int=8
    max_events:int=4096


@dataclass(frozen=True, slots=True)
class ItemBalance:
    item_id:str
    unit:str
    scale:int
    authorized_ticks:int
    reserved_ticks:int
    received_ticks:int
    available_ticks:int


@dataclass(frozen=True, slots=True)
class Receipt:
    operation_id:str
    contract_id:str
    action:Action
    actor_id:str
    committed_version:int
    contract_revision_no:int
    measurement_id:str|None
    measurement_revision_no:int|None
    measurement_state:MeasurementState|None
    balances:tuple[ItemBalance,...]


@dataclass(frozen=True, slots=True)
class WriteResult:
    receipt:Receipt
    replayed:bool


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    contract_id:str
    version:int
    revision_no:int
    member_ids:tuple[str,...]
    balances:tuple[ItemBalance,...]


@dataclass(frozen=True, slots=True)
class SubmissionFact:
    version:int
    operation_id:str
    actor_id:str
    contract_revision_no:int


@dataclass(frozen=True, slots=True)
class DecisionFact:
    version:int
    operation_id:str
    action:Action
    actor_id:str
    accepted_lines:tuple[Line,...]
    reason:str|None


@dataclass(frozen=True, slots=True)
class ReversalFact:
    version:int
    operation_id:str
    actor_id:str
    reverses_operation_id:str
    reason:str


@dataclass(frozen=True, slots=True)
class MeasurementRevision:
    revision_no:int
    lines:tuple[Line,...]
    submission:SubmissionFact|None
    decision:DecisionFact|None
    reversal:ReversalFact|None


@dataclass(frozen=True, slots=True)
class MeasurementSnapshot:
    contract_id:str
    measurement_id:str
    owner_id:str
    state:MeasurementState
    revisions:tuple[MeasurementRevision,...]


@dataclass(frozen=True, slots=True)
class BalanceDelta:
    item_id:str
    authorized_delta:int
    reserved_delta:int
    received_delta:int


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    version:int
    action:Action
    actor_id:str
    operation_id:str
    contract_revision_no:int
    measurement_id:str|None
    measurement_revision_no:int|None
    command_json:str
    deltas:tuple[BalanceDelta,...]


@dataclass(frozen=True, slots=True)
class HistoryPage:
    through_version:int
    events:tuple[EventSnapshot,...]
    next_after:int
    complete:bool


@dataclass(frozen=True, slots=True)
class _MeasurementRow:
    contract_id:str
    measurement_id:str
    owner_id:str
    state:MeasurementState
    revision_no:int


@dataclass(frozen=True, slots=True)
class _StoredOperation:
    canonical_payload:str
    payload_sha256:str
    receipt:Receipt


@dataclass(frozen=True, slots=True)
class _CommitPlan:
    contract:ContractSnapshot
    create_contract:bool
    measurement:_MeasurementRow|None
    create_measurement:bool
    new_revision:MeasurementRevision|None
    submission:SubmissionFact|None
    decision:DecisionFact|None
    reversal:ReversalFact|None
    event:EventSnapshot
    operation:_StoredOperation


class LedgerError(Exception):
    """Closed error metadata; no native exception, path or arbitrary message."""
    def __init__(self, code: ErrorCode, outcome: Outcome = Outcome.NO_EFFECT,
                 current_version: int | None = None):
        if type(code) is not ErrorCode or type(outcome) is not Outcome:
            raise TypeError("invalid error metadata")
        if current_version is not None and (
            type(current_version) is not int or not 1 <= current_version <= 4096
            or code is not ErrorCode.STALE_VERSION
        ):
            raise TypeError("invalid error metadata")
        if code is ErrorCode.COMMIT_UNCERTAIN and outcome is not Outcome.UNKNOWN:
            raise TypeError("invalid error metadata")
        self.code = code
        self.outcome = outcome
        self.current_version = current_version
        super().__init__(code.value)

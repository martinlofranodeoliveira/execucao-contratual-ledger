from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .store import _Store, _Tx
from .types import (
    Action,
    ActorSpec,
    BalanceDelta,
    ContractSnapshot,
    DecisionFact,
    ErrorCode,
    EventSnapshot,
    HistoryPage,
    ItemBalance,
    ItemSpec,
    LedgerError,
    Limits,
    Line,
    MeasurementRevision,
    MeasurementSnapshot,
    MeasurementState,
    Principal,
    Receipt,
    ReversalFact,
    Role,
    SubmissionFact,
    WriteResult,
    _CommitPlan,
    _MeasurementRow,
    _StoredOperation,
)

__all__ = ["Ledger"]

_ID_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_UNIT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]{0,31}\Z", re.ASCII)

_MAX_ACTORS = 32
_MAX_CONTRACTS = 32
_MAX_ITEMS = 16
_MAX_MEASUREMENTS = 256
_MAX_REVISIONS = 8
_MAX_EVENTS = 4096
_MAX_TICKS = 10**12
_MAX_REASON_BYTES = 256
_MAX_PATH_BYTES = 4096
_MAX_TEXT_BYTES = 16384
_ELEVATED_ROLES = (Role.MANAGER, Role.RECEIVER, Role.AUDITOR)


def _validate_id(value: object) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return value


def _validate_unit(value: object) -> str:
    if type(value) is not str or _UNIT_RE.fullmatch(value) is None:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return value


def _validate_principal(principal: object) -> str:
    if type(principal) is not Principal:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return _validate_id(principal.actor_id)


def _validate_path(path: object) -> str:
    if type(path) is not str or not path or "\x00" in path:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    try:
        encoded = path.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise LedgerError(ErrorCode.INVALID_INPUT) from exc
    if len(encoded) > _MAX_PATH_BYTES:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return path


def _validate_timeout(timeout_ms: object) -> int:
    if type(timeout_ms) is not int or timeout_ms < 1 or timeout_ms > 5000:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return timeout_ms


def _validate_limits(limits: object) -> Limits:
    if type(limits) is not Limits:
        raise LedgerError(ErrorCode.INVALID_LIMIT)
    bounds = (
        (limits.max_actors, _MAX_ACTORS),
        (limits.max_contracts, _MAX_CONTRACTS),
        (limits.max_items_per_contract, _MAX_ITEMS),
        (limits.max_measurements_per_contract, _MAX_MEASUREMENTS),
        (limits.max_revisions_per_measurement, _MAX_REVISIONS),
        (limits.max_events, _MAX_EVENTS),
    )
    for val, max_bound in bounds:
        if type(val) is not int or val < 0 or val > max_bound:
            raise LedgerError(ErrorCode.INVALID_LIMIT)
    return limits


def _validate_actors(actors: object) -> tuple[ActorSpec, ...]:
    if type(actors) not in (list, tuple) or not (1 <= len(actors) <= _MAX_ACTORS):
        raise LedgerError(ErrorCode.INVALID_INPUT)
    seen_ids: set[str] = set()
    normalized: list[ActorSpec] = []
    has_manager = False
    for entry in actors:
        if type(entry) is not ActorSpec:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        actor_id = _validate_id(entry.actor_id)
        if actor_id in seen_ids:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        seen_ids.add(actor_id)
        roles_raw = entry.roles
        if type(roles_raw) not in (list, tuple) or not (1 <= len(roles_raw) <= 4):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        seen_roles: set[Role] = set()
        role_list: list[Role] = []
        for r in roles_raw:
            if type(r) is not Role or r in seen_roles:
                raise LedgerError(ErrorCode.INVALID_INPUT)
            seen_roles.add(r)
            role_list.append(r)
        if Role.MANAGER in seen_roles:
            has_manager = True
        role_list.sort(key=lambda x: x.value)
        normalized.append(ActorSpec(actor_id=actor_id, roles=tuple(role_list)))
    if not has_manager:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    normalized.sort(key=lambda a: a.actor_id)
    return tuple(normalized)


def _validate_expected_version(expected_version: object, *, is_create_contract: bool) -> int:
    if type(expected_version) is not int:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    if is_create_contract:
        if expected_version != 0:
            raise LedgerError(ErrorCode.INVALID_INPUT)
    elif not (1 <= expected_version <= _MAX_EVENTS):
        raise LedgerError(ErrorCode.INVALID_INPUT)
    return expected_version


def _validate_reason(reason: object, *, allow_none: bool = False) -> str | None:
    if reason is None:
        if allow_none:
            return None
        raise LedgerError(ErrorCode.INVALID_INPUT)
    if type(reason) is not str:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    try:
        raw = reason.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise LedgerError(ErrorCode.INVALID_INPUT) from exc
    if not (1 <= len(raw) <= _MAX_REASON_BYTES) or not reason.strip():
        raise LedgerError(ErrorCode.INVALID_INPUT)
    for ch in reason:
        cp = ord(ch)
        if cp <= 31 or (127 <= cp <= 159):
            raise LedgerError(ErrorCode.INVALID_INPUT)
    return reason


def _validate_items(items: object) -> tuple[ItemSpec, ...]:
    if type(items) not in (list, tuple) or not (1 <= len(items) <= _MAX_ITEMS):
        raise LedgerError(ErrorCode.INVALID_INPUT)
    seen: set[str] = set()
    normalized: list[ItemSpec] = []
    for item in items:
        if type(item) is not ItemSpec:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        item_id = _validate_id(item.item_id)
        if item_id in seen:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        seen.add(item_id)
        unit = _validate_unit(item.unit)
        if type(item.scale) is not int or item.scale not in (1, 1000):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        if type(item.authorized_ticks) is not int or not (1 <= item.authorized_ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        normalized.append(
            ItemSpec(
                item_id=item_id,
                unit=unit,
                scale=item.scale,
                authorized_ticks=item.authorized_ticks,
            )
        )
    normalized.sort(key=lambda x: x.item_id)
    return tuple(normalized)


def _validate_member_ids(member_ids: object) -> tuple[str, ...]:
    if type(member_ids) not in (list, tuple) or not (1 <= len(member_ids) <= _MAX_ACTORS):
        raise LedgerError(ErrorCode.INVALID_INPUT)
    seen: set[str] = set()
    normalized: list[str] = []
    for mid in member_ids:
        valid_id = _validate_id(mid)
        if valid_id in seen:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        seen.add(valid_id)
        normalized.append(valid_id)
    normalized.sort()
    return tuple(normalized)


def _validate_lines(
    lines: object,
    *,
    allow_zero: bool,
    require_positive_any: bool,
) -> tuple[Line, ...]:
    if type(lines) not in (list, tuple) or not (1 <= len(lines) <= _MAX_ITEMS):
        raise LedgerError(ErrorCode.INVALID_INPUT)
    seen: set[str] = set()
    normalized: list[Line] = []
    min_ticks = 0 if allow_zero else 1
    has_positive = False
    for line in lines:
        if type(line) is not Line:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        item_id = _validate_id(line.item_id)
        if item_id in seen:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        seen.add(item_id)
        if type(line.ticks) is not int or not (min_ticks <= line.ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        if line.ticks > 0:
            has_positive = True
        normalized.append(Line(item_id=item_id, ticks=line.ticks))
    if require_positive_any and not has_positive:
        raise LedgerError(ErrorCode.INVALID_INPUT)
    normalized.sort(key=lambda x: x.item_id)
    return tuple(normalized)


def _encode_canonical_command(
    *,
    action: Action,
    actor_id: str,
    contract_id: str,
    expected_version: int,
    data: dict[str, Any],
) -> tuple[str, str]:
    envelope = {
        "action": action.value,
        "actor_id": actor_id,
        "contract_id": contract_id,
        "data": data,
        "expected_version": expected_version,
    }
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw = canonical.encode("utf-8")
    if len(raw) > _MAX_TEXT_BYTES:
        raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
    return canonical, hashlib.sha256(raw).hexdigest()


def _check_artifact_bounds(receipt: Receipt, event: EventSnapshot) -> None:
    receipt_doc = {
        "action": receipt.action.value,
        "actor_id": receipt.actor_id,
        "balances": [
            {
                "authorized_ticks": b.authorized_ticks,
                "available_ticks": b.available_ticks,
                "item_id": b.item_id,
                "received_ticks": b.received_ticks,
                "reserved_ticks": b.reserved_ticks,
                "scale": b.scale,
                "unit": b.unit,
            }
            for b in receipt.balances
        ],
        "committed_version": receipt.committed_version,
        "contract_id": receipt.contract_id,
        "contract_revision_no": receipt.contract_revision_no,
        "measurement_id": receipt.measurement_id,
        "measurement_revision_no": receipt.measurement_revision_no,
        "measurement_state": None if receipt.measurement_state is None else receipt.measurement_state.value,
        "operation_id": receipt.operation_id,
    }
    event_doc = {
        "action": event.action.value,
        "actor_id": event.actor_id,
        "command_json": event.command_json,
        "contract_revision_no": event.contract_revision_no,
        "deltas": [
            {
                "authorized_delta": d.authorized_delta,
                "item_id": d.item_id,
                "received_delta": d.received_delta,
                "reserved_delta": d.reserved_delta,
            }
            for d in event.deltas
        ],
        "measurement_id": event.measurement_id,
        "measurement_revision_no": event.measurement_revision_no,
        "operation_id": event.operation_id,
        "version": event.version,
    }
    for doc in (receipt_doc, event_doc):
        encoded = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > _MAX_TEXT_BYTES:
            raise LedgerError(ErrorCode.LIMIT_EXCEEDED)


def _validate_stored_contract(contract: ContractSnapshot) -> None:
    if contract.version < 1 or contract.revision_no < 1:
        raise LedgerError(ErrorCode.CORRUPT_STORE)
    if not contract.member_ids or not contract.balances:
        raise LedgerError(ErrorCode.CORRUPT_STORE)
    prev_member: str | None = None
    for mid in contract.member_ids:
        if prev_member is not None and mid <= prev_member:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        prev_member = mid
    prev_item: str | None = None
    for bal in contract.balances:
        if prev_item is not None and bal.item_id <= prev_item:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        prev_item = bal.item_id
        if bal.scale not in (1, 1000):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if not (0 <= bal.authorized_ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if not (0 <= bal.reserved_ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if not (0 <= bal.received_ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if bal.reserved_ticks + bal.received_ticks > bal.authorized_ticks:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if bal.available_ticks != bal.authorized_ticks - bal.reserved_ticks - bal.received_ticks:
            raise LedgerError(ErrorCode.CORRUPT_STORE)


def _validate_stored_revision(
    contract: ContractSnapshot,
    m_row: _MeasurementRow,
    rev: MeasurementRevision,
    *,
    is_current: bool,
) -> None:
    if m_row.revision_no < 1 or rev.revision_no < 1 or not rev.lines:
        raise LedgerError(ErrorCode.CORRUPT_STORE)
    contract_items = {b.item_id for b in contract.balances}
    prev_item: str | None = None
    for line in rev.lines:
        if line.item_id not in contract_items or not (1 <= line.ticks <= _MAX_TICKS):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if prev_item is not None and line.item_id <= prev_item:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        prev_item = line.item_id
    if not is_current:
        if rev.reversal is not None:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        if rev.decision is not None and (
            rev.submission is None or rev.decision.action != Action.RETURN_MEASUREMENT
        ):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
        return
    if rev.revision_no != m_row.revision_no:
        raise LedgerError(ErrorCode.CORRUPT_STORE)
    state = m_row.state
    if state == MeasurementState.DRAFT:
        if rev.submission is not None or rev.decision is not None or rev.reversal is not None:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
    elif state == MeasurementState.SUBMITTED:
        if rev.submission is None or rev.decision is not None or rev.reversal is not None:
            raise LedgerError(ErrorCode.CORRUPT_STORE)
    elif state == MeasurementState.RETURNED:
        if (
            rev.submission is None
            or rev.decision is None
            or rev.decision.action != Action.RETURN_MEASUREMENT
            or rev.reversal is not None
        ):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
    elif state in (MeasurementState.ACCEPTED, MeasurementState.ACCEPTED_PARTIAL):
        if (
            rev.submission is None
            or rev.decision is None
            or rev.decision.action != Action.RECEIVE_MEASUREMENT
            or rev.reversal is not None
        ):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
    elif state == MeasurementState.REVERSED:
        if (
            rev.submission is None
            or rev.decision is None
            or rev.decision.action != Action.RECEIVE_MEASUREMENT
            or rev.reversal is None
            or rev.reversal.reverses_operation_id != rev.decision.operation_id
        ):
            raise LedgerError(ErrorCode.CORRUPT_STORE)
    else:
        raise LedgerError(ErrorCode.CORRUPT_STORE)


def _compute_deltas(
    old_balances: tuple[ItemBalance, ...],
    new_balances: tuple[ItemBalance, ...],
) -> tuple[BalanceDelta, ...]:
    deltas: list[BalanceDelta] = []
    for old_b, new_b in zip(old_balances, new_balances, strict=True):
        auth_d = new_b.authorized_ticks - old_b.authorized_ticks
        res_d = new_b.reserved_ticks - old_b.reserved_ticks
        rec_d = new_b.received_ticks - old_b.received_ticks
        if auth_d != 0 or res_d != 0 or rec_d != 0:
            deltas.append(
                BalanceDelta(
                    item_id=new_b.item_id,
                    authorized_delta=auth_d,
                    reserved_delta=res_d,
                    received_delta=rec_d,
                )
            )
    return tuple(deltas)


def _build_plan(
    *,
    action: Action,
    actor_id: str,
    operation_id: str,
    canonical_payload: str,
    payload_sha256: str,
    contract: ContractSnapshot,
    create_contract: bool,
    measurement: _MeasurementRow | None,
    create_measurement: bool,
    new_revision: MeasurementRevision | None,
    submission: SubmissionFact | None,
    decision: DecisionFact | None,
    reversal: ReversalFact | None,
    deltas: tuple[BalanceDelta, ...],
) -> _CommitPlan:
    m_id = None if measurement is None else measurement.measurement_id
    m_rev_no = None if measurement is None else measurement.revision_no
    m_state = None if measurement is None else measurement.state
    receipt = Receipt(
        operation_id=operation_id,
        contract_id=contract.contract_id,
        action=action,
        actor_id=actor_id,
        committed_version=contract.version,
        contract_revision_no=contract.revision_no,
        measurement_id=m_id,
        measurement_revision_no=m_rev_no,
        measurement_state=m_state,
        balances=contract.balances,
    )
    event = EventSnapshot(
        version=contract.version,
        action=action,
        actor_id=actor_id,
        operation_id=operation_id,
        contract_revision_no=contract.revision_no,
        measurement_id=m_id,
        measurement_revision_no=m_rev_no,
        command_json=canonical_payload,
        deltas=deltas,
    )
    _check_artifact_bounds(receipt, event)
    stored_op = _StoredOperation(
        canonical_payload=canonical_payload,
        payload_sha256=payload_sha256,
        receipt=receipt,
    )
    return _CommitPlan(
        contract=contract,
        create_contract=create_contract,
        measurement=measurement,
        create_measurement=create_measurement,
        new_revision=new_revision,
        submission=submission,
        decision=decision,
        reversal=reversal,
        event=event,
        operation=stored_op,
    )


def _check_idempotency(
    tx: _Tx,
    contract_id: str,
    operation_id: str,
    canonical_payload: str,
    payload_sha256: str,
) -> Receipt | None:
    existing = tx.operation(contract_id, operation_id, owner_filter=None)
    if existing is None:
        return None
    if existing.payload_sha256 == payload_sha256 and existing.canonical_payload == canonical_payload:
        return existing.receipt
    raise LedgerError(ErrorCode.IDEMPOTENCY_CONFLICT)


def _require_seeded_member(
    tx: _Tx,
    actor_id: str,
    contract_id: str,
) -> tuple[tuple[Role, ...], ContractSnapshot]:
    roles = tx.actor_roles(actor_id)
    if roles is None:
        raise LedgerError(ErrorCode.FORBIDDEN)
    contract = tx.contract(contract_id)
    if contract is None or actor_id not in contract.member_ids:
        raise LedgerError(ErrorCode.NOT_FOUND)
    _validate_stored_contract(contract)
    return roles, contract


def _require_measurement(
    tx: _Tx,
    contract: ContractSnapshot,
    measurement_id: str,
) -> tuple[_MeasurementRow, MeasurementRevision]:
    m_row = tx.measurement(contract.contract_id, measurement_id)
    if m_row is None:
        raise LedgerError(ErrorCode.NOT_FOUND)
    rev = tx.current_revision(contract.contract_id, measurement_id)
    if rev is None:
        raise LedgerError(ErrorCode.CORRUPT_STORE)
    _validate_stored_revision(contract, m_row, rev, is_current=True)
    return m_row, rev


class Ledger:
    __slots__ = ("_store",)

    def __init__(self, store: _Store) -> None:
        if type(store) is not _Store:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        self._store = store

    @classmethod
    def create(
        cls,
        path: str,
        actors: list[ActorSpec] | tuple[ActorSpec, ...],
        *,
        limits: Limits = Limits(),
        timeout_ms: int = 1000,
    ) -> Ledger:
        valid_path = _validate_path(path)
        valid_timeout = _validate_timeout(timeout_ms)
        valid_limits = _validate_limits(limits)
        valid_actors = _validate_actors(actors)
        if len(valid_actors) > valid_limits.max_actors:
            raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
        store = _Store.create(valid_path, valid_actors, valid_limits, valid_timeout)
        return cls(store)

    @classmethod
    def open(cls, path: str, *, timeout_ms: int = 1000) -> Ledger:
        valid_path = _validate_path(path)
        valid_timeout = _validate_timeout(timeout_ms)
        store = _Store.open(valid_path, valid_timeout)
        return cls(store)

    def close(self) -> None:
        self._store.close()

    def create_contract(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        items: list[ItemSpec] | tuple[ItemSpec, ...],
        member_ids: list[str] | tuple[str, ...],
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=True)
        valid_items = _validate_items(items)
        valid_members = _validate_member_ids(member_ids)
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.CREATE_CONTRACT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={
                "items": [
                    {
                        "authorized_ticks": it.authorized_ticks,
                        "item_id": it.item_id,
                        "scale": it.scale,
                        "unit": it.unit,
                    }
                    for it in valid_items
                ],
                "member_ids": list(valid_members),
            },
        )
        with self._store.write() as tx:
            roles = tx.actor_roles(actor_id)
            if roles is None:
                raise LedgerError(ErrorCode.FORBIDDEN)
            if Role.MANAGER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            existing_contract = tx.contract(valid_cid)
            if existing_contract is not None:
                _validate_stored_contract(existing_contract)
                if actor_id not in existing_contract.member_ids:
                    raise LedgerError(ErrorCode.NOT_FOUND)
                if Role.MANAGER not in roles:
                    raise LedgerError(ErrorCode.FORBIDDEN)
                replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
                if replayed is not None:
                    result = WriteResult(receipt=replayed, replayed=True)
                else:
                    raise LedgerError(ErrorCode.ALREADY_EXISTS)
            else:
                if Role.MANAGER not in roles:
                    raise LedgerError(ErrorCode.FORBIDDEN)
                if actor_id not in valid_members:
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                member_roles: dict[str, tuple[Role, ...]] = {}
                for mid in valid_members:
                    m_roles = tx.actor_roles(mid)
                    if m_roles is None:
                        raise LedgerError(ErrorCode.INVALID_INPUT)
                    member_roles[mid] = m_roles
                has_pair = any(
                    m1 != m2 and Role.SUBMITTER in r1 and Role.RECEIVER in r2
                    for m1, r1 in member_roles.items()
                    for m2, r2 in member_roles.items()
                )
                if not has_pair:
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                limits = self._store.limits
                if (
                    len(valid_items) > limits.max_items_per_contract
                    or tx.contract_count() >= limits.max_contracts
                    or tx.event_count() >= limits.max_events
                ):
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                balances = tuple(
                    ItemBalance(
                        item_id=it.item_id,
                        unit=it.unit,
                        scale=it.scale,
                        authorized_ticks=it.authorized_ticks,
                        reserved_ticks=0,
                        received_ticks=0,
                        available_ticks=it.authorized_ticks,
                    )
                    for it in valid_items
                )
                deltas = tuple(
                    BalanceDelta(
                        item_id=it.item_id,
                        authorized_delta=it.authorized_ticks,
                        reserved_delta=0,
                        received_delta=0,
                    )
                    for it in valid_items
                )
                snapshot = ContractSnapshot(
                    contract_id=valid_cid,
                    version=1,
                    revision_no=1,
                    member_ids=valid_members,
                    balances=balances,
                )
                plan = _build_plan(
                    action=Action.CREATE_CONTRACT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=snapshot,
                    create_contract=True,
                    measurement=None,
                    create_measurement=False,
                    new_revision=None,
                    submission=None,
                    decision=None,
                    reversal=None,
                    deltas=deltas,
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def create_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
        lines: list[Line] | tuple[Line, ...],
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        valid_lines = _validate_lines(lines, allow_zero=False, require_positive_any=True)
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.CREATE_MEASUREMENT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={
                "lines": [{"item_id": l.item_id, "ticks": l.ticks} for l in valid_lines],
                "measurement_id": valid_mid,
            },
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.SUBMITTER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            existing_measurement = tx.measurement(valid_cid, valid_mid)
            if existing_measurement is not None and existing_measurement.owner_id != actor_id:
                raise LedgerError(ErrorCode.NOT_FOUND)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if existing_measurement is not None:
                    raise LedgerError(ErrorCode.ALREADY_EXISTS)
                contract_items = {b.item_id for b in contract.balances}
                if any(l.item_id not in contract_items for l in valid_lines):
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                limits = self._store.limits
                if (
                    tx.measurement_count(valid_cid) >= limits.max_measurements_per_contract
                    or limits.max_revisions_per_measurement < 1
                    or tx.event_count() >= limits.max_events
                ):
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=contract.version + 1,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=contract.balances,
                )
                m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=actor_id,
                    state=MeasurementState.DRAFT,
                    revision_no=1,
                )
                new_rev = MeasurementRevision(
                    revision_no=1,
                    lines=valid_lines,
                    submission=None,
                    decision=None,
                    reversal=None,
                )
                plan = _build_plan(
                    action=Action.CREATE_MEASUREMENT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=m_row,
                    create_measurement=True,
                    new_revision=new_rev,
                    submission=None,
                    decision=None,
                    reversal=None,
                    deltas=(),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def revise_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
        lines: list[Line] | tuple[Line, ...],
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        valid_lines = _validate_lines(lines, allow_zero=False, require_positive_any=True)
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.REVISE_MEASUREMENT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={
                "lines": [{"item_id": l.item_id, "ticks": l.ticks} for l in valid_lines],
                "measurement_id": valid_mid,
            },
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.SUBMITTER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            m_row, _ = _require_measurement(tx, contract, valid_mid)
            if m_row.owner_id != actor_id:
                raise LedgerError(ErrorCode.NOT_FOUND)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if m_row.state not in (MeasurementState.DRAFT, MeasurementState.RETURNED):
                    raise LedgerError(ErrorCode.STATE_CONFLICT)
                contract_items = {b.item_id for b in contract.balances}
                if any(l.item_id not in contract_items for l in valid_lines):
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                limits = self._store.limits
                rev_count = tx.revision_count(valid_cid, valid_mid)
                if rev_count != m_row.revision_no:
                    raise LedgerError(ErrorCode.CORRUPT_STORE)
                if rev_count >= limits.max_revisions_per_measurement or tx.event_count() >= limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                next_rev_no = m_row.revision_no + 1
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=contract.version + 1,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=contract.balances,
                )
                next_m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=m_row.owner_id,
                    state=MeasurementState.DRAFT,
                    revision_no=next_rev_no,
                )
                new_rev = MeasurementRevision(
                    revision_no=next_rev_no,
                    lines=valid_lines,
                    submission=None,
                    decision=None,
                    reversal=None,
                )
                plan = _build_plan(
                    action=Action.REVISE_MEASUREMENT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=next_m_row,
                    create_measurement=False,
                    new_revision=new_rev,
                    submission=None,
                    decision=None,
                    reversal=None,
                    deltas=(),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def submit_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.SUBMIT_MEASUREMENT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={"measurement_id": valid_mid},
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.SUBMITTER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            m_row, current_rev = _require_measurement(tx, contract, valid_mid)
            if m_row.owner_id != actor_id:
                raise LedgerError(ErrorCode.NOT_FOUND)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if m_row.state != MeasurementState.DRAFT:
                    raise LedgerError(ErrorCode.STATE_CONFLICT)
                line_map = {l.item_id: l.ticks for l in current_rev.lines}
                new_balances_list: list[ItemBalance] = []
                for bal in contract.balances:
                    req = line_map.get(bal.item_id, 0)
                    if req > bal.available_ticks:
                        raise LedgerError(ErrorCode.INSUFFICIENT_BALANCE)
                    new_res = bal.reserved_ticks + req
                    new_avail = bal.authorized_ticks - new_res - bal.received_ticks
                    new_balances_list.append(
                        ItemBalance(
                            item_id=bal.item_id,
                            unit=bal.unit,
                            scale=bal.scale,
                            authorized_ticks=bal.authorized_ticks,
                            reserved_ticks=new_res,
                            received_ticks=bal.received_ticks,
                            available_ticks=new_avail,
                        )
                    )
                if tx.event_count() >= self._store.limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                new_balances = tuple(new_balances_list)
                next_version = contract.version + 1
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=next_version,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=new_balances,
                )
                next_m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=m_row.owner_id,
                    state=MeasurementState.SUBMITTED,
                    revision_no=m_row.revision_no,
                )
                sub_fact = SubmissionFact(
                    version=next_version,
                    operation_id=valid_opid,
                    actor_id=actor_id,
                    contract_revision_no=contract.revision_no,
                )
                plan = _build_plan(
                    action=Action.SUBMIT_MEASUREMENT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=next_m_row,
                    create_measurement=False,
                    new_revision=None,
                    submission=sub_fact,
                    decision=None,
                    reversal=None,
                    deltas=_compute_deltas(contract.balances, new_balances),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def receive_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
        accepted_lines: list[Line] | tuple[Line, ...],
        reason: str | None = None,
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        valid_accepted = _validate_lines(accepted_lines, allow_zero=True, require_positive_any=True)
        valid_reason = _validate_reason(reason, allow_none=True)
        data: dict[str, Any] = {
            "accepted_lines": [{"item_id": l.item_id, "ticks": l.ticks} for l in valid_accepted],
            "measurement_id": valid_mid,
        }
        if valid_reason is not None:
            data["reason"] = valid_reason
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.RECEIVE_MEASUREMENT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data=data,
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.RECEIVER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            m_row, current_rev = _require_measurement(tx, contract, valid_mid)
            if m_row.owner_id == actor_id or (
                current_rev.submission is not None and current_rev.submission.actor_id == actor_id
            ):
                raise LedgerError(ErrorCode.SELF_REVIEW_FORBIDDEN)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if m_row.state != MeasurementState.SUBMITTED:
                    raise LedgerError(ErrorCode.STATE_CONFLICT)
                sub_map = {l.item_id: l.ticks for l in current_rev.lines}
                acc_map = {l.item_id: l.ticks for l in valid_accepted}
                if set(acc_map.keys()) != set(sub_map.keys()):
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                is_full = True
                for iid, sub_ticks in sub_map.items():
                    acc_ticks = acc_map[iid]
                    if acc_ticks > sub_ticks:
                        raise LedgerError(ErrorCode.INVALID_INPUT)
                    if acc_ticks < sub_ticks:
                        is_full = False
                if not is_full and valid_reason is None:
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                new_balances_list: list[ItemBalance] = []
                for bal in contract.balances:
                    sub_t = sub_map.get(bal.item_id, 0)
                    acc_t = acc_map.get(bal.item_id, 0)
                    if bal.reserved_ticks < sub_t:
                        raise LedgerError(ErrorCode.CORRUPT_STORE)
                    new_res = bal.reserved_ticks - sub_t
                    new_rec = bal.received_ticks + acc_t
                    if new_res + new_rec > bal.authorized_ticks:
                        raise LedgerError(ErrorCode.CORRUPT_STORE)
                    new_avail = bal.authorized_ticks - new_res - new_rec
                    new_balances_list.append(
                        ItemBalance(
                            item_id=bal.item_id,
                            unit=bal.unit,
                            scale=bal.scale,
                            authorized_ticks=bal.authorized_ticks,
                            reserved_ticks=new_res,
                            received_ticks=new_rec,
                            available_ticks=new_avail,
                        )
                    )
                if tx.event_count() >= self._store.limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                new_balances = tuple(new_balances_list)
                next_version = contract.version + 1
                next_state = MeasurementState.ACCEPTED if is_full else MeasurementState.ACCEPTED_PARTIAL
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=next_version,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=new_balances,
                )
                next_m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=m_row.owner_id,
                    state=next_state,
                    revision_no=m_row.revision_no,
                )
                dec_fact = DecisionFact(
                    version=next_version,
                    operation_id=valid_opid,
                    action=Action.RECEIVE_MEASUREMENT,
                    actor_id=actor_id,
                    accepted_lines=valid_accepted,
                    reason=valid_reason,
                )
                plan = _build_plan(
                    action=Action.RECEIVE_MEASUREMENT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=next_m_row,
                    create_measurement=False,
                    new_revision=None,
                    submission=None,
                    decision=dec_fact,
                    reversal=None,
                    deltas=_compute_deltas(contract.balances, new_balances),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def return_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
        reason: str,
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        valid_reason = _validate_reason(reason, allow_none=False)
        assert valid_reason is not None
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.RETURN_MEASUREMENT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={"measurement_id": valid_mid, "reason": valid_reason},
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.RECEIVER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            m_row, current_rev = _require_measurement(tx, contract, valid_mid)
            if m_row.owner_id == actor_id or (
                current_rev.submission is not None and current_rev.submission.actor_id == actor_id
            ):
                raise LedgerError(ErrorCode.SELF_REVIEW_FORBIDDEN)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if m_row.state != MeasurementState.SUBMITTED:
                    raise LedgerError(ErrorCode.STATE_CONFLICT)
                sub_map = {l.item_id: l.ticks for l in current_rev.lines}
                new_balances_list: list[ItemBalance] = []
                for bal in contract.balances:
                    sub_t = sub_map.get(bal.item_id, 0)
                    if bal.reserved_ticks < sub_t:
                        raise LedgerError(ErrorCode.CORRUPT_STORE)
                    new_res = bal.reserved_ticks - sub_t
                    new_avail = bal.authorized_ticks - new_res - bal.received_ticks
                    new_balances_list.append(
                        ItemBalance(
                            item_id=bal.item_id,
                            unit=bal.unit,
                            scale=bal.scale,
                            authorized_ticks=bal.authorized_ticks,
                            reserved_ticks=new_res,
                            received_ticks=bal.received_ticks,
                            available_ticks=new_avail,
                        )
                    )
                if tx.event_count() >= self._store.limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                new_balances = tuple(new_balances_list)
                next_version = contract.version + 1
                zero_lines = tuple(Line(item_id=l.item_id, ticks=0) for l in current_rev.lines)
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=next_version,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=new_balances,
                )
                next_m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=m_row.owner_id,
                    state=MeasurementState.RETURNED,
                    revision_no=m_row.revision_no,
                )
                dec_fact = DecisionFact(
                    version=next_version,
                    operation_id=valid_opid,
                    action=Action.RETURN_MEASUREMENT,
                    actor_id=actor_id,
                    accepted_lines=zero_lines,
                    reason=valid_reason,
                )
                plan = _build_plan(
                    action=Action.RETURN_MEASUREMENT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=next_m_row,
                    create_measurement=False,
                    new_revision=None,
                    submission=None,
                    decision=dec_fact,
                    reversal=None,
                    deltas=_compute_deltas(contract.balances, new_balances),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def amend_contract(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        new_limits: list[Line] | tuple[Line, ...],
        reason: str,
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_limits = _validate_lines(new_limits, allow_zero=True, require_positive_any=False)
        valid_reason = _validate_reason(reason, allow_none=False)
        assert valid_reason is not None
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.AMEND_CONTRACT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={
                "new_limits": [{"item_id": l.item_id, "ticks": l.ticks} for l in valid_limits],
                "reason": valid_reason,
            },
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.MANAGER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                limit_map = {l.item_id: l.ticks for l in valid_limits}
                contract_items = {b.item_id for b in contract.balances}
                if set(limit_map.keys()) != contract_items:
                    raise LedgerError(ErrorCode.INVALID_INPUT)
                new_balances_list: list[ItemBalance] = []
                for bal in contract.balances:
                    target_auth = limit_map[bal.item_id]
                    if target_auth < bal.reserved_ticks + bal.received_ticks:
                        raise LedgerError(ErrorCode.AMENDMENT_FLOOR)
                    new_avail = target_auth - bal.reserved_ticks - bal.received_ticks
                    new_balances_list.append(
                        ItemBalance(
                            item_id=bal.item_id,
                            unit=bal.unit,
                            scale=bal.scale,
                            authorized_ticks=target_auth,
                            reserved_ticks=bal.reserved_ticks,
                            received_ticks=bal.received_ticks,
                            available_ticks=new_avail,
                        )
                    )
                if tx.event_count() >= self._store.limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                new_balances = tuple(new_balances_list)
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=contract.version + 1,
                    revision_no=contract.revision_no + 1,
                    member_ids=contract.member_ids,
                    balances=new_balances,
                )
                plan = _build_plan(
                    action=Action.AMEND_CONTRACT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=None,
                    create_measurement=False,
                    new_revision=None,
                    submission=None,
                    decision=None,
                    reversal=None,
                    deltas=_compute_deltas(contract.balances, new_balances),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def reverse_receipt(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
        expected_version: int,
        measurement_id: str,
        reason: str,
    ) -> WriteResult:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        valid_ev = _validate_expected_version(expected_version, is_create_contract=False)
        valid_mid = _validate_id(measurement_id)
        valid_reason = _validate_reason(reason, allow_none=False)
        assert valid_reason is not None
        canonical_payload, payload_sha256 = _encode_canonical_command(
            action=Action.REVERSE_RECEIPT,
            actor_id=actor_id,
            contract_id=valid_cid,
            expected_version=valid_ev,
            data={"measurement_id": valid_mid, "reason": valid_reason},
        )
        with self._store.write() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if Role.MANAGER not in roles:
                raise LedgerError(ErrorCode.FORBIDDEN)
            m_row, current_rev = _require_measurement(tx, contract, valid_mid)
            if (
                m_row.owner_id == actor_id
                or (current_rev.submission is not None and current_rev.submission.actor_id == actor_id)
                or (
                    current_rev.decision is not None
                    and current_rev.decision.action == Action.RECEIVE_MEASUREMENT
                    and current_rev.decision.actor_id == actor_id
                )
            ):
                raise LedgerError(ErrorCode.SELF_REVIEW_FORBIDDEN)
            replayed = _check_idempotency(tx, valid_cid, valid_opid, canonical_payload, payload_sha256)
            if replayed is not None:
                result = WriteResult(receipt=replayed, replayed=True)
            else:
                if valid_ev != contract.version:
                    raise LedgerError(ErrorCode.STALE_VERSION, current_version=contract.version)
                if m_row.state == MeasurementState.REVERSED:
                    raise LedgerError(ErrorCode.ALREADY_REVERSED)
                if m_row.state not in (MeasurementState.ACCEPTED, MeasurementState.ACCEPTED_PARTIAL):
                    raise LedgerError(ErrorCode.STATE_CONFLICT)
                if current_rev.decision is None or current_rev.submission is None:
                    raise LedgerError(ErrorCode.CORRUPT_STORE)
                acc_map = {l.item_id: l.ticks for l in current_rev.decision.accepted_lines}
                rev_line_ids = {l.item_id for l in current_rev.lines}
                if set(acc_map.keys()) != rev_line_ids or all(v <= 0 for v in acc_map.values()):
                    raise LedgerError(ErrorCode.CORRUPT_STORE)
                new_balances_list: list[ItemBalance] = []
                for bal in contract.balances:
                    acc_t = acc_map.get(bal.item_id, 0)
                    if acc_t < 0 or bal.received_ticks < acc_t:
                        raise LedgerError(ErrorCode.CORRUPT_STORE)
                    new_rec = bal.received_ticks - acc_t
                    new_avail = bal.authorized_ticks - bal.reserved_ticks - new_rec
                    new_balances_list.append(
                        ItemBalance(
                            item_id=bal.item_id,
                            unit=bal.unit,
                            scale=bal.scale,
                            authorized_ticks=bal.authorized_ticks,
                            reserved_ticks=bal.reserved_ticks,
                            received_ticks=new_rec,
                            available_ticks=new_avail,
                        )
                    )
                if tx.event_count() >= self._store.limits.max_events:
                    raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
                new_balances = tuple(new_balances_list)
                next_version = contract.version + 1
                next_contract = ContractSnapshot(
                    contract_id=valid_cid,
                    version=next_version,
                    revision_no=contract.revision_no,
                    member_ids=contract.member_ids,
                    balances=new_balances,
                )
                next_m_row = _MeasurementRow(
                    contract_id=valid_cid,
                    measurement_id=valid_mid,
                    owner_id=m_row.owner_id,
                    state=MeasurementState.REVERSED,
                    revision_no=m_row.revision_no,
                )
                rev_fact = ReversalFact(
                    version=next_version,
                    operation_id=valid_opid,
                    actor_id=actor_id,
                    reverses_operation_id=current_rev.decision.operation_id,
                    reason=valid_reason,
                )
                plan = _build_plan(
                    action=Action.REVERSE_RECEIPT,
                    actor_id=actor_id,
                    operation_id=valid_opid,
                    canonical_payload=canonical_payload,
                    payload_sha256=payload_sha256,
                    contract=next_contract,
                    create_contract=False,
                    measurement=next_m_row,
                    create_measurement=False,
                    new_revision=None,
                    submission=None,
                    decision=None,
                    reversal=rev_fact,
                    deltas=_compute_deltas(contract.balances, new_balances),
                )
                tx.apply(plan)
                result = WriteResult(receipt=plan.operation.receipt, replayed=False)
        return result

    def read_contract(self, *, principal: Principal, contract_id: str) -> ContractSnapshot:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        with self._store.read() as tx:
            _, contract = _require_seeded_member(tx, actor_id, valid_cid)
            return contract

    def read_measurement(
        self,
        *,
        principal: Principal,
        contract_id: str,
        measurement_id: str,
    ) -> MeasurementSnapshot:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_mid = _validate_id(measurement_id)
        with self._store.read() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            m_row = tx.measurement(valid_cid, valid_mid)
            if m_row is None:
                raise LedgerError(ErrorCode.NOT_FOUND)
            is_elevated = any(r in roles for r in _ELEVATED_ROLES)
            if not is_elevated and m_row.owner_id != actor_id:
                raise LedgerError(ErrorCode.NOT_FOUND)
            revs = tx.revisions(valid_cid, valid_mid)
            if len(revs) != m_row.revision_no:
                raise LedgerError(ErrorCode.CORRUPT_STORE)
            for idx, rev in enumerate(revs, start=1):
                if rev.revision_no != idx:
                    raise LedgerError(ErrorCode.CORRUPT_STORE)
                _validate_stored_revision(contract, m_row, rev, is_current=(idx == m_row.revision_no))
            return MeasurementSnapshot(
                contract_id=valid_cid,
                measurement_id=valid_mid,
                owner_id=m_row.owner_id,
                state=m_row.state,
                revisions=revs,
            )

    def read_operation(
        self,
        *,
        principal: Principal,
        contract_id: str,
        operation_id: str,
    ) -> Receipt:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        valid_opid = _validate_id(operation_id)
        with self._store.read() as tx:
            roles, _ = _require_seeded_member(tx, actor_id, valid_cid)
            is_elevated = any(r in roles for r in _ELEVATED_ROLES)
            owner_filter = None if is_elevated else actor_id
            stored_op = tx.operation(valid_cid, valid_opid, owner_filter=owner_filter)
            if stored_op is None:
                raise LedgerError(ErrorCode.NOT_FOUND)
            if stored_op.receipt.contract_id != valid_cid or stored_op.receipt.operation_id != valid_opid:
                raise LedgerError(ErrorCode.CORRUPT_STORE)
            return stored_op.receipt

    def read_history(
        self,
        *,
        principal: Principal,
        contract_id: str,
        after_version: int = 0,
        through_version: int | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self._store.assert_owner_open()
        actor_id = _validate_principal(principal)
        valid_cid = _validate_id(contract_id)
        if type(after_version) is not int or not (0 <= after_version <= _MAX_EVENTS):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        if through_version is not None:
            if type(through_version) is not int or not (0 <= through_version <= _MAX_EVENTS):
                raise LedgerError(ErrorCode.INVALID_INPUT)
            if after_version > through_version:
                raise LedgerError(ErrorCode.INVALID_INPUT)
        if type(limit) is not int or not (1 <= limit <= 100):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        with self._store.read() as tx:
            roles, contract = _require_seeded_member(tx, actor_id, valid_cid)
            if not any(r in roles for r in _ELEVATED_ROLES):
                raise LedgerError(ErrorCode.FORBIDDEN)
            effective_through = contract.version if through_version is None else through_version
            if after_version > effective_through or effective_through > contract.version:
                raise LedgerError(ErrorCode.INVALID_INPUT)
            events = tx.history(valid_cid, after_version, effective_through, limit)
            expected_len = min(limit, effective_through - after_version)
            if len(events) != expected_len:
                raise LedgerError(ErrorCode.CORRUPT_STORE)
            for offset, ev in enumerate(events, start=1):
                if ev.version != after_version + offset:
                    raise LedgerError(ErrorCode.CORRUPT_STORE)
            next_after = events[-1].version if events else after_version
            return HistoryPage(
                through_version=effective_through,
                events=events,
                next_after=next_after,
                complete=(next_after == effective_through),
            )

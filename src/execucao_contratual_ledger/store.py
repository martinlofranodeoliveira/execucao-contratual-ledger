"""Private native SQLite persistence, written as explicit Codex support.

No business-command dispatcher, permission decision or HTTP authentication here.
The future primary domain core supplies immutable commit plans.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from .types import (
    Action, ActorSpec, BalanceDelta, ContractSnapshot, DecisionFact, ErrorCode,
    EventSnapshot, HistoryPage, ItemBalance, ItemSpec, LedgerError, Limits, Line,
    MeasurementRevision, MeasurementSnapshot, MeasurementState, Outcome, Principal,
    Receipt, ReversalFact, Role, SubmissionFact, WriteResult, _CommitPlan,
    _MeasurementRow, _StoredOperation,
)

_APP_ID = 1279478833
_VERSION = 1
_CELL_CAP = 16384
_READ_CAP = 1048576
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_LIMITS = (32, 32, 16, 256, 8, 4096)
_RECORDS = {c.__name__: c for c in (
    Principal, ActorSpec, ItemSpec, Line, Limits, ItemBalance, Receipt, WriteResult,
    ContractSnapshot, SubmissionFact, DecisionFact, ReversalFact,
    MeasurementRevision, MeasurementSnapshot, BalanceDelta, EventSnapshot,
    HistoryPage, _MeasurementRow, _StoredOperation, _CommitPlan,
)}
_ENUMS = {c.__name__: c for c in (
    Role, MeasurementState, Action, Outcome, ErrorCode,
)}
_HINTS = {c: get_type_hints(c) for c in _RECORDS.values()}

_SCHEMA = {
    "policy": """CREATE TABLE policy(id INTEGER PRIMARY KEY CHECK(id=1),
        data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384)) STRICT""",
    "actors": """CREATE TABLE actors(actor_id TEXT PRIMARY KEY,
        data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384)) STRICT""",
    "contracts": """CREATE TABLE contracts(contract_id TEXT PRIMARY KEY,
        version INTEGER NOT NULL CHECK(version BETWEEN 1 AND 4096),
        revision_no INTEGER NOT NULL CHECK(revision_no BETWEEN 1 AND 4096)) STRICT""",
    "members": """CREATE TABLE members(contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        actor_id TEXT NOT NULL REFERENCES actors(actor_id),
        PRIMARY KEY(contract_id,actor_id)) STRICT""",
    "items": """CREATE TABLE items(contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        item_id TEXT NOT NULL,unit TEXT NOT NULL,scale INTEGER NOT NULL CHECK(scale IN(1,1000)),
        authorized INTEGER NOT NULL CHECK(authorized BETWEEN 0 AND 1000000000000),
        reserved INTEGER NOT NULL CHECK(reserved BETWEEN 0 AND 1000000000000),
        received INTEGER NOT NULL CHECK(received BETWEEN 0 AND 1000000000000),
        CHECK(reserved+received<=authorized),PRIMARY KEY(contract_id,item_id)) STRICT""",
    "measurements": """CREATE TABLE measurements(contract_id TEXT NOT NULL,
        measurement_id TEXT NOT NULL,owner_id TEXT NOT NULL,state TEXT NOT NULL
        CHECK(state IN('DRAFT','SUBMITTED','RETURNED','ACCEPTED','ACCEPTED_PARTIAL','REVERSED')),
        revision_no INTEGER NOT NULL CHECK(revision_no BETWEEN 1 AND 8),
        PRIMARY KEY(contract_id,measurement_id),
        FOREIGN KEY(contract_id,owner_id) REFERENCES members(contract_id,actor_id),
        FOREIGN KEY(contract_id,measurement_id,revision_no)
        REFERENCES revisions(contract_id,measurement_id,revision_no) DEFERRABLE INITIALLY DEFERRED) STRICT""",
    "revisions": """CREATE TABLE revisions(contract_id TEXT NOT NULL,measurement_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL CHECK(revision_no BETWEEN 1 AND 8),
        data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,measurement_id,revision_no),
        FOREIGN KEY(contract_id,measurement_id) REFERENCES measurements(contract_id,measurement_id)) STRICT""",
    "submissions": """CREATE TABLE submissions(contract_id TEXT NOT NULL,measurement_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL,data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,measurement_id,revision_no),
        FOREIGN KEY(contract_id,measurement_id,revision_no) REFERENCES revisions(contract_id,measurement_id,revision_no)) STRICT""",
    "decisions": """CREATE TABLE decisions(contract_id TEXT NOT NULL,measurement_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL,data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,measurement_id,revision_no),
        FOREIGN KEY(contract_id,measurement_id,revision_no) REFERENCES revisions(contract_id,measurement_id,revision_no)) STRICT""",
    "reversals": """CREATE TABLE reversals(contract_id TEXT NOT NULL,measurement_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL,data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,measurement_id,revision_no),
        FOREIGN KEY(contract_id,measurement_id,revision_no) REFERENCES revisions(contract_id,measurement_id,revision_no)) STRICT""",
    "operations": """CREATE TABLE operations(contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        operation_id TEXT NOT NULL,event_version INTEGER NOT NULL,measurement_id TEXT,
        data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,operation_id),UNIQUE(contract_id,event_version),
        FOREIGN KEY(contract_id,measurement_id) REFERENCES measurements(contract_id,measurement_id),
        FOREIGN KEY(contract_id,event_version) REFERENCES events(contract_id,version) DEFERRABLE INITIALLY DEFERRED) STRICT""",
    "events": """CREATE TABLE events(contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        version INTEGER NOT NULL CHECK(version BETWEEN 1 AND 4096),operation_id TEXT NOT NULL,
        data TEXT NOT NULL CHECK(length(CAST(data AS BLOB)) BETWEEN 1 AND 16384),
        PRIMARY KEY(contract_id,version),UNIQUE(contract_id,operation_id),
        FOREIGN KEY(contract_id,operation_id) REFERENCES operations(contract_id,operation_id)) STRICT""",
}


def _error(code, outcome=Outcome.NO_EFFECT):
    return LedgerError(code, outcome)


def _conforms(value, hint):
    origin = get_origin(hint)
    if origin is UnionType:
        return any(_conforms(value, h) for h in get_args(hint))
    if origin is tuple:
        args = get_args(hint)
        return (type(value) is tuple and len(args) == 2 and args[1] is Ellipsis
                and all(_conforms(v, args[0]) for v in value))
    if hint in _HINTS:
        return type(value) is hint and all(
            _conforms(getattr(value, f), t) for f, t in _HINTS[hint].items())
    return type(value) is hint


def _shape(value):
    if type(value) not in _HINTS or not _conforms(value, type(value)):
        raise ValueError("record shape")
    for f in fields(value):
        v = getattr(value, f.name)
        if v is None:
            continue
        if f.name.endswith("_id") or f.name == "reverses_operation_id":
            if type(v) is not str or _ID.fullmatch(v) is None:
                raise ValueError("identifier")
        if type(v) is str:
            size = len(v.encode("utf-8", "strict"))
            if size > _CELL_CAP:
                raise ValueError("text size")
            if f.name == "reason" and (not v.strip() or not 1 <= size <= 256
                    or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in v)):
                raise ValueError("reason")
            if f.name == "unit" and (len(v) > 32 or _ID.fullmatch(v) is None):
                raise ValueError("unit")
            if f.name == "payload_sha256" and re.fullmatch(r"[0-9a-f]{64}", v) is None:
                raise ValueError("digest")
        if type(v) is int:
            if f.name == "scale" and v not in (1, 1000):
                raise ValueError("scale")
            if f.name.endswith("_ticks") or f.name == "ticks":
                if not 0 <= v <= 10**12:
                    raise ValueError("quantity")
            if f.name.endswith("_delta") and not -10**12 <= v <= 10**12:
                raise ValueError("delta")
            if f.name in ("version", "committed_version", "contract_revision_no"):
                if not 1 <= v <= 4096:
                    raise ValueError("version")
            if f.name in ("revision_no", "measurement_revision_no"):
                cap = 4096 if type(value) is ContractSnapshot else 8
                if not 1 <= v <= cap:
                    raise ValueError("revision")
        if type(v) in _HINTS:
            _shape(v)
        if type(v) is tuple:
            if len(v) > 256:
                raise ValueError("tuple bound")
            for cell in v:
                if type(cell) in _HINTS:
                    _shape(cell)
    if type(value) is Limits:
        if any(not 0 <= getattr(value, f.name) <= cap
               for f, cap in zip(fields(value), _LIMITS)):
            raise ValueError("limits")
    if type(value) is ActorSpec:
        if not value.roles or len(value.roles) > 4 or len(set(value.roles)) != len(value.roles):
            raise ValueError("roles")
    if type(value) is ItemBalance:
        if (value.available_ticks != value.authorized_ticks-value.reserved_ticks-value.received_ticks
                or value.available_ticks < 0):
            raise ValueError("balance")
    if type(value) in (ContractSnapshot, Receipt):
        ids = tuple(b.item_id for b in value.balances)
        if not 1 <= len(ids) <= 16 or ids != tuple(sorted(set(ids))):
            raise ValueError("item order")
    if type(value) is ContractSnapshot:
        if not 1 <= len(value.member_ids) <= 32 or value.member_ids != tuple(sorted(set(value.member_ids))):
            raise ValueError("member order")
    if type(value) is MeasurementRevision:
        ids = tuple(x.item_id for x in value.lines)
        if not 1 <= len(ids) <= 16 or ids != tuple(sorted(set(ids))) or any(x.ticks == 0 for x in value.lines):
            raise ValueError("revision lines")
    if type(value) in (Receipt, EventSnapshot):
        contract_action = value.action in (Action.CREATE_CONTRACT, Action.AMEND_CONTRACT)
        if contract_action != (value.measurement_id is None) or (value.measurement_id is None) != (value.measurement_revision_no is None):
            raise ValueError("measurement metadata")
        if type(value) is Receipt and (value.measurement_id is None) != (value.measurement_state is None):
            raise ValueError("measurement state")
    if type(value) is EventSnapshot:
        ids = tuple(x.item_id for x in value.deltas)
        if len(ids) > 16 or ids != tuple(sorted(set(ids))) or any(
                x.authorized_delta == x.reserved_delta == x.received_delta == 0 for x in value.deltas):
            raise ValueError("delta order")


def _pack(value):
    if type(value) in _ENUMS.values():
        return {"$enum": type(value).__name__, "value": value.value}
    if type(value) in _HINTS:
        return {"$record": type(value).__name__,
                "fields": {f.name: _pack(getattr(value, f.name)) for f in fields(value)}}
    if type(value) is tuple:
        return [_pack(v) for v in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("unsupported value")


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate key")
        result[k] = v
    return result


def _load(text):
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))


def _unpack(value):
    if type(value) is list:
        return tuple(_unpack(v) for v in value)
    if type(value) is dict:
        if set(value) == {"$enum", "value"} and value["$enum"] in _ENUMS:
            return _ENUMS[value["$enum"]](value["value"])
        if set(value) == {"$record", "fields"} and value["$record"] in _RECORDS:
            cls = _RECORDS[value["$record"]]
            raw = value["fields"]
            if type(raw) is dict and set(raw) == set(_HINTS[cls]):
                return cls(**{k: _unpack(v) for k, v in raw.items()})
        raise ValueError("unknown record")
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError("unsupported primitive")


def _encode(value):
    try:
        _shape(value)
        data = json.dumps(_pack(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error(ErrorCode.STORAGE_ERROR) from None
    if len(data.encode("utf-8")) > _CELL_CAP:
        raise _error(ErrorCode.LIMIT_EXCEEDED)
    return data


def _decode(data, cls):
    try:
        if type(data) is not str or len(data.encode("utf-8")) > _CELL_CAP:
            raise ValueError("text")
        obj = _unpack(_load(data))
        if type(obj) is not cls:
            raise ValueError("class")
        _shape(obj)
        return obj
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise _error(ErrorCode.CORRUPT_STORE) from None


def _path(path, timeout_ms):
    try:
        if type(path) is not str or not path or "\0" in path or len(path.encode("utf-8")) > 4096:
            raise ValueError("path")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 5000:
            raise ValueError("timeout")
        return Path(os.path.abspath(path))
    except (ValueError, UnicodeError):
        raise _error(ErrorCode.INVALID_INPUT) from None


def _connection(path, timeout_ms):
    c = sqlite3.connect(path.as_uri()+"?mode=rw", uri=True,
                        timeout=timeout_ms/1000, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA read_uncommitted=OFF")
        c.execute("PRAGMA busy_timeout="+str(timeout_ms))
        if c.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise _error(ErrorCode.INCOMPATIBLE_STORE)
        return c
    except BaseException:
        c.close()
        raise


class _Store:
    def __init__(self, connection, limits):
        self._conn = connection
        self._thread = threading.current_thread()
        self._closed = False
        self._active = False
        self.limits = limits

    @classmethod
    def create(cls, path, actors, limits, timeout_ms):
        path = _path(path, timeout_ms)
        try:
            if type(limits) is not Limits:
                raise ValueError("limits")
            _shape(limits)
        except (ValueError, TypeError):
            raise _error(ErrorCode.INVALID_LIMIT) from None
        try:
            if type(actors) is not tuple or not 1 <= len(actors) <= 32:
                raise ValueError("actors")
            for actor in actors:
                if type(actor) is not ActorSpec:
                    raise ValueError("actor")
                _shape(actor)
            if len({a.actor_id for a in actors}) != len(actors) or not any(
                    Role.MANAGER in a.roles for a in actors):
                raise ValueError("bootstrap")
        except (ValueError, TypeError, UnicodeError):
            raise _error(ErrorCode.INVALID_INPUT) from None
        if len(actors) > limits.max_actors:
            raise _error(ErrorCode.LIMIT_EXCEEDED)
        # Prepare all finite metadata BEFORE exclusive file creation.
        seed = [(a.actor_id, _encode(a)) for a in actors]
        policy = _encode(limits)
        try:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise _error(ErrorCode.ALREADY_EXISTS) from None
        except OSError:
            raise _error(ErrorCode.STORAGE_ERROR) from None
        identity = os.fstat(fd)
        os.close(fd)
        store = None
        try:
            c = _connection(path, timeout_ms)
            store = cls(c, limits)
            if c.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise _error(ErrorCode.STORAGE_ERROR)
            with store.write():
                for ddl in _SCHEMA.values():
                    c.execute(ddl)
                c.execute("PRAGMA application_id="+str(_APP_ID))
                c.execute("PRAGMA user_version="+str(_VERSION))
                c.execute("INSERT INTO policy VALUES(1,?)", (policy,))
                c.executemany("INSERT INTO actors VALUES(?,?)", seed)
            return store
        except BaseException as error:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
            known = isinstance(error, LedgerError) and error.outcome is Outcome.NO_EFFECT
            if store is None and isinstance(error, (sqlite3.Error, OSError)):
                known = True
            if known:
                try:
                    now = os.stat(path, follow_symlinks=False)
                    if (now.st_dev, now.st_ino) == (identity.st_dev, identity.st_ino) and now.st_nlink == 1 and stat.S_ISREG(now.st_mode):
                        os.unlink(path)
                    else:
                        known = False
                except OSError:
                    known = False
            if not isinstance(error, Exception):
                raise
            if not known:
                raise _error(ErrorCode.COMMIT_UNCERTAIN, Outcome.UNKNOWN) from None
            if isinstance(error, LedgerError):
                raise
            raise _error(ErrorCode.STORAGE_ERROR) from None

    @classmethod
    def open(cls, path, timeout_ms):
        path = _path(path, timeout_ms)
        try:
            st = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                raise _error(ErrorCode.INCOMPATIBLE_STORE)
            c = _connection(path, timeout_ms)
        except FileNotFoundError:
            raise _error(ErrorCode.NOT_FOUND) from None
        except (OSError, sqlite3.Error):
            raise _error(ErrorCode.STORAGE_ERROR) from None
        store = cls(c, Limits())
        try:
            with store.read() as tx:
                tx._streaming = True
                store._audit(tx)
            return store
        except BaseException:
            store._poison()
            raise

    def assert_owner_open(self):
        if threading.current_thread() is not self._thread:
            raise _error(ErrorCode.WRONG_THREAD)
        if self._closed:
            raise _error(ErrorCode.CLOSED)

    def _poison(self):
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            pass

    def close(self):
        if threading.current_thread() is not self._thread:
            raise _error(ErrorCode.WRONG_THREAD)
        if self._closed:
            return
        if self._conn.in_transaction:
            if not self._rollback():
                self._poison()
                raise _error(ErrorCode.COMMIT_UNCERTAIN, Outcome.UNKNOWN)
        try:
            self._conn.close()
        except Exception:
            self._closed = True
            raise _error(ErrorCode.STORAGE_ERROR) from None
        self._closed = True

    def _rollback(self):
        try:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            return not self._conn.in_transaction
        except Exception:
            return False

    @contextmanager
    def _transaction(self, write):
        self.assert_owner_open()
        if self._active or self._conn.in_transaction:
            raise _error(ErrorCode.STORAGE_ERROR)
        try:
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        except BaseException as error:
            if self._conn.in_transaction and not self._rollback():
                self._poison()
                if isinstance(error, Exception):
                    raise _error(ErrorCode.COMMIT_UNCERTAIN, Outcome.UNKNOWN) from None
            if not isinstance(error, Exception):
                raise
            if isinstance(error, sqlite3.Error) and getattr(error, "sqlite_errorcode", 0) & 255 == sqlite3.SQLITE_BUSY:
                raise _error(ErrorCode.BUSY) from None
            raise _error(ErrorCode.STORAGE_ERROR) from None
        self._active = True
        tx = _Tx(self, write)
        try:
            try:
                yield tx
            except BaseException as error:
                if not self._rollback():
                    self._poison()
                    if isinstance(error, Exception):
                        raise _error(ErrorCode.COMMIT_UNCERTAIN, Outcome.UNKNOWN) from None
                if not isinstance(error, Exception) or isinstance(error, LedgerError):
                    raise
                raise _error(ErrorCode.STORAGE_ERROR) from None
            try:
                self._conn.execute("COMMIT")
            except BaseException as error:
                known = self._conn.in_transaction and self._rollback()
                if not known:
                    self._poison()
                if not isinstance(error, Exception):
                    raise
                if known:
                    raise _error(ErrorCode.STORAGE_ERROR) from None
                raise _error(ErrorCode.COMMIT_UNCERTAIN, Outcome.UNKNOWN) from None
        finally:
            tx._valid = False
            self._active = False

    def read(self):
        return self._transaction(False)

    def write(self):
        return self._transaction(True)

    def _audit(self, tx):
        c = self._conn
        if (c.execute("PRAGMA application_id").fetchone() != (_APP_ID,)
                or c.execute("PRAGMA user_version").fetchone() != (_VERSION,)
                or c.execute("PRAGMA journal_mode").fetchone() != ("delete",)):
            raise _error(ErrorCode.INCOMPATIBLE_STORE)
        entries = tx._q("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name", (), 32)
        if {name: sql for kind, name, sql in entries if kind == "table"} != _SCHEMA or any(
                kind != "table" for kind, _, _ in entries):
            raise _error(ErrorCode.INCOMPATIBLE_STORE)
        if c.execute("PRAGMA quick_check(1)").fetchone() != ("ok",) or c.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _error(ErrorCode.CORRUPT_STORE)
        policy = tx._q("SELECT id,data FROM policy", (), 1)
        if len(policy) != 1 or policy[0][0] != 1:
            raise _error(ErrorCode.CORRUPT_STORE)
        self.limits = _decode(policy[0][1], Limits)
        actors = tx._q("SELECT actor_id,data FROM actors ORDER BY actor_id", (), 32)
        if not 1 <= len(actors) <= self.limits.max_actors:
            raise _error(ErrorCode.CORRUPT_STORE)
        seeded = []
        for actor_id, data in actors:
            actor = _decode(data, ActorSpec)
            if actor.actor_id != actor_id:
                raise _error(ErrorCode.CORRUPT_STORE)
            seeded.append(actor)
        if not any(Role.MANAGER in a.roles for a in seeded):
            raise _error(ErrorCode.CORRUPT_STORE)
        contracts = tx._q("SELECT contract_id FROM contracts ORDER BY contract_id", (), 32)
        if len(contracts) > self.limits.max_contracts or tx.event_count() > self.limits.max_events:
            raise _error(ErrorCode.CORRUPT_STORE)
        for (contract_id,) in contracts:
            self._audit_contract(tx, contract_id)

    def _audit_contract(self, tx, contract_id):
        header = tx.contract(contract_id)
        if not header.member_ids or not 1 <= len(header.balances) <= self.limits.max_items_per_contract:
            raise _error(ErrorCode.CORRUPT_STORE)
        totals = {b.item_id: [0, 0, 0] for b in header.balances}
        units = {b.item_id: (b.unit, b.scale) for b in header.balances}
        if tx._number("SELECT count(*) AS n FROM events WHERE contract_id=?", (contract_id,)) != header.version:
            raise _error(ErrorCode.CORRUPT_STORE)
        if tx._number("SELECT count(*) AS n FROM operations WHERE contract_id=?", (contract_id,)) != header.version:
            raise _error(ErrorCode.CORRUPT_STORE)
        revision = 1
        for start in range(0, header.version, 16):
            events = tx.history(contract_id, start, header.version, 16)
            if len(events) != min(16, header.version-start):
                raise _error(ErrorCode.CORRUPT_STORE)
            for expected, event in enumerate(events, start+1):
                if event.version != expected:
                    raise _error(ErrorCode.CORRUPT_STORE)
                if expected == 1 and event.action is not Action.CREATE_CONTRACT:
                    raise _error(ErrorCode.CORRUPT_STORE)
                if expected > 1 and event.action is Action.CREATE_CONTRACT:
                    raise _error(ErrorCode.CORRUPT_STORE)
                if event.action is Action.AMEND_CONTRACT:
                    revision += 1
                if event.contract_revision_no != revision or event.actor_id not in header.member_ids:
                    raise _error(ErrorCode.CORRUPT_STORE)
                operation = tx.operation(contract_id, event.operation_id)
                if operation is None:
                    raise _error(ErrorCode.CORRUPT_STORE)
                receipt = operation.receipt
                if (receipt.committed_version, receipt.action, receipt.actor_id,
                    receipt.contract_revision_no, receipt.measurement_id,
                    receipt.measurement_revision_no) != (
                    event.version, event.action, event.actor_id, event.contract_revision_no,
                    event.measurement_id, event.measurement_revision_no
                ) or event.command_json != operation.canonical_payload:
                    raise _error(ErrorCode.CORRUPT_STORE)
                seen = set()
                for d in event.deltas:
                    if d.item_id not in totals or d.item_id in seen:
                        raise _error(ErrorCode.CORRUPT_STORE)
                    seen.add(d.item_id)
                    totals[d.item_id] = [a+b for a, b in zip(totals[d.item_id],
                        (d.authorized_delta, d.reserved_delta, d.received_delta))]
                expected_balances = []
                for item_id, (authorized, reserved, received) in sorted(totals.items()):
                    unit, scale = units[item_id]
                    if not 0 <= reserved+received <= authorized <= 10**12 or min(reserved, received) < 0:
                        raise _error(ErrorCode.CORRUPT_STORE)
                    expected_balances.append(ItemBalance(item_id, unit, scale, authorized,
                                                        reserved, received, authorized-reserved-received))
                if receipt.balances != tuple(expected_balances):
                    raise _error(ErrorCode.CORRUPT_STORE)
        if revision != header.revision_no or tuple(expected_balances) != header.balances:
            raise _error(ErrorCode.CORRUPT_STORE)
        rows = tx._q("SELECT measurement_id FROM measurements WHERE contract_id=? ORDER BY measurement_id", (contract_id,), 256)
        if len(rows) > self.limits.max_measurements_per_contract:
            raise _error(ErrorCode.CORRUPT_STORE)
        reserved = {b.item_id: 0 for b in header.balances}
        received = dict(reserved)
        for (measurement_id,) in rows:
            m = tx.measurement(contract_id, measurement_id)
            revisions = tx.revisions(contract_id, measurement_id)
            if not 1 <= len(revisions) == m.revision_no <= self.limits.max_revisions_per_measurement:
                raise _error(ErrorCode.CORRUPT_STORE)
            for index, r in enumerate(revisions, 1):
                if r.revision_no != index or not r.lines or len(r.lines) > len(units):
                    raise _error(ErrorCode.CORRUPT_STORE)
                line_ids = tuple(line.item_id for line in r.lines)
                if line_ids != tuple(sorted(set(line_ids))) or any(
                        line.item_id not in units or line.ticks <= 0 for line in r.lines):
                    raise _error(ErrorCode.CORRUPT_STORE)
                for fact in (r.submission, r.decision, r.reversal):
                    if fact is not None:
                        op = tx.operation(contract_id, fact.operation_id)
                        action = (Action.SUBMIT_MEASUREMENT if type(fact) is SubmissionFact
                            else Action.REVERSE_RECEIPT if type(fact) is ReversalFact
                            else fact.action)
                        if op is None or (op.receipt.measurement_id, op.receipt.measurement_revision_no,
                            op.receipt.committed_version, op.receipt.actor_id, op.receipt.action) != (
                            measurement_id, index, fact.version, fact.actor_id, action):
                            raise _error(ErrorCode.CORRUPT_STORE)
                        if type(fact) is SubmissionFact and (op.receipt.measurement_state is not MeasurementState.SUBMITTED
                                or op.receipt.contract_revision_no != fact.contract_revision_no):
                            raise _error(ErrorCode.CORRUPT_STORE)
                        if type(fact) is ReversalFact and op.receipt.measurement_state is not MeasurementState.REVERSED:
                            raise _error(ErrorCode.CORRUPT_STORE)
                if r.submission is not None and r.submission.actor_id != m.owner_id:
                    raise _error(ErrorCode.CORRUPT_STORE)
                if r.decision is not None:
                    if r.submission is None or r.decision.version <= r.submission.version:
                        raise _error(ErrorCode.CORRUPT_STORE)
                    if r.decision.actor_id == m.owner_id:
                        raise _error(ErrorCode.CORRUPT_STORE)
                    if tuple(x.item_id for x in r.decision.accepted_lines) != line_ids or any(
                            not 0 <= a.ticks <= line.ticks for line, a in zip(r.lines, r.decision.accepted_lines)):
                        raise _error(ErrorCode.CORRUPT_STORE)
                    if r.decision.action not in (Action.RECEIVE_MEASUREMENT, Action.RETURN_MEASUREMENT):
                        raise _error(ErrorCode.CORRUPT_STORE)
                    accepted = r.decision.accepted_lines
                    if ((r.decision.action is Action.RETURN_MEASUREMENT and any(x.ticks != 0 for x in accepted))
                            or (r.decision.action is Action.RECEIVE_MEASUREMENT and not any(x.ticks > 0 for x in accepted))):
                        raise _error(ErrorCode.CORRUPT_STORE)
                    full = all(a.ticks == b.ticks for a, b in zip(accepted, r.lines))
                    decision_state = (MeasurementState.RETURNED if r.decision.action is Action.RETURN_MEASUREMENT
                        else MeasurementState.ACCEPTED if full else MeasurementState.ACCEPTED_PARTIAL)
                    if tx.operation(contract_id, r.decision.operation_id).receipt.measurement_state is not decision_state:
                        raise _error(ErrorCode.CORRUPT_STORE)
                    if (r.decision.action is Action.RETURN_MEASUREMENT or not full) and r.decision.reason is None:
                        raise _error(ErrorCode.CORRUPT_STORE)
                if r.reversal is not None and (r.decision is None
                        or r.decision.action is not Action.RECEIVE_MEASUREMENT
                        or r.reversal.reverses_operation_id != r.decision.operation_id
                        or r.reversal.version <= r.decision.version
                        or r.reversal.actor_id in (m.owner_id, r.decision.actor_id)):
                    raise _error(ErrorCode.CORRUPT_STORE)
                if index < len(revisions) and (r.reversal is not None or (
                        r.decision is not None and r.decision.action is not Action.RETURN_MEASUREMENT)
                        or (r.submission is not None and r.decision is None)):
                    raise _error(ErrorCode.CORRUPT_STORE)
            r = revisions[-1]
            if m.state is MeasurementState.DRAFT:
                valid = r.submission is r.decision is r.reversal is None
            elif m.state is MeasurementState.SUBMITTED:
                valid = r.submission is not None and r.decision is r.reversal is None
                for line in r.lines:
                    reserved[line.item_id] += line.ticks
            else:
                valid = r.submission is not None and r.decision is not None
                if valid and m.state is MeasurementState.RETURNED:
                    valid = r.decision.action is Action.RETURN_MEASUREMENT and all(x.ticks == 0 for x in r.decision.accepted_lines) and r.reversal is None
                elif valid:
                    valid = r.decision.action is Action.RECEIVE_MEASUREMENT and any(x.ticks > 0 for x in r.decision.accepted_lines)
                    full = all(a.ticks == b.ticks for a, b in zip(r.decision.accepted_lines, r.lines))
                    valid = valid and ((m.state is MeasurementState.REVERSED and r.reversal is not None)
                        or (m.state is MeasurementState.ACCEPTED and full and r.reversal is None)
                        or (m.state is MeasurementState.ACCEPTED_PARTIAL and not full and r.reversal is None))
                    if r.reversal is None:
                        for line in r.decision.accepted_lines:
                            received[line.item_id] += line.ticks
            if not valid:
                raise _error(ErrorCode.CORRUPT_STORE)
        if any((reserved[b.item_id], received[b.item_id]) != (b.reserved_ticks, b.received_ticks) for b in header.balances):
            raise _error(ErrorCode.CORRUPT_STORE)


class _Tx:
    def __init__(self, store, write):
        self._store = store
        self._write = write
        self._valid = True
        self._used = 0
        self._streaming = False

    def _check(self):
        self._store.assert_owner_open()
        if not self._valid:
            raise _error(ErrorCode.CLOSED)

    def _q(self, sql, parameters, row_cap):
        self._check()
        c = self._store._conn
        description = c.execute("SELECT * FROM ("+sql+") LIMIT 0", parameters).description
        names = ['"'+x[0].replace('"', '""')+'"' for x in description]
        sizes = ["CASE typeof("+n+") WHEN 'null' THEN 0 WHEN 'integer' THEN 8 WHEN 'real' THEN 8 ELSE length(CAST("+n+" AS BLOB)) END" for n in names]
        total = "+".join(sizes)
        maxcell = "max("+",".join(sizes+["0"])+")"
        count, size, cell = c.execute("SELECT count(*),coalesce(sum("+total+"),0),coalesce(max("+maxcell+"),0) FROM ("+sql+")", parameters).fetchone()
        if count > row_cap or cell > _CELL_CAP:
            raise _error(ErrorCode.CORRUPT_STORE)
        if size > _READ_CAP or (not self._streaming and self._used+size > _READ_CAP):
            raise _error(ErrorCode.LIMIT_EXCEEDED)
        if not self._streaming:
            self._used += size
        return c.execute(sql, parameters).fetchall()

    def _number(self, sql, parameters=()):
        result = self._q(sql, parameters, 1)
        if len(result) != 1 or type(result[0][0]) is not int or result[0][0] < 0:
            raise _error(ErrorCode.CORRUPT_STORE)
        return result[0][0]

    def actor_roles(self, actor_id):
        rows = self._q("SELECT data FROM actors WHERE actor_id=?", (actor_id,), 1)
        if not rows:
            return None
        actor = _decode(rows[0][0], ActorSpec)
        if actor.actor_id != actor_id:
            raise _error(ErrorCode.CORRUPT_STORE)
        return actor.roles

    def contract(self, contract_id):
        rows = self._q("SELECT version,revision_no FROM contracts WHERE contract_id=?", (contract_id,), 1)
        if not rows:
            return None
        members = self._q("SELECT actor_id FROM members WHERE contract_id=? ORDER BY actor_id", (contract_id,), 32)
        items = self._q("SELECT item_id,unit,scale,authorized,reserved,received FROM items WHERE contract_id=? ORDER BY item_id", (contract_id,), 16)
        result = ContractSnapshot(contract_id, *rows[0], tuple(r[0] for r in members),
            tuple(ItemBalance(*r, r[3]-r[4]-r[5]) for r in items))
        try:
            _shape(result)
        except (ValueError, TypeError, UnicodeError):
            raise _error(ErrorCode.CORRUPT_STORE) from None
        return result

    def measurement(self, contract_id, measurement_id):
        rows = self._q("SELECT owner_id,state,revision_no FROM measurements WHERE contract_id=? AND measurement_id=?", (contract_id, measurement_id), 1)
        if not rows:
            return None
        try:
            result = _MeasurementRow(contract_id, measurement_id, rows[0][0], MeasurementState(rows[0][1]), rows[0][2])
            _shape(result)
            return result
        except (ValueError, TypeError, UnicodeError):
            raise _error(ErrorCode.CORRUPT_STORE) from None

    def _revision(self, contract_id, measurement_id, revision_no):
        key = (contract_id, measurement_id, revision_no)
        rows = self._q("SELECT data FROM revisions WHERE contract_id=? AND measurement_id=? AND revision_no=?", key, 1)
        if not rows:
            return None
        r = _decode(rows[0][0], MeasurementRevision)
        if r.revision_no != revision_no or r.submission is not None or r.decision is not None or r.reversal is not None:
            raise _error(ErrorCode.CORRUPT_STORE)
        facts = []
        for table, cls in (("submissions", SubmissionFact), ("decisions", DecisionFact), ("reversals", ReversalFact)):
            rows = self._q("SELECT data FROM "+table+" WHERE contract_id=? AND measurement_id=? AND revision_no=?", key, 1)
            facts.append(_decode(rows[0][0], cls) if rows else None)
        return MeasurementRevision(r.revision_no, r.lines, *facts)

    def current_revision(self, contract_id, measurement_id):
        m = self.measurement(contract_id, measurement_id)
        return self._revision(contract_id, measurement_id, m.revision_no) if m else None

    def revisions(self, contract_id, measurement_id):
        rows = self._q("SELECT revision_no FROM revisions WHERE contract_id=? AND measurement_id=? ORDER BY revision_no", (contract_id, measurement_id), 8)
        return tuple(self._revision(contract_id, measurement_id, r[0]) for r in rows)

    def operation(self, contract_id, operation_id, owner_filter=None):
        if owner_filter is None:
            rows = self._q("SELECT data,event_version,measurement_id FROM operations WHERE contract_id=? AND operation_id=?", (contract_id, operation_id), 1)
        else:
            rows = self._q("SELECT o.data,o.event_version,o.measurement_id FROM operations AS o JOIN measurements AS m ON m.contract_id=o.contract_id AND m.measurement_id=o.measurement_id WHERE o.contract_id=? AND o.operation_id=? AND m.owner_id=?", (contract_id, operation_id, owner_filter), 1)
        if not rows:
            return None
        op = _decode(rows[0][0], _StoredOperation)
        receipt = op.receipt
        try:
            payload = _load(op.canonical_payload)
            if (type(payload) is not dict or set(payload) != {"action", "actor_id", "contract_id", "expected_version", "data"}
                    or type(payload["data"]) is not dict
                    or json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) != op.canonical_payload
                    or hashlib.sha256(op.canonical_payload.encode("utf-8")).hexdigest() != op.payload_sha256
                    or (receipt.contract_id, receipt.operation_id, receipt.committed_version, receipt.measurement_id) != (contract_id, operation_id, rows[0][1], rows[0][2])
                    or payload["action"] != receipt.action.value or payload["actor_id"] != receipt.actor_id
                    or payload["contract_id"] != contract_id or type(payload["expected_version"]) is not int
                    or payload["expected_version"]+1 != receipt.committed_version):
                raise ValueError("operation")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _error(ErrorCode.CORRUPT_STORE) from None
        return op

    def contract_count(self):
        return self._number("SELECT count(*) AS n FROM contracts")

    def measurement_count(self, contract_id):
        return self._number("SELECT count(*) AS n FROM measurements WHERE contract_id=?", (contract_id,))

    def revision_count(self, contract_id, measurement_id):
        return self._number("SELECT count(*) AS n FROM revisions WHERE contract_id=? AND measurement_id=?", (contract_id, measurement_id))

    def event_count(self):
        return self._number("SELECT count(*) AS n FROM events")

    def history(self, contract_id, after, through, limit):
        if any(type(x) is not int for x in (after, through, limit)) or not 0 <= after <= through <= 4096 or not 1 <= limit <= 100:
            raise _error(ErrorCode.STORAGE_ERROR)
        rows = self._q("SELECT version,operation_id,data FROM events WHERE contract_id=? AND version>? AND version<=? ORDER BY version LIMIT ?", (contract_id, after, through, limit), 100)
        result = []
        for version, op_id, data in rows:
            event = _decode(data, EventSnapshot)
            if event.version != version or event.operation_id != op_id:
                raise _error(ErrorCode.CORRUPT_STORE)
            result.append(event)
        return tuple(result)

    def apply(self, plan):
        self._check()
        if not self._write or type(plan) is not _CommitPlan:
            raise _error(ErrorCode.STORAGE_ERROR)
        try:
            _shape(plan)
        except (ValueError, TypeError, UnicodeError):
            raise _error(ErrorCode.STORAGE_ERROR) from None
        h, m, e, op = plan.contract, plan.measurement, plan.event, plan.operation
        if (h.version, h.revision_no, e.operation_id, e.actor_id, e.action,
            e.measurement_id, e.measurement_revision_no) != (
            op.receipt.committed_version, op.receipt.contract_revision_no,
            op.receipt.operation_id, op.receipt.actor_id, op.receipt.action,
            op.receipt.measurement_id, op.receipt.measurement_revision_no
        ) or e.version != h.version or e.contract_revision_no != h.revision_no or op.receipt.contract_id != h.contract_id or op.receipt.balances != h.balances or e.command_json != op.canonical_payload:
            raise _error(ErrorCode.STORAGE_ERROR)
        if (m is None) != (e.measurement_id is None) or (m is not None and (
                m.contract_id != h.contract_id or m.measurement_id != e.measurement_id
                or m.revision_no != e.measurement_revision_no
                or op.receipt.measurement_state is not m.state)) or (m is None and op.receipt.measurement_state is not None):
            raise _error(ErrorCode.STORAGE_ERROR)
        if plan.create_measurement and m is None:
            raise _error(ErrorCode.STORAGE_ERROR)
        if plan.new_revision is not None and (m is None
                or plan.new_revision.revision_no != m.revision_no
                or any(x is not None for x in (plan.new_revision.submission,
                    plan.new_revision.decision, plan.new_revision.reversal))):
            raise _error(ErrorCode.STORAGE_ERROR)
        # Encode/size-admit all facts before the first SQL mutation.
        operation_data, event_data = _encode(op), _encode(e)
        facts = [(table, _encode(value)) for table, value in (
            ("revisions", plan.new_revision), ("submissions", plan.submission),
            ("decisions", plan.decision), ("reversals", plan.reversal)) if value is not None]
        c = self._store._conn
        if plan.create_contract:
            if h.version != 1 or h.revision_no != 1:
                raise _error(ErrorCode.STORAGE_ERROR)
            c.execute("INSERT INTO contracts VALUES(?,?,?)", (h.contract_id, h.version, h.revision_no))
            c.executemany("INSERT INTO members VALUES(?,?)", [(h.contract_id, actor) for actor in h.member_ids])
            c.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?)", [(h.contract_id, b.item_id, b.unit, b.scale, b.authorized_ticks, b.reserved_ticks, b.received_ticks) for b in h.balances])
        else:
            old = self.contract(h.contract_id)
            if old is None or old.member_ids != h.member_ids or tuple(
                    (b.item_id, b.unit, b.scale) for b in old.balances) != tuple(
                    (b.item_id, b.unit, b.scale) for b in h.balances):
                raise _error(ErrorCode.STORAGE_ERROR)
            if c.execute("UPDATE contracts SET version=?,revision_no=? WHERE contract_id=? AND version=?", (h.version, h.revision_no, h.contract_id, h.version-1)).rowcount != 1:
                raise _error(ErrorCode.STORAGE_ERROR)
            for b in h.balances:
                if c.execute("UPDATE items SET authorized=?,reserved=?,received=? WHERE contract_id=? AND item_id=? AND unit=? AND scale=?", (b.authorized_ticks, b.reserved_ticks, b.received_ticks, h.contract_id, b.item_id, b.unit, b.scale)).rowcount != 1:
                    raise _error(ErrorCode.STORAGE_ERROR)
        if m is not None:
            if plan.create_measurement:
                c.execute("INSERT INTO measurements VALUES(?,?,?,?,?)", (m.contract_id, m.measurement_id, m.owner_id, m.state.value, m.revision_no))
            elif c.execute("UPDATE measurements SET state=?,revision_no=? WHERE contract_id=? AND measurement_id=? AND owner_id=?", (m.state.value, m.revision_no, m.contract_id, m.measurement_id, m.owner_id)).rowcount != 1:
                raise _error(ErrorCode.STORAGE_ERROR)
        if facts and m is None:
            raise _error(ErrorCode.STORAGE_ERROR)
        for table, data in facts:
            c.execute("INSERT INTO "+table+" VALUES(?,?,?,?)", (m.contract_id, m.measurement_id, m.revision_no, data))
        c.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (h.contract_id, op.receipt.operation_id, h.version, op.receipt.measurement_id, operation_data))
        c.execute("INSERT INTO events VALUES(?,?,?,?)", (h.contract_id, h.version, e.operation_id, event_data))

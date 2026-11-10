"""Closed JSON adapter shapes; Codex support, no identity from request data."""
from __future__ import annotations
from dataclasses import fields
import json
from .types import (
    Action, BalanceDelta, ContractSnapshot, DecisionFact, ErrorCode, EventSnapshot,
    HistoryPage, ItemBalance, ItemSpec, LedgerError, Line, MeasurementRevision,
    MeasurementSnapshot, MeasurementState, Outcome, Receipt, ReversalFact, Role,
    SubmissionFact, WriteResult,
)
MAX_BODY = 65536
MAX_OUTPUT = 1048576
_RECORDS = (WriteResult, Receipt, ContractSnapshot, MeasurementSnapshot,
    MeasurementRevision, SubmissionFact, DecisionFact, ReversalFact, EventSnapshot,
    BalanceDelta, ItemBalance, Line, HistoryPage)
_ENUMS = (Action, MeasurementState, Role, ErrorCode, Outcome)
_FIELDS = {
    Action.CREATE_CONTRACT: ({'items','member_ids'}, set()),
    Action.CREATE_MEASUREMENT: ({'measurement_id','lines'}, set()),
    Action.REVISE_MEASUREMENT: ({'measurement_id','lines'}, set()),
    Action.SUBMIT_MEASUREMENT: ({'measurement_id'}, set()),
    Action.RECEIVE_MEASUREMENT: ({'measurement_id','accepted_lines'}, {'reason'}),
    Action.RETURN_MEASUREMENT: ({'measurement_id','reason'}, set()),
    Action.AMEND_CONTRACT: ({'new_limits','reason'}, set()),
    Action.REVERSE_RECEIPT: ({'measurement_id','reason'}, set()),
}


def _invalid():
    return LedgerError(ErrorCode.INVALID_INPUT)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def load_json(data):
    if type(data) is not bytes or len(data) > MAX_BODY:
        raise _invalid()
    try:
        return json.loads(data.decode('utf-8','strict'),object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


def _objects(value, keys, maximum, cls):
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise _invalid()
    result = []
    for cell in value:
        if type(cell) is not dict or set(cell) != keys:
            raise _invalid()
        result.append(cls(**cell))
    return tuple(result)


def command_arguments(action, body):
    if type(action) is not Action or type(body) is not dict:
        raise _invalid()
    required, optional = _FIELDS[action]
    base = {'contract_id','operation_id','expected_version'}
    if not base|required <= set(body) or not set(body) <= base|required|optional:
        raise _invalid()
    result = dict(body)
    for name in ('lines','accepted_lines','new_limits'):
        if name in result:
            result[name] = _objects(result[name], {'item_id','ticks'},16,Line)
    if 'items' in result:
        result['items'] = _objects(result['items'],
            {'item_id','unit','scale','authorized_ticks'},16,ItemSpec)
    if 'member_ids' in result:
        members = result['member_ids']
        if type(members) is not list or not 1 <= len(members) <= 32:
            raise _invalid()
        result['member_ids'] = tuple(members)
    return result


def invoke(ledger, principal, action, body):
    arguments = command_arguments(action,body)
    # Action is an exact closed enum, never an arbitrary attribute from JSON.
    return getattr(ledger,action.value)(principal=principal,**arguments)


def _value(value, depth=0):
    if depth > 8:
        raise ValueError('wire depth')
    kind = type(value)
    if kind in _ENUMS:
        return value.value
    if value is None or kind in (str,int,bool):
        return value
    if kind is tuple and len(value) <= 256:
        return [_value(v,depth+1) for v in value]
    if kind in _RECORDS:
        return {f.name:_value(getattr(value,f.name),depth+1) for f in fields(value)}
    raise ValueError('unsupported response shape')


def encode_result(value):
    if type(value) not in _RECORDS:
        raise ValueError('unsupported response record')
    data = json.dumps({'result':_value(value)},sort_keys=True,
        separators=(',',':'),ensure_ascii=True).encode('utf-8')
    if len(data) > MAX_OUTPUT:
        raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
    return data


def encode_error(error):
    if type(error) is not LedgerError or type(error.code) is not ErrorCode or type(error.outcome) is not Outcome:
        raise ValueError('invalid error metadata')
    version = error.current_version
    if version is not None and (type(version) is not int or not 1 <= version <= 4096 or error.code is not ErrorCode.STALE_VERSION):
        raise ValueError('invalid error metadata')
    if error.code is ErrorCode.COMMIT_UNCERTAIN and error.outcome is not Outcome.UNKNOWN:
        raise ValueError('invalid error outcome')
    return json.dumps({'error':{'code':error.code.value,'outcome':error.outcome.value,
        'current_version':version}},sort_keys=True,separators=(',',':')).encode('ascii')

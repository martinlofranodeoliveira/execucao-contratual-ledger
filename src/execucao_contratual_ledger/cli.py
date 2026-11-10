"""Trusted local CLI and synthetic HTTP demonstration; Codex support."""
from __future__ import annotations
import argparse
import json
import os
import sys
from . import __version__
from .core import Ledger
from .demo import run_demo
from .types import ActorSpec, Action, ErrorCode, LedgerError, Limits, Outcome, Principal, Role
from .wire import MAX_BODY, MAX_OUTPUT, encode_error, encode_result, invoke, load_json


class _Parser(argparse.ArgumentParser):
    def error(self,message):
        # argparse's original message may echo arbitrary input values/paths.
        raise LedgerError(ErrorCode.INVALID_INPUT)


def _parser():
    parser=_Parser(prog='execucao-contratual-ledger',description='Synthetic contract execution ledger: quantities, revisions and real SQLite/HTTP.')
    parser.add_argument('--version',action='version',version=__version__)
    commands=parser.add_subparsers(dest='command',required=True)
    demo=commands.add_parser('demo',help='Run the real loopback HTTP synthetic workflow; credentials stay in memory.')
    demo.add_argument('--assert',dest='assert_demo',action='store_true',help='Explicit always-asserting demo; assertions are active with or without this flag.')
    create=commands.add_parser('init',help='Create an exclusive database from trusted bootstrap JSON on stdin.')
    create.add_argument('database')
    execute=commands.add_parser('execute',help='Run one bounded JSON command as an explicitly trusted local SDK actor.')
    execute.add_argument('database');execute.add_argument('--actor',required=True)
    read=commands.add_parser('read',help='Read an authorized immutable snapshot.')
    read.add_argument('database');read.add_argument('--actor',required=True);read.add_argument('--contract',required=True)
    view=read.add_mutually_exclusive_group()
    view.add_argument('--measurement');view.add_argument('--operation');view.add_argument('--history',action='store_true')
    read.add_argument('--after',type=int,default=0);read.add_argument('--through',type=int);read.add_argument('--limit',type=int,default=100)
    return parser


def _input():
    data=bytearray()
    while True:
        chunk=sys.stdin.buffer.read(MAX_BODY+1-len(data))
        if type(chunk) is not bytes:raise OSError('input unavailable')
        if not chunk:break
        data.extend(chunk)
        if len(data)>MAX_BODY:raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
    return load_json(bytes(data))


def _write(data):
    if type(data) is not bytes or len(data)>MAX_OUTPUT:raise LedgerError(ErrorCode.LIMIT_EXCEEDED)
    try:
        sys.stdout.buffer.write(data+b'\n')
        sys.stdout.flush()
    except (OSError,ValueError):
        _quiet_failed(sys.stdout)
        raise


def _quiet_failed(stream):
    # Replace only the failed standard FD so CPython shutdown cannot override4
    # with120 when flushing buffered output. Not a rollback or atomic stdout claim.
    try:
        target=stream.fileno();sink=os.open(os.devnull,os.O_WRONLY)
        try:os.dup2(sink,target)
        finally:os.close(sink)
    except (OSError,ValueError,AttributeError):pass


def _flush():
    good=True
    for stream in (sys.stdout,sys.stderr):
        try:stream.flush()
        except (OSError,ValueError):good=False;_quiet_failed(stream)
    return good


def _error_output(error):
    try:sys.stderr.buffer.write(encode_error(error)+b'\n')
    except (OSError,ValueError):_quiet_failed(sys.stderr)


def _code(error):
    if error.code in (ErrorCode.INVALID_INPUT,ErrorCode.INVALID_LIMIT):return 2
    if error.code in (ErrorCode.BUSY,ErrorCode.CLOSED,ErrorCode.WRONG_THREAD,ErrorCode.STORAGE_ERROR,
        ErrorCode.INCOMPATIBLE_STORE,ErrorCode.CORRUPT_STORE,ErrorCode.COMMIT_UNCERTAIN):return 4
    return 3


def _bootstrap(body):
    if type(body) is not dict or 'actors' not in body or not set(body)<={'actors','limits'}:raise LedgerError(ErrorCode.INVALID_INPUT)
    actors=body['actors']
    if type(actors) is not list or not 1<=len(actors)<=32:raise LedgerError(ErrorCode.INVALID_INPUT)
    result=[]
    for actor in actors:
        if type(actor) is not dict or set(actor)!={'actor_id','roles'}:raise LedgerError(ErrorCode.INVALID_INPUT)
        roles=actor['roles']
        if type(roles) is not list or not 1<=len(roles)<=4 or any(type(r) is not str for r in roles):raise LedgerError(ErrorCode.INVALID_INPUT)
        try:roles=tuple(Role(r) for r in roles)
        except ValueError:raise LedgerError(ErrorCode.INVALID_INPUT) from None
        result.append(ActorSpec(actor['actor_id'],roles))
    limits=body.get('limits',{})
    names={'max_actors','max_contracts','max_items_per_contract','max_measurements_per_contract','max_revisions_per_measurement','max_events'}
    if type(limits) is not dict or not set(limits)<=names:raise LedgerError(ErrorCode.INVALID_LIMIT)
    return tuple(result),Limits(**limits)


def _dispatch(arguments):
    if arguments.command=='demo':
        summary=run_demo()
        _write(json.dumps(summary,sort_keys=True,separators=(',',':'),allow_nan=False).encode('ascii'));return
    if arguments.command=='init':
        actors,limits=_bootstrap(_input());ledger=Ledger.create(arguments.database,actors,limits=limits)
        try:ledger.close();_write(b'{"created":true}')
        except (OSError,ValueError,LedgerError):raise LedgerError(ErrorCode.COMMIT_UNCERTAIN,Outcome.UNKNOWN) from None
        return
    body=_input() if arguments.command=='execute' else None
    principal=Principal(arguments.actor)
    ledger=Ledger.open(arguments.database)
    try:
        if arguments.command=='execute':
            if type(body) is not dict or 'action' not in body or type(body['action']) is not str:raise LedgerError(ErrorCode.INVALID_INPUT)
            try:action=Action(body['action'])
            except ValueError:raise LedgerError(ErrorCode.INVALID_INPUT) from None
            payload={k:v for k,v in body.items() if k!='action'}
            result=invoke(ledger,principal,action,payload)
            try:_write(encode_result(result))
            except (OSError,ValueError,LedgerError):
                # Result exists only after native context COMMIT; response loss
                # cannot undo it. Exact operation ID can be reconciled/retried.
                raise LedgerError(ErrorCode.COMMIT_UNCERTAIN,Outcome.UNKNOWN) from None
        else:
            options={'principal':principal,'contract_id':arguments.contract}
            if arguments.measurement is not None:value=ledger.read_measurement(**options,measurement_id=arguments.measurement)
            elif arguments.operation is not None:value=ledger.read_operation(**options,operation_id=arguments.operation)
            elif arguments.history:value=ledger.read_history(**options,after_version=arguments.after,through_version=arguments.through,limit=arguments.limit)
            else:value=ledger.read_contract(**options)
            _write(encode_result(value))
    finally:ledger.close()


def main(argv=None):
    code=0
    try:
        try:arguments=_parser().parse_args(argv)
        except SystemExit as exit:
            # Only argparse's controlled help/version exit is consumed.
            code=exit.code if type(exit.code) is int else 2
        else:_dispatch(arguments)
    except LedgerError as error:
        code=_code(error);_error_output(error)
    except Exception:
        code=4;_error_output(LedgerError(ErrorCode.STORAGE_ERROR))
    finally:
        if not _flush():code=4
    return code


if __name__=='__main__':
    raise SystemExit(main())

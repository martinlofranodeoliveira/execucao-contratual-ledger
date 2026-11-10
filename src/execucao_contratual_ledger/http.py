"""Bounded loopback HTTP adapter with ephemeral synthetic authority.

Codex support. Tokens stay in the owning harness/host process. This adapter is a
reproducible local demonstration, not a production identity-provider assertion.
"""
from __future__ import annotations
from collections import Counter
import hmac
import re
import secrets
import socket
import socketserver
import threading
from http import HTTPStatus
from .core import Ledger
from .types import Action, ErrorCode, LedgerError, Outcome, Principal
from .wire import MAX_BODY, encode_error, encode_result, command_arguments, load_json

_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,63}')
_TOKEN = re.compile(r'Bearer ([0-9a-f]{64})')
_HEADER = re.compile(rb'[!#$%&\x27*+.^_`|~0-9A-Za-z-]{1,64}')
_CONFLICT = {ErrorCode.ALREADY_EXISTS,ErrorCode.STALE_VERSION,ErrorCode.STATE_CONFLICT,
    ErrorCode.IDEMPOTENCY_CONFLICT,ErrorCode.INSUFFICIENT_BALANCE,
    ErrorCode.AMENDMENT_FLOOR,ErrorCode.ALREADY_REVERSED}


class SyntheticAuthority:
    """Fresh 256-bit tokens for explicitly trusted synthetic actor IDs, in memory."""
    def __init__(self, actor_ids):
        if type(actor_ids) not in (tuple,list) or not 1 <= len(actor_ids) <= 32:
            raise LedgerError(ErrorCode.INVALID_INPUT)
        if any(type(v) is not str or _ID.fullmatch(v) is None for v in actor_ids) or len(set(actor_ids)) != len(actor_ids):
            raise LedgerError(ErrorCode.INVALID_INPUT)
        self._tokens = {}
        for actor_id in actor_ids:
            for _ in range(3):
                token = secrets.token_hex(32)
                if token not in self._tokens.values():
                    self._tokens[actor_id] = token
                    break
            else:
                raise LedgerError(ErrorCode.STORAGE_ERROR)

    def _bearer_for_owned_client(self, actor_id):
        # Internal demo/test accessor, never a route, log, file or CLI output.
        return 'Bearer '+self._tokens[actor_id]

    def resolve(self, header):
        if type(header) is not str:
            return None
        match = _TOKEN.fullmatch(header)
        if match is None:
            return None
        token = match.group(1)
        found = None
        for actor_id, expected in self._tokens.items():
            if hmac.compare_digest(token,expected):
                found = actor_id
        return Principal(found) if found is not None else None


def _status(error):
    if error.code in (ErrorCode.INVALID_INPUT,ErrorCode.INVALID_LIMIT):return 400
    if error.code is ErrorCode.NOT_FOUND:return 404
    if error.code in (ErrorCode.FORBIDDEN,ErrorCode.SELF_REVIEW_FORBIDDEN):return 403
    if error.code in _CONFLICT:return 409
    if error.code is ErrorCode.LIMIT_EXCEEDED:return 413
    return 503


def _fixed_error(code=ErrorCode.INVALID_INPUT):
    return encode_error(LedgerError(code))


class _Admission(Exception):
    def __init__(self,status):self.status=status


class _Handler(socketserver.StreamRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def _request(self):
        line = self.rfile.readline(1026)
        if len(line)>1024:raise _Admission(414)
        if not line.endswith(b'\r\n'):raise _Admission(400)
        parts = line[:-2].split(b' ')
        if len(parts)!=3 or parts[0] not in (b'GET',b'POST') or parts[2] not in (b'HTTP/1.0',b'HTTP/1.1'):
            raise _Admission(400)
        try:path = parts[1].decode('ascii')
        except UnicodeError:raise _Admission(400) from None
        if not path.startswith('/') or any(ord(c)<33 or ord(c)>126 for c in path):raise _Admission(400)
        headers={};total=0
        for _ in range(33):
            raw=self.rfile.readline(4097);total+=len(raw)
            if total>8192 or len(raw)>4096:raise _Admission(431)
            if raw==b'\r\n':break
            if not raw.endswith(b'\r\n') or b':' not in raw:raise _Admission(400)
            key,value=raw[:-2].split(b':',1)
            if _HEADER.fullmatch(key) is None:raise _Admission(400)
            try:key=key.decode('ascii').lower();value=value.decode('ascii').strip(' \t')
            except UnicodeError:raise _Admission(400) from None
            if key in headers or any((ord(c)<32 and c!='\t') or ord(c)>126 for c in value):raise _Admission(400)
            headers[key]=value
        else:raise _Admission(431)
        if len(headers)>32 or 'transfer-encoding' in headers or 'expect' in headers:raise _Admission(400)
        principal=self.server.authority.resolve(headers.get('authorization'))
        if principal is None:raise _Admission(401)
        length=headers.get('content-length','0')
        if re.fullmatch(r'[0-9]{1,10}',length) is None:raise _Admission(400)
        length=int(length)
        if length>MAX_BODY:raise _Admission(413)
        if parts[0]==b'GET' and length:raise _Admission(400)
        if parts[0]==b'POST' and headers.get('content-type')!='application/json':raise _Admission(400)
        body=self.rfile.read(length)
        if type(body) is not bytes or len(body)!=length:raise _Admission(400)
        return parts[0].decode(),path,principal,body

    def _send(self,status,body):
        header=('HTTP/1.1 '+str(status)+' '+HTTPStatus(status).phrase+'\r\n'
            'Content-Type: application/json\r\nX-Content-Type-Options: nosniff\r\n'
            'Connection: close\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode('ascii')
        try:self.connection.sendall(header+body)
        except OSError:self.server.record('output_failures')
        self.server.record('http_'+str(status))

    def handle(self):
        committed=False
        try:
            method,path,principal,body=self._request()
            if method=='POST':
                prefix='/v1/commands/'
                if not path.startswith(prefix) or '/' in path[len(prefix):] or '?' in path:raise _Admission(404)
                try:action=Action(path[len(prefix):])
                except ValueError:raise _Admission(404) from None
                arguments=command_arguments(action,load_json(body))
            with_close=None
            try:
                ledger=Ledger.open(self.server.db_path,timeout_ms=1000);with_close=ledger
                if method=='POST':
                    value=getattr(ledger,action.value)(principal=principal,**arguments)
                    committed=not value.replayed
                    self.server.record('replayed_writes' if value.replayed else 'committed_writes')
                    response=encode_result(value);status=200 if value.replayed else 201
                else:
                    value=self._read(ledger,principal,path)
                    response=encode_result(value);status=200
            finally:
                if with_close is not None:with_close.close()
        except _Admission as error:
            status=error.status;response=_fixed_error(ErrorCode.NOT_FOUND if status==404 else ErrorCode.INVALID_INPUT)
        except LedgerError as error:
            if committed:error=LedgerError(ErrorCode.COMMIT_UNCERTAIN,Outcome.UNKNOWN)
            status=_status(error);response=encode_error(error)
        except Exception:
            error=LedgerError(ErrorCode.COMMIT_UNCERTAIN,Outcome.UNKNOWN) if committed else LedgerError(ErrorCode.STORAGE_ERROR)
            status=503;response=encode_error(error)
        self._send(status,response)

    def _read(self,ledger,principal,path):
        route,mark,query=path.partition('?');parts=route.split('/')
        if len(parts)<4 or parts[1:3]!=['v1','contracts'] or _ID.fullmatch(parts[3]) is None:raise _Admission(404)
        cid=parts[3]
        if len(parts)==4 and not mark:return ledger.read_contract(principal=principal,contract_id=cid)
        if len(parts)==6 and not mark and parts[4] in ('measurements','operations') and _ID.fullmatch(parts[5]) is not None:
            if parts[4]=='measurements':return ledger.read_measurement(principal=principal,contract_id=cid,measurement_id=parts[5])
            return ledger.read_operation(principal=principal,contract_id=cid,operation_id=parts[5])
        if len(parts)==5 and parts[4]=='history':
            arguments={}
            if mark:
                for cell in query.split('&'):
                    key,eq,value=cell.partition('=')
                    if not eq or key not in ('after_version','through_version','limit') or key in arguments or re.fullmatch(r'[0-9]{1,4}',value) is None:raise _Admission(400)
                    arguments[key]=int(value)
            return ledger.read_history(principal=principal,contract_id=cid,**arguments)
        raise _Admission(404)


class _Server(socketserver.ThreadingMixIn,socketserver.TCPServer):
    allow_reuse_address=False
    daemon_threads=False
    block_on_close=True
    request_queue_size=8
    def __init__(self,db_path,authority):
        self.db_path=db_path;self.authority=authority
        self._workers=threading.BoundedSemaphore(4);self._lock=threading.Lock();self._stats=Counter()
        super().__init__(('127.0.0.1',0),_Handler)
    def record(self,name):
        with self._lock:self._stats[name]=min(self._stats[name]+1,2**63-1)
    def process_request(self,request,client_address):
        if not self._workers.acquire(blocking=False):
            try:
                body=_fixed_error(ErrorCode.BUSY)
                request.settimeout(2)
                request.sendall(('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\n\r\n').encode('ascii')+body)
            except OSError:pass
            finally:self.shutdown_request(request)
            self.record('capacity_rejections');return
        try:super().process_request(request,client_address)
        except BaseException:self._workers.release();raise
    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self._workers.release()
    def handle_error(self,request,client_address):
        # No traceback, raw request, path or Authorization logging.
        self.record('handler_failures')


class LedgerHTTPService:
    """Owned loopback lifecycle; authority configuration is trusted host state."""
    def __init__(self,db_path,authority):
        if type(authority) is not SyntheticAuthority:raise LedgerError(ErrorCode.INVALID_INPUT)
        probe=Ledger.open(db_path);probe.close()
        self._server=_Server(db_path,authority);self._thread=None;self._closed=False
    @property
    def address(self):return self._server.server_address
    def start(self):
        if self._closed or self._thread is not None:raise LedgerError(ErrorCode.CLOSED)
        self._thread=threading.Thread(target=self._server.serve_forever,kwargs={'poll_interval':0.05},name='owned-ledger-http')
        self._thread.start();return self
    def close(self):
        if self._closed:return
        self._closed=True
        if self._thread is not None:
            self._server.shutdown();self._thread.join(timeout=5)
            if self._thread.is_alive():raise LedgerError(ErrorCode.STORAGE_ERROR,Outcome.UNKNOWN)
        self._server.server_close()
    def statistics(self):
        with self._server._lock:return dict(self._server._stats)
    def __enter__(self):return self.start()
    def __exit__(self,*args):self.close()

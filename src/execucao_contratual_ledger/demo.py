"""Reproducible real-TCP synthetic workflow. No token values leave this harness."""
from __future__ import annotations
from collections import Counter
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import threading
import time
from .core import Ledger
from .http import LedgerHTTPService, SyntheticAuthority
from .types import ActorSpec, Principal, Role

ACTORS=(ActorSpec('aud',(Role.AUDITOR,)),ActorSpec('dual',(Role.RECEIVER,Role.SUBMITTER)),
    ActorSpec('mgr',(Role.MANAGER,)),ActorSpec('other',(Role.SUBMITTER,)),
    ActorSpec('recv',(Role.RECEIVER,)),ActorSpec('req',(Role.SUBMITTER,)))
MEMBERS=['aud','dual','mgr','recv','req']


def _require(condition):
    if not condition:raise RuntimeError('synthetic demonstration invariant failed')


def _call(service,authority,actor,method,path,body=None,*,invalid_token=False):
    headers={}
    if actor is not None:
        invalid=next(format(i,'064x') for i in range(33) if format(i,'064x') not in authority._tokens.values())
        headers['Authorization']='Bearer '+invalid if invalid_token else authority._bearer_for_owned_client(actor)
    data=None
    if body is not None:
        data=json.dumps(body,sort_keys=True,separators=(',',':')).encode('utf-8')
        headers['Content-Type']='application/json'
    connection=http.client.HTTPConnection(*service.address,timeout=5)
    try:
        connection.request(method,path,body=data,headers=headers)
        response=connection.getresponse();payload=response.read(1048577)
        _require(len(payload)<=1048576)
        return response.status,json.loads(payload)
    finally:connection.close()


def _write(service,authority,actor,action,version,operation,**data):
    return _call(service,authority,actor,'POST','/v1/commands/'+action,
        {'contract_id':'c','operation_id':operation,'expected_version':version,**data})


def _raw_counts(path):
    c=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,isolation_level=None)
    try:return c.execute('SELECT count(*) FROM events').fetchone()[0],c.execute('SELECT count(*) FROM operations').fetchone()[0]
    finally:c.close()


def discard_committed_response(service,authority,path,body):
    """Send a real TCP command, observe commit independently, discard unread ACK.

    The server may successfully buffer the response. This is not a claim that
    sendall failed, COMMIT raised, or any transaction was rolled back.
    """
    encoded=json.dumps(body,sort_keys=True,separators=(',',':')).encode('ascii')
    headers=('POST /v1/commands/amend_contract HTTP/1.1\r\nHost: 127.0.0.1\r\n'
        'Authorization: '+authority._bearer_for_owned_client('mgr')+'\r\n'
        'Content-Type: application/json\r\nContent-Length: '+str(len(encoded))+'\r\nConnection: close\r\n\r\n').encode('ascii')
    client=socket.create_connection(service.address,timeout=5)
    observer=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,isolation_level=None)
    try:
        client.sendall(headers+encoded)
        deadline=time.monotonic()+5
        while True:
            row=observer.execute('SELECT event_version FROM operations WHERE contract_id=? AND operation_id=?',(body['contract_id'],body['operation_id'])).fetchone()
            if row is not None:break
            if time.monotonic()>=deadline:raise RuntimeError('commit witness deadline')
            threading.Event().wait(0.005)
        # No recv/getresponse is performed: zero response bytes consumed by client.
        client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
        return row[0]
    finally:observer.close();client.close()


def run_demo():
    statuses=Counter()
    def status(expected,result):
        actual,payload=result;statuses[str(actual)]+=1;_require(actual==expected);return payload
    with tempfile.TemporaryDirectory(prefix='execution-contract-demo-') as directory:
        path=Path(directory)/'owned.sqlite'
        ledger=Ledger.create(str(path),ACTORS);ledger.close()
        authority=SyntheticAuthority(tuple(a.actor_id for a in ACTORS))
        with LedgerHTTPService(str(path),authority) as service:
            def write(expected,actor,action,version,operation,**data):
                return status(expected,_write(service,authority,actor,action,version,operation,**data))
            write(201,'mgr','create_contract',0,'create',items=[{'item_id':'i','unit':'un','scale':1,'authorized_ticks':100}],member_ids=MEMBERS)
            write(201,'req','create_measurement',1,'draft1',measurement_id='m1',lines=[{'item_id':'i','ticks':70}])
            saved=write(201,'req','submit_measurement',2,'submit1',measurement_id='m1')['result']['receipt']
            write(201,'recv','receive_measurement',3,'receive1',measurement_id='m1',accepted_lines=[{'item_id':'i','ticks':60}],reason='Synthetic ten units returned')
            replay=write(200,'req','submit_measurement',2,'submit1',measurement_id='m1')['result']
            _require(replay['replayed'] and replay['receipt']==saved)
            write(409,'req','submit_measurement',4,'submit1',measurement_id='m1')
            write(403,'aud','amend_contract',4,'denied_auditor',new_limits=[{'item_id':'i','ticks':100}],reason='Synthetic denial')
            status(404,_call(service,authority,'other','GET','/v1/contracts/c'))
            status(401,_call(service,authority,None,'GET','/v1/contracts/c'))
            status(401,_call(service,authority,'req','GET','/v1/contracts/c',invalid_token=True))
            write(400,'req','submit_measurement',4,'actor_override',measurement_id='m1',actor_id='mgr')
            write(201,'dual','create_measurement',4,'draft_dual',measurement_id='m_dual',lines=[{'item_id':'i','ticks':10}])
            write(201,'dual','submit_measurement',5,'submit_dual',measurement_id='m_dual')
            write(403,'dual','receive_measurement',6,'denied_self',measurement_id='m_dual',accepted_lines=[{'item_id':'i','ticks':10}])
            write(201,'recv','return_measurement',6,'return_dual',measurement_id='m_dual',reason='Synthetic independent return')
            write(201,'req','create_measurement',7,'draft2',measurement_id='m2',lines=[{'item_id':'i','ticks':30}])
            write(201,'req','create_measurement',8,'draft3',measurement_id='m3',lines=[{'item_id':'i','ticks':30}])
            barrier=threading.Barrier(2);results=[];errors=[];lock=threading.Lock()
            def submit(mid):
                try:
                    barrier.wait(timeout=3)
                    value=_write(service,authority,'req','submit_measurement',9,'submit_'+mid,measurement_id=mid)
                    with lock:results.append((mid,*value))
                except Exception:
                    with lock:errors.append('operational client failure')
            threads=[threading.Thread(target=submit,args=(mid,)) for mid in ('m2','m3')]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=6);_require(not thread.is_alive())
            _require(not errors and sorted(r[1] for r in results)==[201,409])
            for _,code,_ in results:statuses[str(code)]+=1
            winner=next(mid for mid,code,_ in results if code==201)
            loser=next(payload for _,code,payload in results if code==409)
            _require(loser['error']['code']=='STALE_VERSION')
            before=_raw_counts(path)
            blocker=sqlite3.connect(path,isolation_level=None);blocker.execute('BEGIN IMMEDIATE')
            try:busy=write(503,'mgr','amend_contract',10,'busy',new_limits=[{'item_id':'i','ticks':100}],reason='Synthetic held lock')
            finally:blocker.execute('ROLLBACK');blocker.close()
            _require(busy['error']['code']=='BUSY' and _raw_counts(path)==before)
            write(409,'mgr','amend_contract',10,'floor89',new_limits=[{'item_id':'i','ticks':89}],reason='Synthetic floor')
            write(201,'mgr','amend_contract',10,'floor90',new_limits=[{'item_id':'i','ticks':90}],reason='Synthetic floor')
            write(201,'mgr','reverse_receipt',11,'reverse1',measurement_id='m1',reason='Synthetic correction')
            write(201,'recv','return_measurement',12,'return_winner',measurement_id=winner,reason='Synthetic whole return')
            body={'contract_id':'c','operation_id':'discarded_response','expected_version':13,'new_limits':[{'item_id':'i','ticks':90}],'reason':'Synthetic unread response'}
            _require(discard_committed_response(service,authority,str(path),body)==14)
            _require(_raw_counts(path)==(14,14))
            recovered=status(200,_call(service,authority,'mgr','POST','/v1/commands/amend_contract',body))['result']
            _require(recovered['replayed'] and recovered['receipt']['committed_version']==14 and _raw_counts(path)==(14,14))
            page=status(200,_call(service,authority,'aud','GET','/v1/contracts/c/history?through_version=14&limit=100'))['result']
            _require(page['complete'] and page['next_after']==14 and [e['version'] for e in page['events']]==list(range(1,15)))
            folded=[0,0,0]
            for event in page['events']:
                for delta in event['deltas']:
                    folded=[x+delta[k] for x,k in zip(folded,('authorized_delta','reserved_delta','received_delta'))]
            _require(folded==[90,0,0])
        # Store remains committed after HTTP lifecycle closes; new credentials fresh.
        current=Ledger.open(str(path))
        try:
            snapshot=current.read_contract(principal=Principal('aud'),contract_id='c')
            _require(snapshot.version==14 and snapshot.balances[0].available_ticks==90)
        finally:current.close()
        fresh=SyntheticAuthority(tuple(a.actor_id for a in ACTORS))
        with LedgerHTTPService(str(path),fresh) as restarted:
            status(401,_call(restarted,authority,'mgr','GET','/v1/contracts/c'))
            status(200,_call(restarted,fresh,'mgr','GET','/v1/contracts/c'))
        return {'status':'PASS_SYNTHETIC_HTTP_DEMO','http_status_counts':dict(sorted(statuses.items())),
            'events':14,'operations':14,'aggregate_version':14,
            'final_item':{'authorized_ticks':90,'reserved_ticks':0,'received_ticks':0,'available_ticks':90},
            'concurrent_submit_codes':[201,409],'concurrent_winner_identity':'unspecified',
            'discarded_response':{'client_response_bytes_consumed':0,'commit_witness_version':14,'exact_retry_replayed':True},
            'reopen_preserved':True,'credentials_regenerated':True,'tokens_exported':False}

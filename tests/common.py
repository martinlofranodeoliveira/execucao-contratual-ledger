"""Independent public-SDK fixtures; no application source calculations imported."""
from pathlib import Path
import json,sqlite3,tempfile
from execucao_contratual_ledger.core import Ledger
from execucao_contratual_ledger.types import (
    Principal,ActorSpec,Role,ItemSpec,Line,Limits,LedgerError,Outcome,WriteResult,
)
ACTORS=(ActorSpec('aud',(Role.AUDITOR,)),ActorSpec('dual',(Role.RECEIVER,Role.SUBMITTER)),ActorSpec('mgr',(Role.MANAGER,)),ActorSpec('other',(Role.SUBMITTER,)),ActorSpec('recv',(Role.RECEIVER,)),ActorSpec('req',(Role.SUBMITTER,)))
MEMBERS=('aud','dual','mgr','recv','req')
TABLES=('policy','actors','contracts','members','items','measurements','revisions','submissions','decisions','reversals','operations','events')

def readonly_snapshot(path):
    connection=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True)
    try:return tuple((table,tuple(sorted(connection.execute('SELECT * FROM '+table).fetchall(),key=lambda r:json.dumps(r)))) for table in TABLES)
    finally:connection.close()

def balance(snapshot,item='i'):
    b=next(v for v in snapshot.balances if v.item_id==item)
    return b.authorized_ticks,b.reserved_ticks,b.received_ticks,b.available_ticks

class Scenario:
    def __init__(self,case,*,limits=None,actors=ACTORS,members=MEMBERS,items=None):
        self.case=case;self.temp=tempfile.TemporaryDirectory(prefix='sdk-independent-')
        self.path=Path(self.temp.name)/'owned?#%.sqlite';self.version=0;self.serial=0
        self.members=members;self.items=items or (ItemSpec('i','un',1,100),)
        try:self.ledger=Ledger.create(str(self.path),actors,limits=limits or Limits())
        except BaseException:self.temp.cleanup();raise
    def __enter__(self):return self
    def __exit__(self,*args):
        try:self.ledger.close()
        finally:self.temp.cleanup()
    def invoke(self,method,*,actor='mgr',op=None,expected=None,**data):
        return getattr(self.ledger,method)(principal=Principal(actor),contract_id='c',operation_id=op or 'op'+str(self.serial+1),expected_version=self.version if expected is None else expected,**data)
    def run(self,method,*,actor='mgr',**data):
        self.serial+=1;old=self.version
        result=self.invoke(method,actor=actor,op='op'+str(self.serial),**data)
        self.case.assertIs(type(result),WriteResult);self.case.assertFalse(result.replayed)
        self.case.assertEqual(result.receipt.committed_version,old+1)
        self.case.assertEqual((result.receipt.contract_id,result.receipt.action.value,result.receipt.actor_id),('c',method,actor))
        self.version=old+1;return result
    def create(self):return self.run('create_contract',items=self.items,member_ids=self.members)
    def draft(self,ticks=70,*,mid='m1',actor='req',lines=None):
        return self.run('create_measurement',actor=actor,measurement_id=mid,lines=lines if lines is not None else (Line('i',ticks),))
    def submit(self,*,mid='m1',actor='req'):
        return self.run('submit_measurement',actor=actor,measurement_id=mid)
    def receive(self,ticks=60,*,mid='m1',actor='recv',reason='Synthetic partial receipt'):
        return self.run('receive_measurement',actor=actor,measurement_id=mid,accepted_lines=(Line('i',ticks),),reason=reason)
    def contract(self):return self.ledger.read_contract(principal=Principal('mgr'),contract_id='c')
    def failure(self,code,method,*,actor='mgr',expected=None,**data):
        before=readonly_snapshot(self.path)
        with self.case.assertRaises(LedgerError) as caught:self.invoke(method,actor=actor,expected=expected,**data)
        self.case.assertIs(caught.exception.code,code);self.case.assertIs(caught.exception.outcome,Outcome.NO_EFFECT)
        self.case.assertEqual(str(caught.exception),code.value)
        self.case.assertEqual(readonly_snapshot(self.path),before)
        return caught.exception

def refusal(case,scenario,code,call):
    before=readonly_snapshot(scenario.path)
    with case.assertRaises(LedgerError) as caught:call()
    case.assertIs(caught.exception.code,code);case.assertIs(caught.exception.outcome,Outcome.NO_EFFECT)
    case.assertEqual(readonly_snapshot(scenario.path),before)
    return caught.exception

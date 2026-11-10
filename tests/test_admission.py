"""Pre-source admission/priority literals, not adapted to generated source."""
from dataclasses import replace
from pathlib import Path
import tempfile,threading,unittest
from common import ACTORS,MEMBERS,Ledger,Scenario,readonly_snapshot,refusal,balance
from execucao_contratual_ledger.types import ActorSpec,Role,Principal,ItemSpec,Line,Limits,LedgerError,ErrorCode,Outcome

class AdmissionAndPriority(unittest.TestCase):
    def test_bootstrap_invalid_or_over_cap_precedes_file_creation(self):
        cases=[((),Limits(),ErrorCode.INVALID_INPUT),((ActorSpec('req',(Role.SUBMITTER,)),),Limits(),ErrorCode.INVALID_INPUT),((ActorSpec('mgr',()),),Limits(),ErrorCode.INVALID_INPUT),(ACTORS,Limits(max_actors=0),ErrorCode.LIMIT_EXCEEDED),(ACTORS,Limits(max_events=True),ErrorCode.INVALID_LIMIT)]
        for actors,limits,code in cases:
            with self.subTest(code=code,actors=actors),tempfile.TemporaryDirectory() as d:
                p=Path(d)/'absent.sqlite'
                with self.assertRaises(LedgerError) as e:Ledger.create(str(p),actors,limits=limits)
                self.assertIs(e.exception.code,code);self.assertIs(e.exception.outcome,Outcome.NO_EFFECT)
                self.assertFalse(p.exists())

    def test_every_limit_is_strict_and_bounded(self):
        for name,hard in [('max_actors',32),('max_contracts',32),('max_items_per_contract',16),('max_measurements_per_contract',256),('max_revisions_per_measurement',8),('max_events',4096)]:
            for bad in [True,-1,hard+1,'0',0.0]:
                with self.subTest(field=name,value=bad),tempfile.TemporaryDirectory() as d:
                    p=Path(d)/'absent.sqlite'
                    with self.assertRaises(LedgerError) as e:Ledger.create(str(p),ACTORS,limits=replace(Limits(),**{name:bad}))
                    self.assertIs(e.exception.code,ErrorCode.INVALID_LIMIT);self.assertFalse(p.exists())

    def test_zero_event_policy_rejects_first_business_command(self):
        with Scenario(self,limits=Limits(max_events=0)) as s:
            s.failure(ErrorCode.LIMIT_EXCEEDED,'create_contract',items=s.items,member_ids=s.members)

    def test_invalid_creation_items_and_empty_collections(self):
        with Scenario(self) as s:
            for items in [[],[ItemSpec('i','un',1,0)],[ItemSpec('i','un',True,100)],[ItemSpec('i','un x',1,100)],[ItemSpec('i','un',1,100),ItemSpec('i','un',1,100)]]:
                with self.subTest(items=items):s.failure(ErrorCode.INVALID_INPUT,'create_contract',items=items,member_ids=s.members)
            s.failure(ErrorCode.INVALID_INPUT,'create_contract',items=s.items,member_ids=[])

    def test_draft_quantity_types_minima_and_duplicates(self):
        with Scenario(self) as s:
            s.create()
            for value in [True,1.0,'7',-1,1000000000001,0]:
                with self.subTest(value=value):s.failure(ErrorCode.INVALID_INPUT,'create_measurement',actor='req',measurement_id='m1',lines=[Line('i',value)])
            for lines in [[],[Line('i',1),Line('i',2)]]:
                with self.subTest(lines=lines):s.failure(ErrorCode.INVALID_INPUT,'create_measurement',actor='req',measurement_id='m1',lines=lines)

    def test_acceptance_total_set_bounds_and_reason(self):
        with Scenario(self,items=(ItemSpec('i','un',1,100),ItemSpec('j','un',1,100))) as s:
            s.create();s.draft(lines=(Line('i',70),Line('j',30)));s.submit()
            invalid=[((Line('i',0),Line('j',0)),'valid'),((Line('i',1),),'valid'),((Line('i',71),Line('j',30)),'valid'),((Line('i',60),Line('j',30)),None),((Line('i',60),Line('j',30)),'  '),((Line('i',60),Line('j',30)),'bad\x7f')]
            for lines,reason in invalid:
                with self.subTest(lines=lines,reason=reason):s.failure(ErrorCode.INVALID_INPUT,'receive_measurement',actor='recv',measurement_id='m1',accepted_lines=lines,reason=reason)

    def test_versions_are_strict_and_closed_thread_precedes_bad_data(self):
        with Scenario(self) as s:
            s.create();s.draft()
            for version in [False,4097,0,-1,'2']:
                s.failure(ErrorCode.INVALID_INPUT,'submit_measurement',actor='req',expected=version,measurement_id='m1')
            s.ledger.close()
            with self.assertRaises(LedgerError) as e:s.invoke('create_measurement',actor='req',measurement_id='m1',lines=[Line('i','bad')])
            self.assertIs(e.exception.code,ErrorCode.CLOSED)
            errors=[]
            def wrong():
                try:s.invoke('create_measurement',actor='req',measurement_id='m1',lines=[Line('i','bad')])
                except LedgerError as e:errors.append(e.code)
            thread=threading.Thread(target=wrong);thread.start();thread.join(timeout=3)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[ErrorCode.WRONG_THREAD])

    def test_stale_precedes_insufficiency_duplicate_and_terminal_state(self):
        with Scenario(self) as s:
            s.create();s.draft(101);s.draft(1,mid='other_draft')
            e=s.failure(ErrorCode.STALE_VERSION,'submit_measurement',actor='req',expected=2,measurement_id='m1')
            self.assertEqual(e.current_version,3)
            s.failure(ErrorCode.INSUFFICIENT_BALANCE,'submit_measurement',actor='req',measurement_id='m1')
            s.failure(ErrorCode.ALREADY_EXISTS,'create_measurement',actor='req',measurement_id='m1',lines=[Line('i',1)])
        with Scenario(self) as s:
            s.create();s.draft();s.submit();s.receive()
            s.failure(ErrorCode.STATE_CONFLICT,'submit_measurement',actor='req',measurement_id='m1')

    def test_roles_scope_self_review_and_union(self):
        with Scenario(self) as s:
            s.create();original=s.run('amend_contract',new_limits=(Line('i',100),),reason='Synthetic unchanged ceiling')
            s.failure(ErrorCode.FORBIDDEN,'amend_contract',actor='aud',op=original.receipt.operation_id,expected=1,new_limits=(Line('i',100),),reason='Synthetic unchanged ceiling')
            refusal(self,s,ErrorCode.NOT_FOUND,lambda:s.ledger.read_operation(principal=Principal('other'),contract_id='c',operation_id=original.receipt.operation_id))
            s.draft(actor='dual');s.submit(actor='dual')
            s.failure(ErrorCode.SELF_REVIEW_FORBIDDEN,'receive_measurement',actor='dual',measurement_id='m1',accepted_lines=(Line('i',70),))
        extra=ActorSpec('combined',(Role.AUDITOR,Role.MANAGER,Role.RECEIVER))
        with Scenario(self,actors=ACTORS+(extra,),members=tuple(sorted(MEMBERS+('combined',)))) as s:
            s.create();s.run('amend_contract',actor='combined',new_limits=(Line('i',100),),reason='Synthetic role union')
            s.draft();s.submit();s.receive(70,actor='combined',reason=None)
            s.failure(ErrorCode.SELF_REVIEW_FORBIDDEN,'reverse_receipt',actor='combined',measurement_id='m1',reason='Synthetic denial')

    def test_owned_lock_is_busy_and_no_effect(self):
        with Scenario(self) as s:
            s.create();client=Ledger.open(str(s.path),timeout_ms=20)
            import sqlite3
            lock=sqlite3.connect(s.path,isolation_level=None);lock.execute('BEGIN IMMEDIATE')
            try:
                refusal(self,s,ErrorCode.BUSY,lambda:client.create_measurement(principal=Principal('req'),contract_id='c',operation_id='busy',expected_version=1,measurement_id='m1',lines=(Line('i',1),)))
            finally:lock.execute('ROLLBACK');lock.close();client.close()

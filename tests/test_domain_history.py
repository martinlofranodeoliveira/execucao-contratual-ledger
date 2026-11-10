"""Independent committed-fact, receipt, revision, history and visibility scenarios."""
from dataclasses import FrozenInstanceError
import unittest
from common import Scenario,Ledger,ACTORS,MEMBERS,readonly_snapshot,refusal,balance
from execucao_contratual_ledger.types import Principal,Line,ItemSpec,Limits,ErrorCode,MeasurementState,ActorSpec,Role

class DomainFactsAndHistory(unittest.TestCase):
    def test_original_receipt_replay_after_partial_acceptance(self):
        with Scenario(self) as s:
            s.create();s.draft();submitted=s.submit();s.receive()
            before=readonly_snapshot(s.path)
            retry=s.invoke('submit_measurement',actor='req',op=submitted.receipt.operation_id,expected=2,measurement_id='m1')
            self.assertTrue(retry.replayed);self.assertEqual(retry.receipt,submitted.receipt)
            self.assertEqual(balance(retry.receipt),(100,70,0,30));self.assertEqual(balance(s.contract()),(100,0,60,40))
            self.assertEqual(s.contract().version,4);self.assertEqual(readonly_snapshot(s.path),before)
            s.failure(ErrorCode.IDEMPOTENCY_CONFLICT,'submit_measurement',actor='req',op=submitted.receipt.operation_id,expected=4,measurement_id='m1')
            receipt=s.ledger.read_operation(principal=Principal('req'),contract_id='c',operation_id='op4')
            self.assertEqual(receipt.actor_id,'recv');self.assertEqual(receipt.measurement_state,MeasurementState.ACCEPTED_PARTIAL)

    def test_partial_return_floor_and_reversal_preserve_other_reservation(self):
        with Scenario(self) as s:
            s.create();s.draft();s.submit();accepted=s.receive()
            s.draft(20,mid='reserved');s.submit(mid='reserved')
            self.assertEqual(balance(s.contract()),(100,20,60,20))
            s.failure(ErrorCode.AMENDMENT_FLOOR,'amend_contract',new_limits=(Line('i',79),),reason='Synthetic floor')
            s.run('amend_contract',new_limits=(Line('i',80),),reason='Synthetic floor')
            self.assertEqual(balance(s.contract()),(80,20,60,0))
            s.run('reverse_receipt',measurement_id='m1',reason='Synthetic correction')
            self.assertEqual(balance(s.contract()),(80,20,0,60))
            m=s.ledger.read_measurement(principal=Principal('aud'),contract_id='c',measurement_id='m1')
            self.assertEqual(m.revisions[0].decision.accepted_lines,(Line('i',60),))
            self.assertEqual(m.revisions[0].reversal.reverses_operation_id,accepted.receipt.operation_id)
            s.failure(ErrorCode.ALREADY_REVERSED,'reverse_receipt',measurement_id='m1',reason='Synthetic second reversal')
            s.failure(ErrorCode.STATE_CONFLICT,'revise_measurement',actor='req',measurement_id='m1',lines=(Line('i',1),))

    def test_returned_revision_is_append_only_and_needs_resubmit(self):
        with Scenario(self) as s:
            s.create();s.draft(70)
            s.run('revise_measurement',actor='req',measurement_id='m1',lines=(Line('i',60),));s.submit()
            s.failure(ErrorCode.STATE_CONFLICT,'revise_measurement',actor='req',measurement_id='m1',lines=(Line('i',55),))
            s.run('return_measurement',actor='recv',measurement_id='m1',reason='Synthetic return')
            self.assertEqual(balance(s.contract()),(100,0,0,100))
            s.failure(ErrorCode.STATE_CONFLICT,'submit_measurement',actor='req',measurement_id='m1')
            s.run('revise_measurement',actor='req',measurement_id='m1',lines=(Line('i',55),))
            m=s.ledger.read_measurement(principal=Principal('req'),contract_id='c',measurement_id='m1')
            self.assertEqual(m.state,MeasurementState.DRAFT);self.assertEqual([r.revision_no for r in m.revisions],[1,2,3])
            self.assertEqual([r.lines[0].ticks for r in m.revisions],[70,60,55])
            self.assertEqual(m.revisions[1].decision.accepted_lines,(Line('i',0),))
            self.assertIsNone(m.revisions[2].submission);self.assertEqual(balance(s.contract()),(100,0,0,100))
            with self.assertRaises(FrozenInstanceError):m.owner_id='other'
            s.ledger.close();s.ledger=Ledger.open(str(s.path))
            self.assertEqual(s.ledger.read_measurement(principal=Principal('aud'),contract_id='c',measurement_id='m1'),m)

    def test_seven_literal_pagination_cases_and_anchored_later_write(self):
        with Scenario(self) as s:
            s.create();s.draft();s.submit();s.receive()
            page=s.ledger.read_history(principal=Principal('aud'),contract_id='c',limit=2)
            self.assertEqual((page.through_version,[e.version for e in page.events],page.next_after,page.complete),(4,[1,2],2,False))
            for after,through,limit,versions,next_after,complete in [(0,0,1,[],0,True),(4,4,100,[],4,True)]:
                p=s.ledger.read_history(principal=Principal('mgr'),contract_id='c',after_version=after,through_version=through,limit=limit)
                self.assertEqual(([e.version for e in p.events],p.next_after,p.complete),(versions,next_after,complete))
            for after,through,limit in [(3,2,2),(0,5,2),(0,4,True)]:
                refusal(self,s,ErrorCode.INVALID_INPUT,lambda a=after,t=through,l=limit:s.ledger.read_history(principal=Principal('mgr'),contract_id='c',after_version=a,through_version=t,limit=l))
            refusal(self,s,ErrorCode.FORBIDDEN,lambda:s.ledger.read_history(principal=Principal('req'),contract_id='c'))
            s.run('amend_contract',new_limits=(Line('i',100),),reason='Synthetic later write')
            page2=s.ledger.read_history(principal=Principal('aud'),contract_id='c',after_version=2,through_version=4,limit=2)
            self.assertEqual((page2.through_version,[e.version for e in page2.events],page2.next_after,page2.complete),(4,[3,4],4,True))

    def test_visibility_ownership_and_complete_multiline_rollback(self):
        with Scenario(self,actors=ACTORS+(ActorSpec('peer',(Role.SUBMITTER,)),),members=tuple(sorted(MEMBERS+('peer',))),items=(ItemSpec('i','un',1,100),ItemSpec('j','hour',1000,50))) as s:
            s.create();s.draft(lines=(Line('i',70),Line('j',70)))
            s.failure(ErrorCode.INSUFFICIENT_BALANCE,'submit_measurement',actor='req',measurement_id='m1')
            self.assertEqual(balance(s.contract()),(100,0,0,100));self.assertEqual(balance(s.contract(),'j'),(50,0,0,50))
            refusal(self,s,ErrorCode.NOT_FOUND,lambda:s.ledger.read_measurement(principal=Principal('peer'),contract_id='c',measurement_id='m1'))
            refusal(self,s,ErrorCode.NOT_FOUND,lambda:s.ledger.read_contract(principal=Principal('other'),contract_id='c'))
            self.assertEqual(s.ledger.read_contract(principal=Principal('req'),contract_id='c').version,2)
            refusal(self,s,ErrorCode.NOT_FOUND,lambda:s.ledger.read_operation(principal=Principal('req'),contract_id='c',operation_id='op1'))

    def test_scale_ticks_and_releasing_whole_partial_reservation(self):
        with Scenario(self,items=(ItemSpec('i','hour',1000,1000),ItemSpec('j','unit',1,2))) as s:
            s.create();s.draft(lines=(Line('i',375),Line('j',1)));s.submit()
            s.run('receive_measurement',actor='recv',measurement_id='m1',accepted_lines=(Line('i',250),Line('j',1)),reason='Synthetic partial ticks')
            self.assertEqual(balance(s.contract()),(1000,0,250,750));self.assertEqual(balance(s.contract(),'j'),(2,0,1,1))
            self.assertEqual([(b.unit,b.scale) for b in s.contract().balances],[('hour',1000),('unit',1)])

    def test_retained_quota_and_replay_at_full_event_capacity(self):
        for limits,method,data in [(Limits(max_measurements_per_contract=0),'create_measurement',{'measurement_id':'m1','lines':(Line('i',1),)}),(Limits(max_revisions_per_measurement=0),'create_measurement',{'measurement_id':'m1','lines':(Line('i',1),)})]:
            with self.subTest(limits=limits),Scenario(self,limits=limits) as s:
                s.create();s.failure(ErrorCode.LIMIT_EXCEEDED,method,actor='req',**data)
        with Scenario(self,limits=Limits(max_revisions_per_measurement=1)) as s:
            s.create();s.draft();s.failure(ErrorCode.LIMIT_EXCEEDED,'revise_measurement',actor='req',measurement_id='m1',lines=(Line('i',70),))
        with Scenario(self,limits=Limits(max_events=3)) as s:
            s.create();s.draft();saved=s.submit()
            s.failure(ErrorCode.LIMIT_EXCEEDED,'receive_measurement',actor='recv',measurement_id='m1',accepted_lines=(Line('i',70),))
            before=readonly_snapshot(s.path)
            retry=s.invoke('submit_measurement',actor='req',expected=2,op=saved.receipt.operation_id,measurement_id='m1')
            self.assertTrue(retry.replayed);self.assertEqual(retry.receipt,saved.receipt);self.assertEqual(readonly_snapshot(s.path),before)

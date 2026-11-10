"""Post-DATA counterexamples from the unchanged pre-source authority contract."""
import unittest
from common import Scenario,ACTORS,MEMBERS,refusal
from execucao_contratual_ledger.types import ActorSpec,Role,Principal,Line,ErrorCode

class AuthorizationOrdering(unittest.TestCase):
    def test_create_contract_requires_global_manager_before_object_lookup(self):
        with Scenario(self) as s:
            s.create()
            for contract_id in ['absent','c']:
                with self.subTest(contract_id=contract_id):
                    refusal(self,s,ErrorCode.FORBIDDEN,lambda cid=contract_id:s.ledger.create_contract(principal=Principal('other'),contract_id=cid,operation_id='denied',expected_version=0,items=s.items,member_ids=tuple(sorted(MEMBERS+('other',)))))

    def test_create_measurement_checks_existing_ownership_before_cached_key(self):
        with Scenario(self,actors=ACTORS+(ActorSpec('peer',(Role.SUBMITTER,)),),members=tuple(sorted(MEMBERS+('peer',)))) as s:
            s.create();s.draft(mid='private_req')
            s.failure(ErrorCode.NOT_FOUND,'create_measurement',actor='peer',measurement_id='private_req',lines=(Line('i',1),))

    def test_create_foreign_measurement_id_is_hidden_before_idempotency_conflict(self):
        with Scenario(self,actors=ACTORS+(ActorSpec('peer',(Role.SUBMITTER,)),),members=tuple(sorted(MEMBERS+('peer',)))) as s:
            s.create();s.draft(mid='private_req')
            own=s.draft(1,mid='own_peer',actor='peer')
            s.failure(ErrorCode.NOT_FOUND,'create_measurement',actor='peer',op=own.receipt.operation_id,expected=2,measurement_id='private_req',lines=(Line('i',1),))

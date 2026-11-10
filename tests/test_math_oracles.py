"""All 196 signed-delta and 336 amendment cases against the future public SDK."""
from pathlib import Path
import hashlib,json,runpy,unittest
from common import Scenario,balance
from execucao_contratual_ledger.types import ErrorCode,ItemSpec,Line
F=Path(__file__).parent/'fixtures'

class ExhaustiveIndependentOracles(unittest.TestCase):
    def test_all_196_small_balance_decisions_and_reversals(self):
        reference=runpy.run_path(str(F/'independent-reference.py'))
        frozen=json.loads((F/'pre-source-oracles.json').read_text())
        self.assertEqual(reference['corpus'](),frozen['exhaustive_small_balance_corpus'])
        count=0
        for a in range(6):
            for received in range(a+1):
                for reserved in range(a-received+1):
                    available=a-received-reserved
                    for q in range(1,available+1):
                        for accepted in range(q+1):
                            with self.subTest(a=a,r=received,s=reserved,q=q,x=accepted),Scenario(self,items=(ItemSpec('i','un',1,a),)) as s:
                                s.create()
                                if received:
                                    s.draft(received,mid='baseline_received');s.submit(mid='baseline_received');s.receive(received,mid='baseline_received',reason=None)
                                if reserved:
                                    s.draft(reserved,mid='baseline_reserved');s.submit(mid='baseline_reserved')
                                s.draft(q,mid='target');s.submit(mid='target')
                                self.assertEqual(balance(s.contract()),(a,reserved+q,received,available-q))
                                if accepted:s.receive(accepted,mid='target',reason=None if accepted==q else 'Synthetic remainder returned')
                                else:s.run('return_measurement',actor='recv',measurement_id='target',reason='Synthetic whole return')
                                self.assertEqual(balance(s.contract()),(a,reserved,received+accepted,available-accepted))
                                if accepted:
                                    s.run('reverse_receipt',measurement_id='target',reason='Synthetic correction')
                                    self.assertEqual(balance(s.contract()),(a,reserved,received,available))
                                count+=1
        self.assertEqual(count,196)

    def test_all_336_amendment_floor_cases(self):
        count=0
        for a in range(6):
            for received in range(a+1):
                for reserved in range(a-received+1):
                    for limit in range(6):
                        with self.subTest(a=a,r=received,s=reserved,new=limit),Scenario(self,items=(ItemSpec('i','un',1,max(a,1)),)) as s:
                            s.create()
                            if a==0:s.run('amend_contract',new_limits=(Line('i',0),),reason='Synthetic zero ceiling')
                            if received:
                                s.draft(received,mid='received');s.submit(mid='received');s.receive(received,mid='received',reason=None)
                            if reserved:s.draft(reserved,mid='reserved');s.submit(mid='reserved')
                            if limit<received+reserved:
                                s.failure(ErrorCode.AMENDMENT_FLOOR,'amend_contract',new_limits=(Line('i',limit),),reason='Synthetic floor probe')
                                self.assertEqual(balance(s.contract()),(a,reserved,received,a-reserved-received))
                            else:
                                before=s.contract().revision_no
                                s.run('amend_contract',new_limits=(Line('i',limit),),reason='Synthetic floor probe')
                                self.assertEqual(s.contract().revision_no,before+1)
                                self.assertEqual(balance(s.contract()),(limit,reserved,received,limit-reserved-received))
                            count+=1
        self.assertEqual(count,336)

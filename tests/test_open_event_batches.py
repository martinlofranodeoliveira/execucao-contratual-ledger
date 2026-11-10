"""Private corrupted-store provider seams; no SDK authority/security claim."""
import sqlite3,unittest
from common import Scenario,Ledger,balance
from execucao_contratual_ledger.types import LedgerError,ErrorCode,Line

class OpenEventBatchIntegrity(unittest.TestCase):
    def _shift_tail(self,scenario,old_version):
        scenario.ledger.close()
        raw=sqlite3.connect(scenario.path,isolation_level=None)
        try:
            raw.execute('PRAGMA foreign_keys=ON')
            self.assertEqual(raw.execute('PRAGMA foreign_keys').fetchone(),(1,))
            raw.execute('BEGIN IMMEDIATE');raw.execute('PRAGMA defer_foreign_keys=ON')
            self.assertEqual(raw.execute('PRAGMA defer_foreign_keys').fetchone(),(1,))
            self.assertEqual(raw.execute('UPDATE events SET version=? WHERE contract_id=? AND version=?',(old_version+1,'c',old_version)).rowcount,1)
            self.assertEqual(raw.execute('UPDATE operations SET event_version=? WHERE contract_id=? AND event_version=?',(old_version+1,'c',old_version)).rowcount,1)
            raw.execute('COMMIT')
            self.assertEqual(raw.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(raw.execute('SELECT version FROM contracts').fetchone()[0],old_version)
            self.assertEqual(raw.execute('SELECT count(*) FROM events').fetchone()[0],old_version)
        finally:raw.close()
        with self.assertRaises(LedgerError) as error:Ledger.open(str(scenario.path))
        self.assertIs(error.exception.code,ErrorCode.CORRUPT_STORE)

    def test_single_event_shift_is_corrupt_not_storage_error(self):
        with Scenario(self) as scenario:
            scenario.create();self._shift_tail(scenario,1)

    def test_zero_delta_draft_tail_shift_cannot_be_silently_accepted(self):
        with Scenario(self) as scenario:
            scenario.create();scenario.draft();self._shift_tail(scenario,2)

    def test_valid_sixteen_and_seventeen_event_batch_boundaries(self):
        for count in (16,17):
            with self.subTest(events=count),Scenario(self) as scenario:
                scenario.create()
                for _ in range(1,count):scenario.run('amend_contract',new_limits=(Line('i',100),),reason='Synthetic unchanged ceiling')
                scenario.ledger.close();scenario.ledger=Ledger.open(str(scenario.path))
                self.assertEqual(scenario.contract().version,count)
                self.assertEqual(balance(scenario.contract()),(100,0,0,100))

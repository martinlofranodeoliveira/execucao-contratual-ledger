"""Private qualified-provider seams; not public SDK input/admission coverage."""
from pathlib import Path
import sqlite3,tempfile,unittest
from execucao_contratual_ledger.store import _Store
from execucao_contratual_ledger.types import ActorSpec,Role,Limits,LedgerError,ErrorCode,Outcome
ACTORS=(ActorSpec('mgr',(Role.MANAGER,)),ActorSpec('req',(Role.SUBMITTER,)),ActorSpec('recv',(Role.RECEIVER,)))

class PrivateProviderSeams(unittest.TestCase):
    def test_exact_1048576_and_1048577_raw_read_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            store=_Store.create(str(Path(d)/'owned.sqlite'),ACTORS,Limits(),1000)
            try:
                store._conn.execute('CREATE TEMP TABLE probe(v TEXT NOT NULL) STRICT')
                store._conn.executemany('INSERT INTO probe VALUES(?)',[('x'*16384,)]*64+[('x',)])
                with store.read() as tx:self.assertEqual(len(tx._q('SELECT v FROM probe LIMIT 64',(),100)),64)
                with self.assertRaises(LedgerError) as e:
                    with store.read() as tx:tx._q('SELECT v FROM probe LIMIT 65',(),100)
                self.assertIs(e.exception.code,ErrorCode.LIMIT_EXCEEDED)
            finally:store.close()

    def test_native_commit_and_rollback_denial_is_unknown_and_poisoned(self):
        with tempfile.TemporaryDirectory() as d:
            p=str(Path(d)/'owned.sqlite');store=_Store.create(p,ACTORS,Limits(),1000)
            store._conn.set_authorizer(lambda op,x,y,db,source:sqlite3.SQLITE_DENY if op==sqlite3.SQLITE_TRANSACTION and x in ('COMMIT','ROLLBACK') else sqlite3.SQLITE_OK)
            with self.assertRaises(LedgerError) as e:
                with store.write():store._conn.execute("INSERT INTO contracts VALUES('c',1,1)")
            self.assertIs(e.exception.code,ErrorCode.COMMIT_UNCERTAIN);self.assertIs(e.exception.outcome,Outcome.UNKNOWN)
            with self.assertRaises(LedgerError) as closed:store.assert_owner_open()
            self.assertIs(closed.exception.code,ErrorCode.CLOSED)
            # This controlled denial happened before a COMMIT, not an actual lost ACK.
            reopened=_Store.open(p,1000)
            try:
                with reopened.read() as tx:self.assertIsNone(tx.contract('c'))
            finally:reopened.close()

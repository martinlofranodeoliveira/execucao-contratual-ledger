"""Real TCP, bounded parser, local CLI I/O and committed-write ACK cases."""
from pathlib import Path
import http.client,json,os,socket,sqlite3,subprocess,sys,tempfile,unittest
from common import ACTORS,MEMBERS,Ledger,readonly_snapshot
from execucao_contratual_ledger import cli,wire
from execucao_contratual_ledger.demo import run_demo,_call
from execucao_contratual_ledger.http import LedgerHTTPService,SyntheticAuthority
from execucao_contratual_ledger.types import ErrorCode,LedgerError,Principal,ItemSpec

class HttpAndCli(unittest.TestCase):
    def test_full_real_tcp_demo_and_commit_response_discard_replay(self):
        result=run_demo()
        self.assertEqual(result['status'],'PASS_SYNTHETIC_HTTP_DEMO')
        self.assertEqual(result['http_status_counts'],{'200':4,'201':13,'400':1,'401':3,'403':2,'404':1,'409':3,'503':1})
        self.assertEqual((result['events'],result['operations'],result['aggregate_version']),(14,14,14))
        self.assertEqual(result['discarded_response'],{'client_response_bytes_consumed':0,'commit_witness_version':14,'exact_retry_replayed':True})
        self.assertFalse(result['tokens_exported'])

    def test_actual_tcp_shape_token_and_schema_refusals_no_effect(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'owned.sqlite';db=Ledger.create(str(path),ACTORS);db.close()
            authority=SyntheticAuthority(tuple(a.actor_id for a in ACTORS))
            values=list(authority._tokens.values());self.assertEqual(len(set(values)),len(ACTORS))
            self.assertTrue(all(len(v)==64 and set(v)<=set('0123456789abcdef') for v in values))
            before=readonly_snapshot(path)
            with LedgerHTTPService(str(path),authority) as service:
                self.assertEqual(_call(service,authority,None,'GET','/v1/contracts/c')[0],401)
                self.assertEqual(_call(service,authority,'mgr','GET','/v1/contracts/c',invalid_token=True)[0],401)
                body={'contract_id':'c','operation_id':'one','expected_version':0,'items':[{'item_id':'i','unit':'un','scale':1,'authorized_ticks':100}],'member_ids':list(MEMBERS),'actor_id':'mgr'}
                self.assertEqual(_call(service,authority,'mgr','POST','/v1/commands/create_contract',body)[0],400)
                for raw in [b'{"contract_id":"c","contract_id":"other"}',b'{"expected_version":NaN}',b'\xff']:
                    connection=http.client.HTTPConnection(*service.address,timeout=5)
                    try:
                        connection.request('POST','/v1/commands/create_contract',body=raw,headers={'Authorization':authority._bearer_for_owned_client('mgr'),'Content-Type':'application/json'})
                        response=connection.getresponse();self.assertEqual(response.status,400);response.read()
                    finally:connection.close()
            self.assertEqual(readonly_snapshot(path),before)

    def test_actual_raw_http_caps_and_ambiguous_headers(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'owned.sqlite';db=Ledger.create(str(path),ACTORS);db.close()
            authority=SyntheticAuthority(tuple(a.actor_id for a in ACTORS));before=readonly_snapshot(path)
            with LedgerHTTPService(str(path),authority) as service:
                auth=authority._bearer_for_owned_client('mgr').encode('ascii')
                requests=[(414,b'GET /'+b'x'*1100+b' HTTP/1.1\r\n\r\n'),(431,b'GET /v1/contracts/c HTTP/1.1\r\nX: '+b'x'*4100+b'\r\n\r\n'),(400,b'POST /v1/commands/create_contract HTTP/1.1\r\nAuthorization: '+auth+b'\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n'),(400,b'POST /v1/commands/create_contract HTTP/1.1\r\nAuthorization: '+auth+b'\r\nTransfer-Encoding: chunked\r\n\r\n'),(413,b'POST /v1/commands/create_contract HTTP/1.1\r\nAuthorization: '+auth+b'\r\nContent-Type: application/json\r\nContent-Length: 65537\r\n\r\n')]
                for expected,request in requests:
                    with self.subTest(status=expected):
                        connection=socket.create_connection(service.address,timeout=5)
                        try:
                            connection.sendall(request);reply=http.client.HTTPResponse(connection);reply.begin();self.assertEqual(reply.status,expected);reply.read()
                        finally:connection.close()
            self.assertEqual(readonly_snapshot(path),before)

    def test_wire_unknown_objects_and_error_envelope_are_closed(self):
        class Foreign:
            def __getattribute__(self,name):raise AssertionError('foreign callback')
        with self.assertRaises(ValueError):wire.encode_result(Foreign())
        payload=json.loads(wire.encode_error(LedgerError(ErrorCode.INVALID_INPUT)))
        self.assertEqual(payload,{'error':{'code':'INVALID_INPUT','outcome':'NO_EFFECT','current_version':None}})
        with self.assertRaises(LedgerError):wire.load_json(b'{"a":1,"a":2}')

    def _run(self,*args,input=None,stdout=subprocess.PIPE,stderr=subprocess.PIPE):
        return subprocess.run([sys.executable,'-I','-B','-m','execucao_contratual_ledger.cli',*args],input=input,stdout=stdout,stderr=stderr)

    def test_real_cli_init_execute_read_and_input_error_codes(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'owned.sqlite'
            bootstrap={'actors':[{'actor_id':a.actor_id,'roles':[r.value for r in a.roles]} for a in ACTORS]}
            created=self._run('init',str(path),input=json.dumps(bootstrap).encode());self.assertEqual(created.returncode,0);self.assertEqual(json.loads(created.stdout),{'created':True})
            body={'action':'create_contract','contract_id':'c','operation_id':'one','expected_version':0,'items':[{'item_id':'i','unit':'un','scale':1,'authorized_ticks':100}],'member_ids':list(MEMBERS)}
            write=self._run('execute',str(path),'--actor','mgr',input=json.dumps(body).encode());self.assertEqual(write.returncode,0)
            self.assertEqual(json.loads(write.stdout)['result']['receipt']['committed_version'],1)
            read=self._run('read',str(path),'--actor','aud','--contract','c');self.assertEqual(read.returncode,0);self.assertEqual(json.loads(read.stdout)['result']['version'],1)
            before=readonly_snapshot(path)
            bad=self._run('execute',str(path),'--actor','mgr',input=b'{bad');self.assertEqual(bad.returncode,2);self.assertEqual(json.loads(bad.stderr)['error']['code'],'INVALID_INPUT')
            unknown=self._run('read',str(path),'--actor','other','--contract','c');self.assertEqual(unknown.returncode,3)
            echo=self._run('read',str(path),'--actor','mgr','--contract','c','--after','DO_NOT_ECHO_INPUT');self.assertEqual(echo.returncode,2);self.assertNotIn(b'DO_NOT_ECHO_INPUT',echo.stderr)
            self.assertEqual(readonly_snapshot(path),before)

    def test_real_full_streams_help_and_committed_output_failure_exit4(self):
        with open('/dev/full','wb',buffering=0) as full:
            help_result=self._run('--help',stdout=full,stderr=full)
            self.assertEqual(help_result.returncode,4)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'owned.sqlite';db=Ledger.create(str(path),ACTORS)
            db.create_contract(principal=Principal('mgr'),contract_id='c',operation_id='create',expected_version=0,items=(ItemSpec('i','un',1,100),),member_ids=MEMBERS);db.close()
            body={'action':'create_measurement','contract_id':'c','operation_id':'lost_stdout','expected_version':1,'measurement_id':'m1','lines':[{'item_id':'i','ticks':70}]}
            with open('/dev/full','wb',buffering=0) as full:
                result=self._run('execute',str(path),'--actor','req',input=json.dumps(body).encode(),stdout=full)
            self.assertEqual(result.returncode,4)
            self.assertEqual(json.loads(result.stderr)['error']['outcome'],'UNKNOWN')
            db=Ledger.open(str(path))
            try:
                receipt=db.read_operation(principal=Principal('mgr'),contract_id='c',operation_id='lost_stdout')
                self.assertEqual(receipt.committed_version,2)
            finally:db.close()

    def test_real_closed_stdout_pipe_help_exit4_no_sigpipe(self):
        reader,writer=os.pipe();os.close(reader)
        try:
            result=self._run('--help',stdout=writer)
            self.assertEqual(result.returncode,4)
        finally:os.close(writer)

class RegisteredCliAndNonblockingEOF(unittest.TestCase):
    def test_nonblocking_valid_prefix_and_none_refused_before_effect(self):
        with tempfile.TemporaryDirectory() as d:
            for mode in ('execute','init','empty'):
                with self.subTest(mode=mode):
                    path=Path(d)/(mode+'.sqlite')
                    if mode!='init':
                        db=Ledger.create(str(path),ACTORS);db.close();before=readonly_snapshot(path)
                    body={'actors':[{'actor_id':a.actor_id,'roles':[r.value for r in a.roles]} for a in ACTORS]} if mode=='init' else {'action':'create_contract','contract_id':'c','operation_id':'prefix','expected_version':0,'items':[{'item_id':'i','unit':'un','scale':1,'authorized_ticks':100}],'member_ids':list(MEMBERS)}
                    reader,writer=os.pipe();os.set_blocking(reader,False)
                    if mode!='empty':os.write(writer,json.dumps(body).encode())
                    args=['init',str(path)] if mode=='init' else ['execute',str(path),'--actor','mgr']
                    child=subprocess.Popen([sys.executable,'-I','-B','-m','execucao_contratual_ledger.cli',*args],stdin=reader,stdout=subprocess.PIPE,stderr=subprocess.PIPE);os.close(reader)
                    try:
                        stdout,stderr=child.communicate(timeout=5)
                        self.assertEqual(child.returncode,4);self.assertEqual(stdout,b'')
                        self.assertEqual(json.loads(stderr)['error'],{'code':'STORAGE_ERROR','outcome':'NO_EFFECT','current_version':None})
                        if mode=='init':self.assertFalse(path.exists())
                        else:self.assertEqual(readonly_snapshot(path),before)
                    finally:os.close(writer)
    def test_registered_demo_assert_argv_is_real_asserting_workflow(self):
        result=subprocess.run([sys.executable,'-I','-B','-m','execucao_contratual_ledger.cli','demo','--assert'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout)['events'],14)

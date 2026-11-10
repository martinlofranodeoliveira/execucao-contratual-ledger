"""Root helper command: installed wheel checked against the actual /work source.

The root controller mounts /work RW and checks source hashes before/after; this is not a no-bind or read-only-root gate.
"""
from pathlib import Path
import hashlib,json,os,subprocess,sys,unittest
import execucao_contratual_ledger as package

def main():
    root=Path('/work');installed=Path(package.__file__).parent
    if not str(installed).startswith('/opt/quality-runtime/'):
        raise RuntimeError('installed artifact prefix mismatch')
    for source in (root/'src/execucao_contratual_ledger').glob('*.py'):
        if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256((installed/source.name).read_bytes()).digest():
            raise RuntimeError('installed artifact/source SHA mismatch')
    if len(sys.argv)!=2 or sys.argv[1] not in ('test','demo'):raise RuntimeError('invalid quality command')
    if sys.argv[1]=='test':
        suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test_*.py')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    result=subprocess.run(['/opt/quality-runtime/bin/execucao-contratual-ledger','demo','--assert'],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('installed demo failed')
    value=json.loads(result.stdout)
    if (value['status']!='PASS_SYNTHETIC_HTTP_DEMO' or value['events']!=14 or value['operations']!=14
            or value['final_item']!={'authorized_ticks':90,'reserved_ticks':0,'received_ticks':0,'available_ticks':90}
            or value['discarded_response']!={'client_response_bytes_consumed':0,'commit_witness_version':14,'exact_retry_replayed':True}):
        raise RuntimeError('installed demo assertions failed')
    print(json.dumps(value,sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())

"""Cold installed artifact consumer: no checkout, compiler or build backend."""
from pathlib import Path
import hashlib,importlib.metadata as md,importlib.util,json,os,shutil,sqlite3,subprocess,sys
import execucao_contratual_ledger as package
from execucao_contratual_ledger.demo import run_demo
expected=json.loads(Path('/consumer/package-check.json').read_text())
assert sys.version.split()[0]=='3.13.18' and sqlite3.sqlite_version=='3.40.1'
assert os.getuid()==1000 and os.getgid()==1000
status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(),16)==0 and status['NoNewPrivs'].strip()=='1'
assert sorted(d.metadata['Name'] for d in md.distributions())==['execucao-contratual-ledger']
for dependency in ('pip','setuptools','wheel','build','packaging','pyproject_hooks'):
    assert importlib.util.find_spec(dependency) is None
assert not Path('/opt/sdk').exists() and not Path('/opt/admitted-source').exists()
assert all(shutil.which(name) is None for name in ('gcc','g++','clang','cc','make'))
root=Path(package.__file__).parent;assert str(root).startswith('/opt/runtime/')
for name,info in expected['source_manifest'].items():
    if name.startswith('src/') and name.endswith('.py'):
        path=root/Path(name).name
        assert hashlib.sha256(path.read_bytes()).hexdigest()==info['sha256']
console='/opt/runtime/bin/execucao-contratual-ledger'
for option in ('--help','--version'):
    result=subprocess.run([console,option],capture_output=True,text=True)
    assert result.returncode==0
result=subprocess.run([console,'demo','--assert'],capture_output=True,text=True)
assert result.returncode==0
summary=json.loads(result.stdout)
assert summary['status']=='PASS_SYNTHETIC_HTTP_DEMO' and summary['events']==14 and summary['operations']==14
assert summary['discarded_response']=={'client_response_bytes_consumed':0,'commit_witness_version':14,'exact_retry_replayed':True}
print(json.dumps({'status':'PASS_COLD_INSTALLED_HTTP_CLI_CONSUMER','python':sys.version.split()[0],
 'sqlite':sqlite3.sqlite_version,'package_path':str(root),'runtime_distributions':['execucao-contratual-ledger'],
 'build_backend_available':False,'compiler_available':False,'source_fallback':False,'source_hashes_verified':True,
 'console_demo':summary},sort_keys=True))

"""Original SDK package/installed consumer baseline, no host mounts/network."""
from pathlib import Path
import base64,hashlib,importlib.metadata as md,json,os,resource,shutil,subprocess,sys,tarfile,traceback,zipfile
root=Path('/opt/admitted-source');expected={str(x.relative_to(root)):{'bytes':x.stat().st_size,'sha256':hashlib.sha256(x.read_bytes()).hexdigest()} for x in root.rglob('*') if x.is_file()};logs={};artifacts={};stage='source_admission'
def run(name,args):
    result=subprocess.run(args,cwd='/tmp/consumer-work',capture_output=True,text=True)
    logs[name+'.stdout']=result.stdout;logs[name+'.stderr']=result.stderr
    if result.returncode:raise RuntimeError(name+' exit '+str(result.returncode))
    return result

def inventory():
    return {str(x.relative_to(root)):{'bytes':x.stat().st_size,'sha256':hashlib.sha256(x.read_bytes()).hexdigest()} for x in root.rglob('*') if x.is_file()}

result={'status':'BASELINE_NOT_COMPLETE','package_scope':'Genuine own package, rebuilt from own sdist; installed fresh prefix outside checkout. SDK image contains build tooling; separate cold runtime proof follows.'}
try:
    assert inventory()==expected and sys.version.split()[0]=='3.13.18'
    import sqlite3
    assert sqlite3.sqlite_version=='3.40.1'
    versions={name:md.version(name) for name in ['setuptools','wheel','build','pip','packaging','pyproject_hooks']}
    assert versions=={'setuptools':'84.2.1','wheel':'0.48.0','build':'1.6.1','pip':'26.2.2','packaging':'26.3','pyproject_hooks':'1.3.3'},versions
    result['build_versions']=versions
    Path('/tmp/consumer-work').mkdir();shutil.copytree(root,'/tmp/build-tree')
    stage='genuine_sdist_and_wheel'
    run('build-original',[sys.executable,'-I','-B','-m','build','--no-isolation','--sdist','--wheel','--outdir','/tmp/original','/tmp/build-tree'])
    sdist=next(Path('/tmp/original').glob('*.tar.gz'));original_wheel=next(Path('/tmp/original').glob('*.whl'))
    stage='safe_sdist_extract_and_rebuild'
    Path('/tmp/extracted').mkdir()
    with tarfile.open(sdist,'r:gz') as archive:
        members=archive.getmembers();assert len(members)<=512 and sum(m.size for m in members)<=8388608
        for m in members:
            assert not Path(m.name).is_absolute() and '..' not in Path(m.name).parts and (m.isfile() or m.isdir())
        archive.extractall('/tmp/extracted',filter='data')
    extracted=next(Path('/tmp/extracted').iterdir())
    run('build-rebuilt',[sys.executable,'-I','-B','-m','build','--no-isolation','--wheel','--outdir','/tmp/rebuilt',str(extracted)])
    rebuilt=next(Path('/tmp/rebuilt').glob('*.whl'))
    with zipfile.ZipFile(rebuilt) as archive:
        for name,info in expected.items():
            if name.startswith('src/') and name.endswith('.py'):
                assert hashlib.sha256(archive.read(name.removeprefix('src/'))).hexdigest()==info['sha256']
    Path('/tmp/source-manifest.json').write_text(json.dumps(expected))
    stage='fresh_prefix_install'
    run('venv',['/usr/local/bin/python','-I','-B','-m','venv','--without-pip','/tmp/consumer-prefix'])
    consumer='/tmp/consumer-prefix/bin/python'
    run('install',[sys.executable,'-I','-B','-m','pip','--isolated','--disable-pip-version-check','--no-cache-dir','--python',consumer,'install','--no-index','--no-deps','--no-compile',str(rebuilt)])
    receipt_script='''import hashlib,importlib.metadata as md,json,sys
from pathlib import Path
import execucao_contratual_ledger as package
from execucao_contratual_ledger import core,store,types
expected=json.loads(Path('/tmp/source-manifest.json').read_text())
for module in [core,store,types]:
 path=Path(module.__file__);assert str(path).startswith('/tmp/consumer-prefix/')
 key='src/execucao_contratual_ledger/'+path.name
 assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[key]['sha256']
assert sorted(d.metadata['Name'] for d in md.distributions())==['execucao-contratual-ledger']
assert not any(p.startswith('/opt/admitted-source') or p.startswith('/tmp/build-tree') for p in sys.path)
print(json.dumps({'installed_prefix':sys.prefix,'package_file':package.__file__,'source_hashes_verified':True,'only_distribution':'execucao-contratual-ledger','source_fallback':False}))'''
    check=run('installed-source-receipt',[consumer,'-I','-B','-c',receipt_script]);result['installed_receipt']=json.loads(check.stdout)
    stage='independent_sdk_unittests'
    tests=subprocess.run([consumer,'-I','-B','-m','unittest','discover','-s','/opt/admitted-source/tests','-p','test_*.py','-v'],cwd='/tmp/consumer-work',capture_output=True,text=True)
    logs['unittest.stdout']=tests.stdout;logs['unittest.stderr']=tests.stderr
    result['unittest_returncode']=tests.returncode
    result['status']='PASS_BASELINE_INSTALLED_SDK' if tests.returncode==0 else 'RED_BASELINE_INSTALLED_SDK_TESTS_INSPECT_FAILURES'
    if tests.returncode==0:
        stage='console_entrypoint_demo'
        console=str(Path(consumer).parent/'execucao-contratual-ledger')
        demo=run('console-demo',[console,'demo','--assert']);summary=json.loads(demo.stdout)
        assert summary['status']=='PASS_SYNTHETIC_HTTP_DEMO' and summary['events']==14
        assert summary['discarded_response']['client_response_bytes_consumed']==0
        result['console_demo']=summary
        run('console-help',[console,'--help']);run('console-version',[console,'--version'])
    for tag,path in [('sdist',sdist),('original_wheel',original_wheel),('rebuilt_wheel',rebuilt)]:
        b=path.read_bytes();artifacts[tag]={'filename':path.name,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'base64':base64.b64encode(b).decode()}
except BaseException as e:
    result['status']='FAILED_PREPARATION_OR_PACKAGE_STAGE_NOT_YET_CLASSIFIED';logs['exception.txt']=traceback.format_exc()
finally:
    result['stage']=stage;result['source_after_matches']=inventory()==expected;result['source_manifest']=expected
    result['python']=sys.version.split()[0];result['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result['peak_child_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    result['uid']=os.getuid();result['logs']=logs;result['artifacts']=artifacts
    print(json.dumps(result,sort_keys=True))

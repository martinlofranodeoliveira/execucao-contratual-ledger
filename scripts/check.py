"""Own public native package pipeline; no host bind/environment forwarding."""
from pathlib import Path
import argparse,base64,hashlib,json,os,re,shutil,subprocess,tempfile,uuid
from verify_notices import verify
ROOT=Path(__file__).resolve().parents[1]
MARKER='.owned-by-execucao-contract-check'
MANAGED={MARKER,'original','rebuilt','sdist','logs','package-check.json','sdk-container.json','runtime-consumer.json','runtime-container.json','native-success.json'}
LIMITS=['--network','none','--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','256','--memory','1g','--cpus','2','--read-only','--tmpfs','/tmp:rw,exec,nosuid,nodev,size=512m']

def _no_links(path):
    if path.is_symlink():raise RuntimeError('generated path is a symlink')
    if path.is_dir():
        for parent,dirs,files in os.walk(path,followlinks=False):
            for name in dirs+files:
                if (Path(parent)/name).is_symlink():raise RuntimeError('generated tree contains a symlink')

def reset_dist(root):
    dist=root/'dist'
    if dist.is_symlink() or (dist.exists() and not dist.is_dir()):raise RuntimeError('unsafe generated destination')
    if dist.exists() and any(dist.iterdir()):
        marker=dist/MARKER
        if marker.is_symlink() or not marker.is_file() or marker.read_text()!='execucao-contratual-ledger-check-v1\n':raise RuntimeError('destination is not an owned check directory')
        # Invalidate an owned regular success receipt even if another generated
        # subtree later fails safety admission. Never follow a success symlink.
        success=dist/'native-success.json'
        if success.is_file() and not success.is_symlink():success.unlink()
        # Validate all owned trees before deleting, never traverse foreign symlinks.
        for child in dist.iterdir():
            if child.name in MANAGED:_no_links(child)
        for child in list(dist.iterdir()):
            if child.name in MANAGED:
                if child.is_dir():shutil.rmtree(child)
                else:child.unlink()
        if any(dist.iterdir()):raise RuntimeError('unrelated files preserved in generated destination')
    dist.mkdir(exist_ok=True)
    (dist/MARKER).write_text('execucao-contratual-ledger-check-v1\n')
    return dist

def run(args,capture=False):
    value=subprocess.run(args,cwd=ROOT,capture_output=capture,text=True)
    if value.returncode:raise RuntimeError('native stage failed')
    return value

def inspect(container):
    data=json.loads(run(['docker','inspect',container],True).stdout)[0]
    h=data['HostConfig'];s=data['State']
    if (data['Config']['User']!='1000:1000' or h['NetworkMode']!='none' or h['Binds'] or data['Mounts']
        or h['CapDrop']!=['ALL'] or h['SecurityOpt']!=['no-new-privileges'] or h['PidsLimit']!=256
        or h['Memory']!=1073741824 or h['NanoCpus']!=2000000000 or not h['ReadonlyRootfs']
        or h['Tmpfs']!={'/tmp':'rw,exec,nosuid,nodev,size=512m'} or s['Running'] or s['ExitCode']!=0 or s['OOMKilled']):
        raise RuntimeError('native confinement or terminal status mismatch')
    return {'id':data['Id'],'image':data['Image'],'user':data['Config']['User'],'network':h['NetworkMode'],
        'binds':h['Binds'],'mounts':data['Mounts'],'cap_drop':h['CapDrop'],'security_opt':h['SecurityOpt'],
        'pids':h['PidsLimit'],'memory':h['Memory'],'nano_cpus':h['NanoCpus'],'read_only':h['ReadonlyRootfs'],
        'tmpfs':h['Tmpfs'],'exit':s['ExitCode'],'oom':s['OOMKilled']}

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--wheelhouse',type=Path)
    args=parser.parse_args(argv)
    # Always invalidate previously owned success BEFORE any Docker stage.
    dist=reset_dist(ROOT)
    if verify(ROOT)!=163:raise RuntimeError('actual notice inventory count mismatch')
    locks=[]
    for line in (ROOT/'requirements-build.lock').read_text().splitlines():
        match=re.fullmatch(r'([A-Za-z0-9_]+)==([0-9.]+) --hash=sha256:([0-9a-f]{64})',line)
        if match is None:raise RuntimeError('invalid closed build lock')
        name,version,digest=match.groups();locks.append((name+'-'+version+'-py3-none-any.whl',digest))
    if len(locks)!=5:raise RuntimeError('build closure mismatch')
    token=uuid.uuid4().hex;owned=[];success=None
    sdk='execucao-contract-sdk:'+token;runtime='execucao-contract-runtime:'+token
    try:
        with tempfile.TemporaryDirectory(prefix='execution-contract-check-') as scratch:
            scratch=Path(scratch);sdk_context=scratch/'sdk';sdk_context.mkdir();wheels=sdk_context/'wheels';wheels.mkdir()
            if args.wheelhouse is None:
                prepared='execucao-contract-preparation:'+token
                run(['docker','build','-f','Dockerfile.prepare','-t',prepared,'.'])
                reader=run(['docker','create',*LIMITS,'--entrypoint','/bin/true',prepared],True).stdout.strip();owned.append(reader)
                for name,_ in locks:run(['docker','cp',reader+':/opt/wheelhouse/'+name,str(wheels/name)])
            else:
                for name,_ in locks:
                    source=args.wheelhouse/name
                    if source.is_symlink() or not source.is_file() or source.stat().st_size>16777216:raise RuntimeError('invalid cached wheel')
                    shutil.copyfile(source,wheels/name)
            for name,digest in locks:
                if hashlib.sha256((wheels/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('cached wheel hash mismatch')
            ignore=shutil.ignore_patterns('dist','.git','.venv','.cache','__pycache__','*.pyc','*.egg-info')
            shutil.copytree(ROOT,sdk_context/'source',ignore=ignore,symlinks=True)
            _no_links(sdk_context/'source');shutil.copyfile(ROOT/'Dockerfile',sdk_context/'Dockerfile')
            run(['docker','build','--network','none','-t',sdk,str(sdk_context)])
            container=run(['docker','create',*LIMITS,sdk],True).stdout.strip();owned.append(container)
            output=run(['docker','start','-a',container],True)
            report=json.loads(output.stdout)
            logs=report.pop('logs');artifacts=report.pop('artifacts')
            (dist/'logs').mkdir()
            for name,text in logs.items():
                if re.fullmatch(r'[A-Za-z0-9_.-]+',name) is None:raise RuntimeError('invalid log name')
                (dist/'logs'/name).write_text(text)
            if (report['status']!='PASS_BASELINE_INSTALLED_SDK' or report['unittest_returncode']!=0
                or not report['source_after_matches'] or report['console_demo']['status']!='PASS_SYNTHETIC_HTTP_DEMO'):
                (dist/'package-check.json').write_text(json.dumps(report,indent=2)+'\n')
                raise RuntimeError('SDK package stage did not pass; inspect generated logs')
            names={'original_wheel':'original','rebuilt_wheel':'rebuilt','sdist':'sdist'}
            for key,record in artifacts.items():
                if key not in names or re.fullmatch(r'[A-Za-z0-9_.-]+',record['filename']) is None:raise RuntimeError('invalid artifact metadata')
                data=base64.b64decode(record.pop('base64'),validate=True)
                if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:raise RuntimeError('artifact hash mismatch')
                folder=dist/names[key];folder.mkdir();(folder/record['filename']).write_bytes(data)
            if set(artifacts)!=set(names):raise RuntimeError('incomplete genuine package artifacts')
            report['artifacts']=artifacts
            (dist/'package-check.json').write_text(json.dumps(report,indent=2)+'\n')
            (dist/'sdk-container.json').write_text(json.dumps(inspect(container),indent=2)+'\n')
            runtime_context=scratch/'runtime';(runtime_context/'distributions').mkdir(parents=True)
            wheel=next((dist/'rebuilt').glob('*.whl'));shutil.copyfile(wheel,runtime_context/'distributions'/wheel.name)
            for source,target in [(ROOT/'Dockerfile.runtime',runtime_context/'Dockerfile'),(ROOT/'scripts/consumer.py',runtime_context/'consumer.py'),(dist/'package-check.json',runtime_context/'package-check.json')]:shutil.copyfile(source,target)
            run(['docker','build','--network','none','-t',runtime,str(runtime_context)])
            consumer=run(['docker','create',*LIMITS,runtime],True).stdout.strip();owned.append(consumer)
            cold=run(['docker','start','-a',consumer],True);receipt=json.loads(cold.stdout)
            if receipt['status']!='PASS_COLD_INSTALLED_HTTP_CLI_CONSUMER' or receipt['source_fallback']:raise RuntimeError('cold runtime did not pass')
            (dist/'runtime-consumer.json').write_text(json.dumps(receipt,indent=2)+'\n')
            (dist/'runtime-container.json').write_text(json.dumps(inspect(consumer),indent=2)+'\n')
            success={'status':'NATIVE_PACKAGE_AND_COLD_HTTP_CLI_COMPLETE','sdk_image':json.loads((dist/'sdk-container.json').read_text())['image'],'runtime_image':json.loads((dist/'runtime-container.json').read_text())['image'],'notice_texts':163,'artifacts':artifacts}

    finally:
        cleanup=[]
        for container in reversed(owned):
            removed=subprocess.run(['docker','rm','-f',container],capture_output=True,text=True)
            absent=subprocess.run(['docker','inspect',container],capture_output=True,text=True)
            cleanup.append({'id':container,'remove_exit':removed.returncode,'confirmed_absent':absent.returncode!=0 and 'no such object' in absent.stderr.lower()})
        (dist/'logs').mkdir(exist_ok=True)
        (dist/'logs/owned-cleanup.json').write_text(json.dumps(cleanup,indent=2)+'\n')
        if any(not entry['confirmed_absent'] for entry in cleanup):raise RuntimeError('owned container cleanup not confirmed')
    if success is None:raise RuntimeError('native pipeline did not complete')
    success['owned_cleanup_confirmed']=True
    (dist/'native-success.json').write_text(json.dumps(success,indent=2)+'\n')
    print(json.dumps(success,sort_keys=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())

"""Verify actual flat notice inventories without following symlinks."""
from pathlib import Path
import hashlib,json

def verify(root):
    root=Path(root);count=0
    build=json.loads((root/'notices/build-manifest.json').read_text())
    base=json.loads((root/'notices/base-manifest.json').read_text())
    records=base['files']+[r for wheel in build['wheels'] for r in wheel['actual_notice_texts']]
    seen=set()
    for record in records:
        rel=Path(record['file'])
        if rel.is_absolute() or '..' in rel.parts or rel.parts[0]!='notices' or str(rel) in seen:raise ValueError('invalid notice inventory')
        seen.add(str(rel));path=root/rel
        if path.is_symlink() or not path.is_file():raise ValueError('missing regular notice')
        data=path.read_bytes()
        if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:raise ValueError('notice bytes changed')
        count+=1
    return count
if __name__=='__main__':
    print(verify(Path(__file__).resolve().parents[1]))

"""Source-only and secret audit of current files, index and every local Git tree."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PAYLOADS=('.zip','.whl','.tar.gz','.tar.bz2','.nupkg','.onnx','.gguf','.db','.db-wal','.db-shm','.sqlite','.index','.exe','.dll','.pyd','.log')
PATTERNS=[('credential-format',re.compile(rb'(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})')),
          ('private-key',re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'))]
FORBIDDEN={'Backup','private','instances','logs','models','cache','third_party/bootstrap-wheels'}

def audit(repo,known_values=(),include_git=True):
    repo=Path(repo).resolve();findings=[];seen=set();versions=0;total_bytes=0
    def scan(name,data,source,oid=None):
        nonlocal versions,total_bytes
        key=(name,oid or hashlib.sha256(data).hexdigest())
        if key in seen:return
        seen.add(key);versions+=1;total_bytes+=len(data)
        lower=name.lower()
        if lower.endswith(PAYLOADS) or any(part.casefold() in {'backup','private','instances','logs','models','cache'} for part in Path(name).parts):
            findings.append({'path':name,'kind':'forbidden-payload-or-private-path','source':source})
        if lower.startswith('src/p2d/upstream_selfreply/'):
            findings.append({'path':name,'kind':'copied-third-party-source','source':source})
        for kind,pattern in PATTERNS:
            if pattern.search(data):findings.append({'path':name,'kind':kind,'source':source})
        for value in known_values:
            if value and str(value).encode('utf-8') in data:
                findings.append({'path':name,'kind':'known-private-value','source':source});break
    for path in repo.rglob('*'):
        if not path.is_file() or any(x in {'.git','__pycache__'} for x in path.relative_to(repo).parts) or path.suffix=='.pyc':continue
        if path.is_symlink():findings.append({'path':path.relative_to(repo).as_posix(),'kind':'symlink','source':'working-tree'});continue
        scan(path.relative_to(repo).as_posix(),path.read_bytes(),'working-tree')
    commits=[];trees=set();index_tree=None
    if include_git and (repo/'.git').exists():
        def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL)
        try:commits=git('rev-list','--all').decode().split()
        except subprocess.CalledProcessError:commits=[]
        for commit in commits:trees.add(git('rev-parse',commit+'^{tree}').decode().strip())
        if (repo/'.git/index').exists():
            # Read the exact staged blob set; no write-tree/index mutation required.
            records=git('ls-files','--stage','-z').split(b'\0')
            for record in records:
                if not record:continue
                meta,name=record.split(b'\t',1);mode,oid,stage=meta.split()
                if stage!=b'0':raise ValueError('Unmerged index cannot be released')
                if mode==b'120000':raise ValueError('Staged symlink cannot be released')
                scan(name.decode(),git('cat-file','blob',oid.decode()),'index',oid.decode())
        for tree in trees:
            for record in git('ls-tree','-r','-z',tree).split(b'\0'):
                if not record:continue
                meta,name=record.split(b'\t',1);mode,kind,oid=meta.split()
                if mode==b'120000':raise ValueError('Historical symlink requires review')
                scan(name.decode(),git('cat-file','blob',oid.decode()),'history:'+tree,oid.decode())
    return {'schema_version':1,'project':'Fat-Fish-Assistant','file_versions_checked':versions,'bytes_checked':total_bytes,'history_commits':len(commits),'history_trees':len(trees),'known_values_count':len(known_values),'unresolved_findings':findings,'passed':not findings,
            'limits':'Known-value and pattern audit; unknown secrets cannot be ruled out. No external runtime or private history is imported.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--secrets-file',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    values=json.loads(a.secrets_file.read_text(encoding='utf-8-sig')) if a.secrets_file else []
    if not isinstance(values,list):raise ValueError('Secret input must be a private JSON list; values are never printed')
    result=audit(a.repo,values)
    if a.output:a.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result));raise SystemExit(not result['passed'])

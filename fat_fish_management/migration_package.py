"""Authenticated portable snapshot envelope. No password files or CLI secrets."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import io
import json
import os
from pathlib import PurePosixPath
import zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC=b'FFA-MIGRATION-1\0'
LIMIT=64*1024*1024*1024

def portable_name(name):
    path=PurePosixPath(name)
    reserved={'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]}
    if (not isinstance(name,str) or not name or path.is_absolute() or '\\' in name or ':' in name or
        any(p in {'','.','..'} or p.endswith((' ','.')) or p.split('.')[0].casefold() in reserved or
            any(ord(ch)<32 or ch in '<>"|?*' for ch in p) for p in name.split('/'))):
        raise ValueError('Unsafe portable path')
    return name

def key(password,salt):
    if len(password)<12:raise ValueError('Migration password needs at least 12 characters')
    return Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(password.encode('utf-8'))

def pack(snapshot,destination,password):
    """Stream standard AES-GCM; all intermediate plaintext stays in a private boundary."""
    import tempfile
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    from fat_fish_management.instance import protect_boundary
    salt=os.urandom(16);nonce=os.urandom(12);header=MAGIC+salt+nonce
    encryptor=Cipher(algorithms.AES(key(password,salt)),modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with tempfile.TemporaryDirectory(prefix='migration-crypto-',dir=snapshot.parent) as temporary:
        work=__import__('pathlib').Path(temporary);protect_boundary(work)
        with tempfile.TemporaryFile(dir=work) as plaintext:
            total=0;count=0
            with zipfile.ZipFile(plaintext,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
                for path in sorted(snapshot.rglob('*')):
                    if path.is_symlink() or path.is_junction():raise ValueError('Snapshot links forbidden')
                    if path.parent==snapshot and path.name=='.fat-fish-assistant-private':continue
                    if not path.is_file():continue
                    name=portable_name(path.relative_to(snapshot).as_posix())
                    count+=1
                    if count>100000:raise ValueError('Snapshot exceeds the supported file-count limit')
                    total+=path.stat().st_size
                    if total>LIMIT:raise ValueError('Expanded snapshot exceeds supported limit')
                    archive.write(path,name)
            if plaintext.tell()>LIMIT:raise ValueError('Compressed snapshot exceeds supported limit')
            plaintext.seek(0)
            with destination.open('xb') as target,zipfile.ZipFile(target,'w',allowZip64=True) as archive:
                with archive.open('snapshot.aesgcm','w',force_zip64=True) as output:
                    output.write(header)
                    while block:=plaintext.read(1024*1024):output.write(encryptor.update(block))
                    output.write(encryptor.finalize());output.write(encryptor.tag)

def unpack(package,destination,password):
    import tempfile
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    from fat_fish_management.instance import protect_boundary
    if destination.exists():raise ValueError('Destination already exists')
    if package.stat().st_size>LIMIT+1024*1024:raise ValueError('Oversized migration package')
    with tempfile.TemporaryDirectory(prefix='migration-crypto-',dir=destination.parent) as temporary:
        work=__import__('pathlib').Path(temporary);protect_boundary(work)
        with tempfile.TemporaryFile(dir=work) as plaintext,zipfile.ZipFile(package) as outer:
            if outer.namelist()!=['snapshot.aesgcm']:raise ValueError('Invalid envelope')
            entry=outer.infolist()[0]
            if entry.file_size>LIMIT+1024:raise ValueError('Oversized envelope')
            with outer.open(entry) as stream:
                header=stream.read(len(MAGIC)+28)
                if len(header)!=len(MAGIC)+28 or not header.startswith(MAGIC):raise ValueError('Unsupported migration format')
                offset=len(MAGIC);salt=header[offset:offset+16];nonce=header[offset+16:offset+28]
                decryptor=Cipher(algorithms.AES(key(password,salt)),modes.GCM(nonce)).decryptor()
                decryptor.authenticate_additional_data(header)
                pending=b'';total=0
                while block:=stream.read(1024*1024):
                    pending+=block;total+=len(block)
                    if total>LIMIT+16:raise ValueError('Oversized encrypted snapshot')
                    if len(pending)>16:plaintext.write(decryptor.update(pending[:-16]));pending=pending[-16:]
                if len(pending)!=16:raise ValueError('Truncated authentication tag')
                plaintext.write(decryptor.finalize_with_tag(pending))
            # Authentication must finish before reading ZIP metadata or restoring files.
            plaintext.seek(0)
            with zipfile.ZipFile(plaintext) as archive:
                seen=set();total=0
                for item in archive.infolist():
                    name=portable_name(item.filename)
                    if name.casefold() in seen or item.is_dir() or (item.external_attr>>16)&0o170000==0o120000:
                        raise ValueError('Unsafe or duplicate ZIP entry')
                    seen.add(name.casefold());total+=item.file_size
                    if total>LIMIT or len(seen)>100000:raise ValueError('Expanded snapshot exceeds limits')
                manifest=json.loads(archive.read('manifest.json'))
                if manifest.get('schema')!=2:raise ValueError('Unsupported snapshot schema')
                for name in seen:
                    if any(parent.as_posix().casefold() in seen for parent in PurePosixPath(name).parents if str(parent)!='.'):
                        raise ValueError('ZIP file/directory path collision')
                destination.mkdir(parents=True);protect_boundary(destination)
                for item in archive.infolist():
                    target=destination/item.filename;target.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(item) as original,target.open('xb') as output:
                        __import__('shutil').copyfileobj(original,output,length=1024*1024)

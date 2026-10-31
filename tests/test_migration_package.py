import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cryptography.exceptions import InvalidTag
import migration_package as m

class Envelope(unittest.TestCase):
    def malicious(self,root,entries):
        import io,json,os,zipfile
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json',json.dumps({'schema':2}))
            for name,value in entries:z.writestr(name,value)
        salt=os.urandom(16);nonce=os.urandom(12);header=m.MAGIC+salt+nonce
        encrypted=AESGCM(m.key('synthetic-password',salt)).encrypt(nonce,buffer.getvalue(),header)
        package=root/'malicious.zip'
        with zipfile.ZipFile(package,'w') as z:z.writestr('snapshot.aesgcm',header+encrypted)
        return package

    def test_unsafe_authenticated_entries(self):
        for entries in [ [('../outside','x')],[('C:/outside','x')],[('payload/x:stream','x')],[('payload/CON.txt','x')],[('payload/x.','x')],[('payload/a','a'),('payload/A','b')],[('payload/a','a'),('payload/a/b','b')] ]:
            with self.subTest(entries=entries),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);package=self.malicious(root,entries)
                with self.assertRaises(ValueError):m.unpack(package,root/'target','synthetic-password')
                self.assertFalse((root/'target').exists())

    def test_symbolic_link_entry(self):
        import zipfile
        entry=zipfile.ZipInfo('payload/link');entry.external_attr=(0o120777<<16)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);package=self.malicious(root,[(entry,'../../outside')])
            with self.assertRaises(ValueError):m.unpack(package,root/'target','synthetic-password')
            self.assertFalse((root/'target').exists())

    def test_decompression_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);package=self.malicious(root,[('payload/expanded','x'*10000)])
            with patch.object(m,'LIMIT',4096),self.assertRaises(ValueError):m.unpack(package,root/'target','synthetic-password')
            self.assertFalse((root/'target').exists())

    def test_snapshot_boundary_marker_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'snapshot';source.mkdir()
            (source/'manifest.json').write_text('{"schema":2}')
            (source/'.fat-fish-assistant-private').write_text('Private snapshot')
            package=root/'test.zip';m.pack(source,package,'synthetic-password')
            with patch('instance.protect_boundary') as protect:
                protect.side_effect=lambda p:(p/'.fat-fish-assistant-private').write_text('Protected destination')
                m.unpack(package,root/'target','synthetic-password')
            self.assertEqual((root/'target/.fat-fish-assistant-private').read_text(),'Protected destination')

    def test_standard_aesgcm_interoperability_and_unicode_password(self):
        import io,zipfile
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'snapshot';source.mkdir()
            (source/'manifest.json').write_text('{"schema":2}')
            (source/'private.txt').write_text('synthetic secret')
            package=root/'test.zip';password='合成迁移密码-十二个字符以上'
            m.pack(source,package,password)
            with zipfile.ZipFile(package) as outer:data=outer.read('snapshot.aesgcm')
            offset=len(m.MAGIC);header=data[:offset+28];salt=header[offset:offset+16];nonce=header[offset+16:offset+28]
            plaintext=AESGCM(m.key(password,salt)).decrypt(nonce,data[len(header):],header)
            with zipfile.ZipFile(io.BytesIO(plaintext)) as z:self.assertEqual(z.read('private.txt'),b'synthetic secret')

    def test_roundtrip_and_wrong_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'snapshot';source.mkdir()
            (source/'manifest.json').write_text('{"schema":2}')
            (source/'secret.txt').write_text('synthetic-key')
            package=root/'test.ffa.zip';m.pack(source,package,'synthetic-password')
            self.assertNotIn(b'synthetic-key',package.read_bytes())
            with self.assertRaises(InvalidTag):m.unpack(package,root/'wrong','wrong-password')
            self.assertFalse((root/'wrong').exists())
            with patch('instance.protect_boundary'):m.unpack(package,root/'restored','synthetic-password')
            self.assertEqual((root/'restored/secret.txt').read_text(),'synthetic-key')
            with self.assertRaises(ValueError):m.unpack(package,root/'restored','synthetic-password')
    def test_tamper_rejected(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();(source/'manifest.json').write_text('{"schema":2}')
            package=root/'original.zip';m.pack(source,package,'synthetic-password')
            with zipfile.ZipFile(package) as z:data=bytearray(z.read('snapshot.aesgcm'))
            data[-1]^=1
            bad=root/'bad.zip'
            with zipfile.ZipFile(bad,'w') as z:z.writestr('snapshot.aesgcm',data)
            with self.assertRaises(InvalidTag):m.unpack(bad,root/'target','synthetic-password')
            self.assertFalse((root/'target').exists())

if __name__=='__main__':unittest.main(verbosity=2)

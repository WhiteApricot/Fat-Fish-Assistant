"""Only an unmodified, hash-matched legacy fixture may be retired."""
import contextlib,importlib,json,os,sys,tempfile,zipfile
from pathlib import Path
source=Path(__file__).resolve().parents[1];sys.path.insert(0,str(source/'scripts'))
from instance import initialize
from configure_experience import configure,native_pack_modules,CATALOG,PACK_ID
release=Path(os.environ['FEIYU_TEST_RELEASE']);old=Path(os.environ['FFA_LEGACY_PACK_FIXTURE'])
for name,sha in CATALOG['retired_test_images'].items():
 import hashlib
 assert hashlib.sha256((old/name).read_bytes()).hexdigest()==sha,'Requires the exact synthetic stock fixture'
with tempfile.TemporaryDirectory(prefix='retire-starter-') as temp,contextlib.chdir(Path.cwd()):
 root=Path(temp)/'instance';initialize(root,'100',release,seed_persona=False)
 native=native_pack_modules(root,release)
 archive=Path(temp)/'legacy.zip'
 with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
  for p in old.rglob('*'):
   if p.is_file():z.write(p,p.relative_to(old))
 native.import_pack_archive(archive,set_as_default=True)
 legacy=native.PACKS_DIR/'ffa-original-basic';custom=legacy/'memes/happy/custom-user.txt';custom.write_text('synthetic user resource')
 user_image=legacy/'memes/happy/custom-user.gif';user_bytes=(release/'resources/starter-memes/gifs/001_hehe.gif').read_bytes();user_image.write_bytes(user_bytes)
 config_path=root/'data/config/meme_manager_config.json';plugin_config=json.loads(config_path.read_text(encoding='utf-8-sig'))
 schema=json.loads((root/'data/plugins/meme_manager/_conf_schema.json').read_text(encoding='utf-8-sig'))
 (root/'private/experience-v1.json').write_text('{}')
 plugin_config['generation']['emotion'].update(max_memes_per_message=-1,probability=35)
 plugin_config['generation']['prompt']['head']=schema['generation']['items']['prompt']['items']['head']['default'];config_path.write_text(json.dumps(plugin_config))
 marker=root/'private/experience-v3.json';marker.unlink();configure(root,release)
 assert '可以附一张' in json.loads(config_path.read_text(encoding='utf-8-sig'))['generation']['prompt']['head']
 retained=json.loads(config_path.read_text(encoding='utf-8-sig'))['generation']['emotion'];assert retained['max_memes_per_message']==-1 and retained['probability']==35
 assert custom.read_text()=='synthetic user resource'
 assert user_image.read_bytes()==user_bytes
 assert next(x for x in native.list_installed_packs() if x['id']=='ffa-original-basic')['is_default']
 assert all(not (legacy/name).exists() for name in CATALOG['retired_test_images'])
 # Reimport the exact old fixture separately to verify whole-pack retirement.
 user_image.unlink()
 native.import_pack_archive(archive,overwrite=True,set_as_default=True)
 custom.unlink(missing_ok=True);marker.unlink();configure(root,release)
 assert not legacy.exists()
 assert next(x for x in native.list_installed_packs() if x['id']==PACK_ID)['is_default']
 assert list((root/'private/config-history').rglob('*.zip')),'Retirement backup required'
print('STOCK_PACK_RETIRED_CUSTOM_PACK_RETAINED_PASS')

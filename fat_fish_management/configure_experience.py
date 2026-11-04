"""Prepare reply settings and verified, attributed whale-girl stickers."""
# Direct workers are also launched by filename, including with Python -I.
if __package__ in (None, ''):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import contextlib
import importlib
import hashlib
import json
import os
import secrets
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from fat_fish_management.lifecycle_lock import exclusive

PROJECT=Path(__file__).resolve().parents[1]
CATALOG=json.loads((PROJECT/'manifests/starter-memes.json').read_text(encoding='utf-8'))
PACK_ID=CATALOG['id']
CATEGORIES={x['category']:x['label'] for x in CATALOG['files'] if x.get('category')}
SEGMENTED={'enable':True,'only_llm_result':True,'interval_method':'random','interval':'0.6,1.3','log_base':2.6,'words_count_threshold':600,'split_mode':'regex','regex':r'```[\s\S]*?```|[^\r\n]+','content_cleanup_rule':''}

def native_pack_modules(root,release):
    os.environ['ASTRBOT_ROOT']=str(root);os.chdir(root)
    sys.path[:0]=[str(release/'astrbot'),str(release/'src')]
    name='ffa_starter_meme'
    module=types.ModuleType(name);module.__path__=[str(root/'data/plugins/meme_manager')];sys.modules[name]=module
    sys.path.insert(0,str(PROJECT/'src'))
    from fat_fish.core.local_credentials import install_capture,capture_generated
    install_capture(root)
    storage=importlib.import_module(name+'.backend.packs.storage')
    from astrbot.core import astrbot_config
    capture_generated(root,astrbot_config)
    return storage

def configure(root,release):
    root=root.resolve();release=release.resolve()
    with exclusive(root):
        if any((root/'private'/x).exists() for x in ['RUNNING','DIRTY','upgrade-journal.json']):raise ValueError('请先停止肥鱼助手，再更新首次使用设置')
        marker=root/'private/experience-v3.json'
        if marker.exists():return json.loads(marker.read_text(encoding='utf-8'))|{'already_prepared':True}
        # Upstream imports may initialize and persist the dashboard configuration.
        # Read our editable snapshot afterwards so those hashes are never lost.
        native=native_pack_modules(root,release) if (root/'data/plugins/meme_manager').is_dir() else None
        path=root/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
        history=root/'private/config-history'/('experience-'+secrets.token_hex(6));history.mkdir(parents=True)
        (history/'cmd_config.json').write_bytes(before)
        # Explicit first-use preset; later runs and restored preferences are left alone.
        if not any((root/'private'/name).exists() for name in ['experience-v1.json','experience-v2.json']):
            cfg.setdefault('platform_settings',{})['segmented_reply']=dict(SEGMENTED)
        pack_result='not-installed'
        if (root/'data/plugins/meme_manager').is_dir():
            config_path=root/'data/config/meme_manager_config.json'
            if config_path.exists():
                (history/'meme_manager_config.json').write_bytes(config_path.read_bytes())
                plugin_cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
            else:plugin_cfg={}
            generation=plugin_cfg.setdefault('generation',{})
            emotion=generation.setdefault('emotion',{})
            if 'max_memes_per_message' not in emotion or (emotion['max_memes_per_message']==-1 and not (root/'private/experience-v1.json').exists()):emotion['max_memes_per_message']=1
            if emotion.get('probability',100)==100:emotion['probability']=35
            llm=emotion.setdefault('llm',{})
            if not llm.get('enabled'):llm.update(enabled=True,context_turns=4,inject_persona=True)
            generation.setdefault('message',{})['send_image_as_base64']=True
            prompt=generation.setdefault('prompt',{})
            schema=json.loads((root/'data/plugins/meme_manager/_conf_schema.json').read_text(encoding='utf-8-sig'))
            prompt['quantity_guidance_enabled']=True
            default_head=schema['generation']['items']['prompt']['items']['head']['default']
            old_default=default_head+'\n轻松日常聊天时，只要有贴切分类，优先附一张表情来表达态度。每次最多一张，不连续刷图；严肃求助、悲伤、道歉、用户不想要表情或指定输出格式时不勉强配图。\n'
            if prompt.get('head',default_head) in {default_head,old_default}:prompt['head']=default_head+'\n轻松日常聊天时，只要有贴切分类，可以附一张贴切表情来表达态度。每次最多一张，不连续刷图；严肃求助、悲伤、道歉、用户不想要表情或指定输出格式时不勉强配图。\n'
            config_path.write_text(json.dumps(plugin_cfg,ensure_ascii=False),encoding='utf-8')
            packs=native.list_installed_packs()
            old=next((x for x in packs if x['id']=='ffa-original-basic'),None)
            stock_old=False
            if old:
                old_root=native.PACKS_DIR/'ffa-original-basic'
                expected=CATALOG['retired_test_images']
                all_files={x.relative_to(old_root).as_posix() for x in old_root.rglob('*') if x.is_file()}
                legacy_categories=json.loads((old_root/'memes_data.json').read_text())
                stock_old=(not old_root.is_symlink() and not old_root.is_junction() and not any(x.is_symlink() or x.is_junction() for x in old_root.rglob('*')) and all_files==set(expected)|{'manifest.json','memes_data.json','LICENSE.txt'} and
                    all(not (old_root/name).is_symlink() and hashlib.sha256((old_root/name).read_bytes()).hexdigest()==sha for name,sha in expected.items()) and
                    (old_root/'LICENSE.txt').read_text()=='Original geometric fish artwork. Copyright Fat-Fish-Assistant contributors. GNU AGPL-3.0; see project LICENSE.' and
                    json.loads((old_root/'manifest.json').read_text())=={'schema_version':1,'id':'ffa-original-basic','name':'肥鱼基础表情','version':'1.0.0','description':'肥鱼助手原创几何小鱼表情，可自由添加和替换','categories':{k:{'description':v} for k,v in legacy_categories.items()}} and
                    json.loads((old_root/'memes_data.json').read_text())=={'happy':'开心、认可、庆祝','sad':'难过、遗憾、安慰','surprised':'惊讶、意外','confused':'疑惑、思考','shy':'害羞、收到夸奖','like':'喜欢、感谢、赞同'})
            custom_old_images=True
            if old and not old_root.is_symlink() and not old_root.is_junction():
                stock_names={name for name,sha in expected.items() if (old_root/name).is_file() and not (old_root/name).is_symlink() and hashlib.sha256((old_root/name).read_bytes()).hexdigest()==sha}
                custom_old_images=any(Path(name).suffix.lower() in {'.gif','.png','.jpg','.jpeg','.webp'} and name not in stock_names for name in all_files)
            choose_default=not any(p.get('image_count',0) for p in packs) or bool(old and old.get('is_default') and (stock_old or not custom_old_images))
            resources=release/'resources/starter-memes'
            for row in CATALOG['files']:
                asset=(resources/row['path']).resolve();asset.relative_to(resources.resolve())
                if hashlib.sha256(asset.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Starter sticker integrity mismatch')
            existing=next((p for p in packs if p['id']==PACK_ID),None)
            if not existing:
                manifest={'schema_version':1,'id':PACK_ID,'name':CATALOG['name'],'version':'1.0.0','description':'Dejavu Moe 创作的 DeepSeek 鲸鱼娘同人动图，日常精选 24 张；MIT 许可。','categories':{k:{'description':v} for k,v in CATEGORIES.items()}}
                with tempfile.TemporaryDirectory(prefix='starter-pack-',dir=root/'private') as tmp:
                    archive=Path(tmp)/'whale.zip'
                    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False));z.writestr('memes_data.json',json.dumps(CATEGORIES,ensure_ascii=False))
                        z.write(resources/'LICENSE','LICENSE.txt')
                        z.writestr('SOURCE.txt',CATALOG['source']+'\nCommit: '+CATALOG['commit']+'\nAuthor: '+CATALOG['author']+'\nSelected GIF files retain original content and filenames.\n')
                        for row in CATALOG['files']:
                            if row.get('category'):z.write(resources/row['path'],'memes/'+row['category']+'/'+Path(row['path']).name)
                    native.import_pack_archive(archive,set_as_default=choose_default,preserve_existing_selection=not choose_default)
                # Identical locked starter files share disk blocks with the release.
                for row in CATALOG['files']:
                    if not row.get('category'):continue
                    target=native.PACKS_DIR/PACK_ID/'memes'/row['category']/Path(row['path']).name
                    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==row['sha256']:
                        pending=target.with_name(target.name+'.link-pending')
                        try:os.link(resources/row['path'],pending);pending.replace(target)
                        except OSError:pending.unlink(missing_ok=True)  # Retain the valid imported copy.
            final=next(p for p in native.list_installed_packs() if p['id']==PACK_ID)
            if final.get('image_count',0)<CATALOG['image_count']:raise ValueError('鲸鱼娘表情未完整导入，保留现场，请查看日志')
            if choose_default and not final.get('is_default'):native.set_default_pack(PACK_ID)
            if stock_old:
                native.export_pack_archive('ffa-original-basic',output_dir=str(history),include_semantic=False,export_mode='share')
                native.uninstall_pack('ffa-original-basic')
            elif old:
                # A user may have added files to the former test pack. Retire
                # only byte-identical stock images, retaining every custom file.
                retired=[]
                if not old_root.is_symlink() and not old_root.is_junction():
                    for name,sha in expected.items():
                        candidate=old_root/name
                        if any(p.is_symlink() or p.is_junction() for p in [candidate,*candidate.parents] if p==old_root or old_root in p.parents):continue
                        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest()==sha:retired.append(candidate)
                if retired:
                    with zipfile.ZipFile(history/'retired-test-images.zip','w') as archive:
                        for candidate in retired:archive.write(candidate,candidate.relative_to(old_root))
                    for candidate in retired:candidate.unlink()
            pack_result='ready'
        pending=path.with_suffix('.pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
        result={'segmented_reply':True,'starter_pack':pack_result,'starter_images':len(CATEGORIES) if pack_result=='ready' else 0,'permissions_changed':False}
        marker.write_text(json.dumps(result),encoding='utf-8');return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--instance',required=True,type=Path);p.add_argument('--release',required=True,type=Path);a=p.parse_args()
    with contextlib.redirect_stdout(sys.stderr):result=configure(a.instance,a.release)
    print(json.dumps(result))

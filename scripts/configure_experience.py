"""Prepare first-use reply settings and an original pack through native APIs."""
import argparse
import contextlib
import importlib
import io
import json
import os
import secrets
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from lifecycle_lock import exclusive

PACK_ID='ffa-original-basic'
CATEGORIES={'happy':'开心、认可、庆祝','sad':'难过、遗憾、安慰','surprised':'惊讶、意外','confused':'疑惑、思考','shy':'害羞、收到夸奖','like':'喜欢、感谢、赞同'}
SEGMENTED={'enable':True,'only_llm_result':True,'interval_method':'random','interval':'0.6,1.3','log_base':2.6,'words_count_threshold':600,'split_mode':'regex','regex':r'```[\s\S]*?```|[^\r\n]+','content_cleanup_rule':''}

def draw_fish(mood):
    """Original geometric illustrations, rendered locally; no external artwork."""
    from PIL import Image,ImageDraw
    image=Image.new('RGBA',(256,256),(255,255,255,0));d=ImageDraw.Draw(image)
    ink='#263a49';colors={'happy':'#ffd887','sad':'#a8d5ef','surprised':'#f7c6a8','confused':'#c4b9e8','shy':'#f4b8ca','like':'#abe0ca'}
    d.ellipse((31,210,218,230),fill='#e9edf1')
    d.polygon([(190,122),(237,83),(232,170)],fill=colors[mood],outline=ink,width=5)
    d.ellipse((26,57,207,211),fill=colors[mood],outline=ink,width=5)
    d.polygon([(99,62),(117,36),(145,61)],fill=colors[mood],outline=ink,width=5)
    d.arc((154,123,183,161),80,260,fill=ink,width=4)
    if mood in {'happy','like'}:
        d.arc((59,102,85,127),190,350,fill=ink,width=5);d.arc((111,102,137,127),190,350,fill=ink,width=5)
        d.arc((83,129,118,157),0,180,fill=ink,width=5)
    elif mood=='sad':
        d.line((59,111,82,118),fill=ink,width=5);d.line((112,118,135,111),fill=ink,width=5)
        d.arc((85,149,117,169),190,350,fill=ink,width=4);d.ellipse((126,126,137,144),fill='#549ddd')
    else:
        d.ellipse((65,109,76,124),fill=ink);d.ellipse((117,109,128,124),fill=ink)
        if mood=='surprised':d.ellipse((91,140,108,161),outline=ink,width=4)
        elif mood=='confused':d.line((89,151,112,147),fill=ink,width=4)
        else:d.arc((88,136,115,151),0,180,fill=ink,width=4)
    if mood in {'shy','like'}:
        d.ellipse((48,133,77,149),fill='#ed8d9a');d.ellipse((124,133,150,149),fill='#ed8d9a')
    output=io.BytesIO();image.save(output,format='PNG');return output.getvalue()

def native_pack_modules(root,release):
    os.environ['ASTRBOT_ROOT']=str(root);os.chdir(root)
    sys.path[:0]=[str(release/'astrbot'),str(release/'src')]
    name='ffa_starter_meme'
    module=types.ModuleType(name);module.__path__=[str(root/'data/plugins/meme_manager')];sys.modules[name]=module
    return importlib.import_module(name+'.backend.packs.storage')

def configure(root,release):
    root=root.resolve();release=release.resolve()
    with exclusive(root):
        if any((root/'private'/x).exists() for x in ['RUNNING','DIRTY','upgrade-journal.json']):raise ValueError('请先停止肥鱼助手，再更新首次使用设置')
        marker=root/'private/experience-v1.json'
        if marker.exists():return json.loads(marker.read_text(encoding='utf-8'))|{'already_prepared':True}
        path=root/'data/cmd_config.json';before=path.read_bytes();cfg=json.loads(before.decode('utf-8-sig'))
        history=root/'private/config-history'/('experience-'+secrets.token_hex(6));history.mkdir(parents=True)
        (history/'cmd_config.json').write_bytes(before)
        # Explicit first-use preset; later runs and restored preferences are left alone.
        cfg.setdefault('platform_settings',{})['segmented_reply']=dict(SEGMENTED)
        pack_result='not-installed'
        if (root/'data/plugins/meme_manager').is_dir():
            config_path=root/'data/config/meme_manager_config.json'
            if config_path.exists():
                (history/'meme_manager_config.json').write_bytes(config_path.read_bytes())
                plugin_cfg=json.loads(config_path.read_text(encoding='utf-8-sig'))
            else:plugin_cfg={}
            emotion=plugin_cfg.setdefault('generation',{}).setdefault('emotion',{})
            if emotion.get('max_memes_per_message',-1)==-1:emotion['max_memes_per_message']=1
            config_path.write_text(json.dumps(plugin_cfg,ensure_ascii=False),encoding='utf-8')
            native=native_pack_modules(root,release)
            packs=native.list_installed_packs()
            existing=next((p for p in packs if p.get('id')==PACK_ID),None)
            if not existing:
                manifest={'schema_version':1,'id':PACK_ID,'name':'肥鱼基础表情','version':'1.0.0','description':'肥鱼助手原创几何小鱼表情，可自由添加和替换','categories':{k:{'description':v} for k,v in CATEGORIES.items()}}
                with tempfile.TemporaryDirectory(prefix='starter-pack-',dir=root/'private') as tmp:
                    archive=Path(tmp)/'original.zip'
                    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                        z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False));z.writestr('memes_data.json',json.dumps(CATEGORIES,ensure_ascii=False))
                        z.writestr('LICENSE.txt','Original geometric fish artwork. Copyright Fat-Fish-Assistant contributors. GNU AGPL-3.0; see project LICENSE.')
                        for mood in CATEGORIES:z.writestr('memes/'+mood+'/fat-fish.png',draw_fish(mood))
                    native.import_pack_archive(archive,set_as_default=not any(p.get('image_count',0) for p in packs),preserve_existing_selection=any(p.get('image_count',0) for p in packs))
            final=next(p for p in native.list_installed_packs() if p.get('id')==PACK_ID)
            if final.get('image_count',0)<len(CATEGORIES):raise ValueError('基础表情未完整导入，保留现场，请查看日志')
            pack_result='ready'
        pending=path.with_suffix('.pending');pending.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf-8');pending.replace(path)
        result={'segmented_reply':True,'starter_pack':pack_result,'original_images':len(CATEGORIES) if pack_result=='ready' else 0,'permissions_changed':False}
        marker.write_text(json.dumps(result),encoding='utf-8');return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--instance',required=True,type=Path);p.add_argument('--release',required=True,type=Path);a=p.parse_args()
    with contextlib.redirect_stdout(sys.stderr):result=configure(a.instance,a.release)
    print(json.dumps(result))

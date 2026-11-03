"""Durable reminder state. Native AstrBot scheduler drives tick; no scheduler here."""
import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime
from contextlib import closing
from ffa_components.reminders.timing import tomorrow_date, parse_time,next_time,display,TZ

class ReminderStore:
    def __init__(self,path):
        self.path=path
        with closing(self.connect()) as db:
            if db.execute('PRAGMA user_version').fetchone()[0]>1:raise ValueError('提醒数据库版本高于当前插件，拒绝降级写入')
            db.executescript('''CREATE TABLE IF NOT EXISTS reminders(
            id TEXT PRIMARY KEY, creator TEXT, target TEXT, scope TEXT, platform TEXT,
            epoch INTEGER, body TEXT, due REAL, rule TEXT, state TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT,due REAL,state TEXT,error TEXT,
            created REAL,PRIMARY KEY(id,due));
            CREATE TABLE IF NOT EXISTS authorization_failures(id TEXT PRIMARY KEY,count INTEGER,error TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS reminder_numbers(number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL);
            INSERT INTO reminder_numbers(id)
            SELECT id FROM reminders WHERE id NOT IN (SELECT id FROM reminder_numbers) ORDER BY due,id;
            CREATE TRIGGER IF NOT EXISTS reminder_number_on_insert AFTER INSERT ON reminders
            BEGIN INSERT INTO reminder_numbers(id) VALUES(new.id); END;
            CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state,due);
            PRAGMA user_version=1;''');db.commit()
            # A crash after reservation may have sent: never blindly resend.
            db.execute("UPDATE deliveries SET state='unknown',error='restart during delivery' WHERE state='sending'");db.commit()
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row;return db
    def rows(self):
        with closing(self.connect()) as db:return [dict(r) for r in db.execute('SELECT reminders.*, reminder_numbers.number FROM reminders JOIN reminder_numbers USING(id) ORDER BY due,number')]
    def allowed(self,row,actor,policy,group):
        if row['scope']!=group:return False
        if actor in policy.blocked_for(group):return False
        if not group:return row['creator']==actor
        return row['creator']==actor or actor==policy.owner or actor in policy.global_admins or (bool(group) and policy.authorize(actor,'group',private=False,group=group))
    def remember_undo(self,db,event,ident,before):
        if hasattr(event,'set_extra'):
            after=db.execute('SELECT * FROM reminders WHERE id=?',(ident,)).fetchone()
            event.set_extra('_p2f_reminder_undo',(before,dict(after)))

    def restore_recent(self,gateway,event,cfg,policy,task):
        before,after=task
        ident=after['id']
        actor=str(event.get_sender_id())
        group=str(event.get_group_id() or '')
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM reminders WHERE id=?',(ident,)).fetchone()
            if not row or dict(row)!=after or not self.allowed(row,actor,policy,group):
                return '提醒状态或权限已变化，未撤销。'
            due=before['due'] if before else after['due']
            if db.execute("SELECT 1 FROM deliveries WHERE id=? AND due IN (?,?)",(ident,due,after['due'])).fetchone():
                return '提醒已进入发送流程，不能撤销。'
            if before and before['state']=='active' and due<=time.time():
                return '原提醒时间已过，请指定新时间。'
            if before:
                db.execute('UPDATE reminders SET body=?,due=?,rule=?,state=?,epoch=?,updated=? WHERE id=?',
                           (before['body'],due,before['rule'],before['state'],gateway.epoch(cfg,group),time.time(),ident))
            else:
                db.execute("UPDATE reminders SET state='deleted',updated=? WHERE id=?",(time.time(),ident))
            db.commit()
        return '已撤销最近的提醒操作。'

    def command(self,gateway,event,args,*,access=None):
        cfg,policy=gateway.load() if access is None else access
        actor=str(event.get_sender_id());group=str(event.get_group_id() or '')
        if policy.level(actor,True,group)<0:return '当前权限不足，未执行'
        if not args or args[0]=='help':return '/remind add 时间提醒我内容：创建提醒\n/remind addto QQ 时间提醒内容：为群成员创建提醒\n/remind list：查看提醒\n/remind show 编号：查看详情\n/remind time 编号 时间：调整时间\n/remind text 编号 内容：修改内容\n/remind pause|resume 编号：暂停或恢复\n/remind delete 编号：删除提醒'
        operation=args[0]
        if operation=='audio':return 'QQ电话及自定义音频暂缓；文字提醒不接收或保存上传音频'
        rows=[r for r in self.rows() if self.allowed(r,actor,policy,group)]
        if operation=='list':return '\n'.join(self.format(r) for r in rows if r['state']!='deleted') or '当前会话没有可管理的提醒'
        if operation=='select':
            label=args[1] if len(args)>1 else ''
            selected=[r for r in rows if r['creator']==actor and r['state'] not in {'deleted','done'}]
            if label=='明天':
                tomorrow=tomorrow_date(datetime.now(TZ))
                selected=[r for r in selected if datetime.fromtimestamp(r['due'],TZ).date()==tomorrow]
            elif label.startswith('每周') and len(label)==3 and label[-1] in '一二三四五六日天':
                weekday='一二三四五六日'.index(label[-1].replace('天','日'))
                selected=[r for r in selected if json.loads(r['rule'])['kind']=='weekly' and datetime.fromtimestamp(r['due'],TZ).weekday()==weekday]
            else:selected=[]
            if len(selected)==1:return self.command(gateway,event,['time',selected[0]['id'],args[2]] if len(args)>2 else ['delete',selected[0]['id']],access=(cfg,policy))
            return '请指定提醒编号后执行；匹配不唯一或无法可靠确定：\n'+('\n'.join(self.format(r) for r in (selected or rows)) or '暂无')
        if operation in {'add','addto'}:
            target=actor
            if operation=='addto':
                if len(args)<3 or not args[1].isdecimal():return '请明确指定目标QQ和提醒时间、内容'
                target=args[1]
                if not group or (target!=actor and not policy.authorize(actor,'group',private=False,group=group)):return '只有本群管理者可以为明确指定的其他成员创建提醒'
                if target in policy.blocked_for(group):return '不能提醒黑名单成员'
            values=args[2:] if operation=='addto' else args[1:]
            if getattr(event,'get_extra',lambda *_:False)('_p2f_natural',False):
                if len(values)!=2 or not all(value.strip() for value in values):
                    return '请明确提醒的时间和事项。'
                parts=values
            else:
                text=' '.join(values)
                parts=text.split('提醒我',1) if '提醒我' in text else text.split('提醒',1)
                if len(parts)!=2 or not parts[1].strip():
                    return '格式：/remind add 明天下午提醒我给老师打电话'
            due,rule,note=parse_time(parts[0]);body=parts[1].strip()
            if 'QQ电话' in parts[0] or '拨号' in parts[0]:note='QQ电话暂缓，本任务仅文字提醒；'+note
            if len(body)>2000:return '提醒内容最多2000字'
            if len([r for r in self.rows() if r['creator']==actor and r['state'] not in {'deleted','done'}])>=100:return '提醒数量已达100，请先清理'
            ident=uuid.uuid4().hex[:12]
            with closing(self.connect()) as db:
                db.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,actor,target,group,event.get_platform_id(),gateway.epoch(cfg,group),body,due,json.dumps(rule),'active',time.time()));db.commit()
                self.remember_undo(db,event,ident,None)
            if hasattr(event,'set_extra'):
                event.set_extra('_p2f_reminder_reference',ident)
            return '已创建提醒 '+str(next(row['number'] for row in self.rows() if row['id']==ident))+'：'+display(due)+'；目标QQ：'+target+'；'+note+'；'+body
        if len(args)<2:return '请指定提醒编号'
        row=next((r for r in rows if r['id']==args[1] or str(r['number'])==args[1]),None)
        if row is None:return '没有此编号或无权管理该提醒'
        if hasattr(event,'set_extra'):
            event.set_extra('_p2f_reminder_reference',row['id'])
        if operation=='show':
            with closing(self.connect()) as db:
                logs=[dict(r) for r in db.execute('SELECT due,state,error FROM deliveries WHERE id=? ORDER BY due DESC LIMIT 5',(row['id'],))]
                failure=db.execute('SELECT count,error FROM authorization_failures WHERE id=?',(row['id'],)).fetchone()
            return self.format(row)+'\n执行记录：'+json.dumps(logs,ensure_ascii=False)+('；授权查询失败：'+json.dumps(dict(failure),ensure_ascii=False) if failure else '')
        if row['state']=='deleted':return '该提醒已删除'
        if operation in {'delete','pause','resume'}:
            if operation in {'pause','resume'} and json.loads(row['rule'])['kind']=='once':return '暂停和恢复只适用于循环提醒'
            state={'delete':'deleted','pause':'paused','resume':'active'}[operation]
            due=row['due']
            if operation=='resume' and due<=time.time():due=next_time(due,json.loads(row['rule']),time.time())
            with closing(self.connect()) as db:
                db.execute('UPDATE reminders SET state=?,due=?,epoch=?,updated=? WHERE id=?',(state,due,gateway.epoch(cfg,group),time.time(),row['id']));db.commit()
                if operation=='resume':db.execute('DELETE FROM authorization_failures WHERE id=?',(row['id'],));db.commit()
                self.remember_undo(db,event,row['id'],row)
            return '已'+{'delete':'删除','pause':'暂停','resume':'恢复'}[operation]+'提醒 '+str(row['number'])
        if len(args)<3:return '请提供新的时间或内容'
        value=' '.join(args[2:])
        with closing(self.connect()) as db:
            if operation=='time':
                due,rule,note=parse_time(value)
                old_rule=json.loads(row['rule'])
                if rule['kind']=='once' and old_rule['kind']!='once' and not any(x in value for x in ('明天','今天','后天','明晚','今晚','之前','截止','后','-')):
                    clock=datetime.fromtimestamp(due,TZ)
                    base=datetime.fromtimestamp(row['due'],TZ).replace(hour=clock.hour,minute=clock.minute,second=0,microsecond=0)
                    rule=old_rule;due=base.timestamp()
                    if due<=time.time():due=next_time(due,rule,time.time())
                    note+='；保留原循环规则'
                db.execute("UPDATE reminders SET due=?,rule=?,state='active',epoch=?,updated=? WHERE id=?",(due,json.dumps(rule),gateway.epoch(cfg,group),time.time(),row['id']));result='已修改提醒时间：'+display(due)+'；'+note
                db.execute('DELETE FROM authorization_failures WHERE id=?',(row['id'],))
            elif operation=='text':
                if not value.strip() or len(value)>2000:return '提醒内容应为1至2000字'
                db.execute('UPDATE reminders SET body=?,updated=? WHERE id=?',(value,time.time(),row['id']));result='已修改提醒内容：'+value
            else:return '不支持的提醒操作'
            db.commit()
            self.remember_undo(db,event,row['id'],row)
            return result
    def format(self,row):
        state={'active':'已激活','paused':'已暂停','done':'已完成','deleted':'已删除','failed':'失败','unknown':'送达待确认','revoked':'已撤权'}.get(row['state'],row['state'])
        rule=json.loads(row['rule'])
        cycle={'once':'不循环','daily':'每天','weekly':'每周','monthly':'每月'}.get(rule['kind'],f"每隔{rule.get('seconds','?')}秒")
        return f"【{state}】编号：{row['number']}；时间：{display(row['due'])}；提醒内容：{row['body']}；{cycle}。"


    async def tick(self,authorize,send,now=None):
        now=time.time() if now is None else now
        for row in self.rows():
            if row['state']!='active' or row['due']>now:continue
            # Authorization precedes reservation and is repeated immediately before I/O.
            try:permitted=await authorize(row)
            except Exception as exc:
                with closing(self.connect()) as db:
                    db.execute('INSERT INTO authorization_failures VALUES(?,1,?,?) ON CONFLICT(id) DO UPDATE SET count=count+1,error=excluded.error,updated=excluded.updated',(row['id'],type(exc).__name__,now))
                    count=db.execute('SELECT count FROM authorization_failures WHERE id=?',(row['id'],)).fetchone()[0]
                    if count>=3:db.execute("UPDATE reminders SET state='failed' WHERE id=?",(row['id'],))
                    db.commit()
                continue
            with closing(self.connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT * FROM reminders WHERE id=?',(row['id'],)).fetchone()
                if not current or current['state']!='active' or current['due']!=row['due'] or current['updated']!=row['updated']:continue
                if not permitted:
                    db.execute("UPDATE reminders SET state='revoked' WHERE id=?",(row['id'],))
                    db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,'revoked','authority no longer valid',?)",(row['id'],row['due'],now));db.commit();continue
                db.execute('DELETE FROM authorization_failures WHERE id=?',(row['id'],))
                inserted=db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,'sending','',?)",(row['id'],row['due'],now)).rowcount
                db.commit()
            if not inserted:
                with closing(self.connect()) as db:
                    prior=db.execute('SELECT state FROM deliveries WHERE id=? AND due=?',(row['id'],row['due'])).fetchone()
                self.advance(row,now,prior['state'] if prior else 'unknown');continue
            state,error='sent',''
            try:
                if not await authorize(row):state='revoked'
                else:
                    async with asyncio.timeout(15):
                        if await send(row) is False:state='revoked'
            except asyncio.CancelledError:
                self.finish(row,'unknown','cancelled during delivery');raise
            except Exception as exc:
                state,error='unknown',type(exc).__name__
            self.finish(row,state,error)
            self.advance(row,now,state)
    def finish(self,row,state,error):
        with closing(self.connect()) as db:
            db.execute('UPDATE deliveries SET state=?,error=? WHERE id=? AND due=?',(state,error,row['id'],row['due']));db.commit()
    def advance(self,row,now,result='sent'):
        due=next_time(row['due'],json.loads(row['rule']),now)
        state='revoked' if result=='revoked' else 'active' if due else 'done' if result=='sent' else 'unknown'
        with closing(self.connect()) as db:
            db.execute('UPDATE reminders SET due=?,state=? WHERE id=? AND due=? AND updated=?',(due or row['due'],state,row['id'],row['due'],row['updated']));db.commit()

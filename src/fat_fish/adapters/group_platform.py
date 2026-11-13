"""Trusted QQ group roles and passive notices."""
import asyncio
import json
import time

class GroupPlatform:
    def __init__(self):
        self.cache={}
        self.observed={}

    def observe(self, group, sender, name, raw):
        role=raw.get('sender',{}).get('role') if isinstance(raw,dict) else None
        if role not in {'owner','admin','member'}:role='unknown'
        people=self.observed.setdefault(group,{})
        people[sender]={'name':str(name)[:80],'role':role,'time':time.monotonic()}
        if len(people)>2000:people.pop(next(iter(people)))
        return {'owner':'群主','admin':'群管理员','member':'群成员','unknown':'群身份未确认'}[role]

    async def roster(self,group,bot,max_age=300):
        now=time.monotonic();entry=self.cache.get(group)
        if max_age==0 or entry is None or now-entry[0]>max_age:
            try:
                rows=await asyncio.wait_for(bot.call_action('get_group_member_list',group_id=int(group),no_cache=max_age==0),3)
                if not isinstance(rows,list):raise ValueError('Malformed member list')
                people={str(x['user_id']):{'name':str(x.get('card') or x.get('nickname') or '群友')[:80],'role':x.get('role','unknown')} for x in rows if isinstance(x,dict) and str(x.get('user_id','')).isdigit()}
                self.cache[group]=(now,people);entry=self.cache[group]
            except Exception:
                return None
        return entry[1]

    async def summary(self,group,bot,blocked,label):
        people=await self.roster(group,bot)
        if people is None:
            return '群主和群管理员名单本次未能核实'
        result=[]
        for ident,info in people.items():
            if ident not in blocked and info['role'] in {'owner','admin'}:
                result.append({'发言者':label(ident),'昵称':info['name'],'QQ群身份':'群主' if info['role']=='owner' else '群管理员'})
        return json.dumps(result,ensure_ascii=False)

    async def notice(self,group,raw,bot,blocked,label):
        kind=raw.get('notice_type');user=str(raw.get('user_id',''));operator=str(raw.get('operator_id',''))
        if user in blocked or operator in blocked:return None
        labels={'group_increase':'成员加入了群聊','group_decrease':'成员离开了群聊','group_admin':'群管理员身份发生变更','group_ban':'群禁言状态发生变更','group_card':'群名片发生变更'}
        if kind not in labels:return None
        if kind in {'group_increase','group_decrease','group_admin','group_card'}:self.cache.pop(group,None)
        name=self.observed.get(group,{}).get(user,{}).get('name')
        if not name:
            try:
                info=await asyncio.wait_for(bot.call_action('get_stranger_info',user_id=int(user)),2)
                if isinstance(info,dict):name=info.get('nickname') or info.get('nick')
            except Exception:pass
        return json.dumps({'来源':'QQ平台系统通知','事件':labels[kind],'成员':label(user),'昵称':str(name or '未取得昵称')[:80],'子类型':str(raw.get('sub_type','')),'禁言秒数':raw.get('duration') if kind=='group_ban' else None},ensure_ascii=False)

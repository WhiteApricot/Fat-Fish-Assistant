"""Small explicit Chinese time policy, Asia/Taipei (UTC+08, no DST)."""
import re
import calendar
from datetime import datetime,timedelta,timezone

TZ=timezone(timedelta(hours=8),'Asia/Taipei')
NUM={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
def number(value):
    if value.isdecimal():return int(value)
    if '十' in value:
        a,b=value.split('十');return (NUM.get(a,1)*10)+NUM.get(b,0)
    return NUM.get(value,-1)

def next_time(stamp,rule,after):
    """Strictly after boundary; monthly 29/30/31 skips absent days."""
    base=datetime.fromtimestamp(stamp,TZ);now=datetime.fromtimestamp(after,TZ)
    kind=rule.get('kind','once')
    if kind=='once':return None
    if kind=='interval':return stamp+(int((after-stamp)//rule['seconds'])+1)*rule['seconds']
    if kind in {'daily','weekly'}:
        delta=1 if kind=='daily' else 7
        return (base+timedelta(days=max(1,(now.date()-base.date()).days//delta+1)*delta)).timestamp()
    year,month=now.year,now.month
    for _ in range(240):
        if rule['day']<=calendar.monthrange(year,month)[1]:
            value=datetime(year,month,rule['day'],base.hour,base.minute,tzinfo=TZ)
            if value>now:return value.timestamp()
        month+=1
        if month==13:year+=1;month=1
    raise ValueError('无法计算下一次时间')

def parse_time(text,now=None):
    now=now or datetime.now(TZ)
    if now.tzinfo is None:now=now.replace(tzinfo=TZ)
    now=now.astimezone(TZ)
    rule={'kind':'once'};note=''
    text=text.replace('半分钟后','30秒后').replace('半小时后','30分钟后')
    rel=re.search(r'(?<![\d.])(\d+)\s*(秒|分钟|小时|天)(?:后|以后)',text) or re.search(r'每隔(\d+)\s*(分钟|小时|天)',text)
    if rel:
        seconds=int(rel[1])*{'秒':1,'分钟':60,'小时':3600,'天':86400}[rel[2]]
        if seconds<=0 or seconds>366*86400:raise ValueError('相对时间应在1秒至366天内')
        if '每隔' in text or '每' in text:rule={'kind':'interval','seconds':seconds}
        return (now+timedelta(seconds=seconds)).timestamp(),rule,'采用明确相对时间'
    if '过会儿' in text or '一会儿' in text:
        minutes=5 if '紧急' in text else 30
        return (now+timedelta(minutes=minutes)).timestamp(),rule,'“过会儿”默认'+str(minutes)+'分钟后'
    week=re.search(r'每(?:周|星期)([一二三四五六日天1-7])',text)
    month=re.search(r'每月(\d{1,2})[日号]',text)
    if week:rule={'kind':'weekly'}
    elif month:
        if not 1<=int(month[1])<=31:raise ValueError('月份日期无效')
        rule={'kind':'monthly','day':int(month[1])}
    elif '每天' in text or '每日' in text:rule={'kind':'daily'}
    day=now.date()
    absolute=re.search(r'(\d{4})-(\d{2})-(\d{2})',text)
    if absolute:day=datetime(*map(int,absolute.groups()),tzinfo=TZ).date()
    elif '后天' in text:day+=timedelta(days=2)
    elif '明天' in text or '明晚' in text:day+=timedelta(days=1)
    elif week:
        weekday='一二三四五六日'.find(week[1].replace('天','日')) if not week[1].isdigit() else int(week[1])-1
        day+=timedelta(days=(weekday-now.weekday())%7)
    clock=re.search(r'([零一二两三四五六七八九十\d]{1,3})(?:点|时|:|：)([零一二三四五六七八九十\d]{1,3})?(分|半)?',text)
    if clock:
        hour=number(clock[1]);minute=30 if clock[3]=='半' else number(clock[2]) if clock[2] else 0
        if any(x in text for x in ('下午','晚上','今晚','明晚')) and 1<=hour<12:hour+=12
        if '中午' in text and hour<11:hour+=12
        if not 0<=hour<=23 or not 0<=minute<=59:raise ValueError('时间无效，请使用24小时制时间')
    else:
        hour=20 if any(x in text for x in ('晚上','今晚','明晚','晚')) else 15 if '下午' in text else 12 if '中午' in text else 9
        minute=0;note='未给精确时刻，采用默认'+str(hour)+':00'
        if not any(x in text for x in ('今天','明天','后天','今晚','明晚','上午','下午','晚上','中午','每天','每日','每周','每星期','每月')) and not absolute:
            raise ValueError('请提供提醒时间，例如“明天下午”或“30分钟后”')
    value=datetime.combine(day,datetime.min.time(),TZ).replace(hour=hour,minute=minute)
    if month:
        value=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if month and int(month[1])<=calendar.monthrange(now.year,now.month)[1]:
            candidate=value.replace(day=int(month[1]))
            if candidate>now:value=candidate
            else:value=datetime.fromtimestamp(next_time(value.timestamp(),rule,now.timestamp()),TZ)
        else:value=datetime.fromtimestamp(next_time(value.timestamp(),rule,now.timestamp()),TZ)
    elif value<=now:
        if rule['kind']!='once':value=datetime.fromtimestamp(next_time(value.timestamp(),rule,now.timestamp()),TZ)
        elif not clock and ('今晚' in text or '今天晚上' in text):value=now+timedelta(minutes=30);note+='；默认时刻已过，改为30分钟后'
        elif absolute or '今天' in text:raise ValueError('指定时间已经过去，请提供未来时间')
        else:value+=timedelta(days=1);note+='；该时刻已过，采用下一日'
    if '之前' in text or '截止' in text or '以前' in text:
        value=max(now+timedelta(minutes=5),value-timedelta(hours=2));note+='；截止范围提前2小时提醒，不作为精确截止时间'
    return value.timestamp(),rule,note or '采用明确时间'

def display(stamp):return datetime.fromtimestamp(stamp,TZ).strftime('%Y-%m-%d %H:%M:%S Asia/Taipei')

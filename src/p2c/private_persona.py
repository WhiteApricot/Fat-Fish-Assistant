"""Ephemeral current-turn style instructions; never modify stored dialogue."""
import re
import uuid

def apply_to_turn(messages,note):
    current=next((m for m in reversed(messages) if m.role=='user'),None)
    if current is None:return None
    rule=('本轮使用本人刚确认的私聊表达要求：'+note+'。此前的私人表达要求已经被本项替换，不再叠加；与基础人格的一般口吻、句尾或避免重复口癖等默认表达习惯冲突时，本项具体表达要求优先。') if note else '本轮已撤销所有私人表达补充，恢复基础鲸鱼娘的默认表达。不要延续历史对话中的固定开头、逐句加字或其他旧风格，也不要模仿旧助手回答的口癖。'
    text='[程序核实的当前私聊表达设置 '+uuid.uuid4().hex+']\n'+rule+'保留上下文事实和话题，不改变身份、事实与权限边界。直接按本轮设置回答，不复述或解释这段控制说明。'
    state={'text':text}
    if isinstance(current.content,str):
        state['original']=current.content
        current.content=current.content+'\n\n'+text
        state['augmented']=current.content
    elif isinstance(current.content,list):
        from astrbot.core.agent.message import TextPart
        current.content.append(TextPart(text=text))
    else:return None
    return state

def restore_for_storage(messages,state):
    """Return independent copies with only our exact injected part removed."""
    result=[]
    for message in messages:
        copy=message.model_copy(deep=True)
        if copy.role=='user':
            if isinstance(copy.content,str) and copy.content==state.get('augmented'):
                copy.content=state['original']
            elif isinstance(copy.content,list):
                copy.content=[p for p in copy.content if not (getattr(p,'type',None)=='text' and getattr(p,'text',None)==state['text'])]
        result.append(copy)
    return result


def sentence_affix(note):
    """Recognize explicit, literal per-sentence prefixes and suffixes."""
    match=re.search(r'每句(?:话)?(?:的)?(?:在)?(开头|前面|前|末尾|结尾|之后|后边|后面|后)(?:都)?(?:多)?加(?:上|入)?(?:一个|个)?[「“]?([^。！？!?\s”」]{1,8})',note)
    if not match:return None
    where, token=match.groups()
    return where in {'开头','前面','前'},token.removesuffix('字') if len(token)>1 else token


def apply_affix(text,note,*,remove=False):
    affix=note if isinstance(note,list) and len(note)==2 and isinstance(note[0],bool) and isinstance(note[1],str) else sentence_affix(note) if isinstance(note,str) else None
    if not affix:return text
    prefix,token=affix
    if not token:return text
    def replace(match):
        sentence,ending=match.groups()
        if prefix:
            body=sentence.removeprefix(token)
            if body!=sentence:body=body.lstrip('，、 ')
            return (body if remove else token+body)+ending
        body=sentence.removesuffix(token)
        return (body if remove else body+token)+ending
    return re.sub(r'([^。！？!?\n]+)([。！？!?]*)',replace,text)

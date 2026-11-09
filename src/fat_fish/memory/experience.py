"""Small runtime policies around existing meme_manager and LivingMemory APIs."""
import asyncio
import functools
import json
import re
import time
from collections import OrderedDict

MEMORY_INSTRUCTION = '''
[当前长期记忆策略]
不必等本人说“记住”。本人明确陈述长期偏好、习惯、重要关系、持续项目或长期计划，
且对未来交流有价值时，主动调用已有 memorize_long_term_memory，简洁保存事实。
每轮最多写一条（memory<=150字、key_facts<=2条），无合适信息就不调用。
普通问答、临时安排、当前情绪、玩笑、假设、转述、测试、机器人猜测不保存。
同一事实已经存在则不重复；本人纠正旧事实时，以最新内容为准，并提示用
/记忆更新 编号 新内容 纠正原记录；忘记单条用 /记忆删除 编号，只能作用当前会话。
密码、口令、验证码、Token、API密钥、私钥、Cookie、授权头等凭据一律不写入记忆，
即便本人要求记住也拒绝。群聊资料必须保留发言者归属，不猜测私人身份或跨会话写入。
用户说不要记住时不调用工具。只有工具返回成功才可说已长期记住；自动提取不必每次播报。
'''


def contains_secret(value):
    if isinstance(value, dict):
        return any((bool(re.fullmatch(r'password|passwd|token|api[_ -]?key|client[_ -]?secret|cookie|authorization|密码|口令|验证码|密钥|令牌', str(key), re.I)) and bool(item)) or contains_secret(item) for key,item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item) for item in value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return bool(re.search(
        r'(?i)(?:sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{16,}|'
        r'-----BEGIN [\w ]*PRIVATE KEY-----|bearer\s+[a-z0-9._~-]{8,}|'
        r'eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+|'
        r'(?:密码|口令|验证码|密钥|令牌|password|passwd|token|api[_ -]?key|'
        r'client[_ -]?secret|cookie|authorization)["\x27]?\s*(?:是|为|[:：=]|\s)\s*\S+|'
        r'(?:密码|口令|验证码|密钥|令牌)[是为:：=\s]*[a-z0-9][a-z0-9_-]{2,})', text))


def durable_content(text):
    return bool(re.search(r'长期|一直|通常|每[天周月年]|习惯|偏[好爱]|最喜欢|不喜欢|过敏|'
                          r'职业|工作是|从事|项目|计划|目标|家人|父亲|母亲|爸爸|妈妈|'
                          r'妻子|丈夫|伴侣|姐姐|妹妹|哥哥|弟弟|朋友|同事|喜欢|爱好|住在|居住|专业|生日', text))


def protect_memorize_tool(tool):
    if tool is None or getattr(tool, '_ffa_candidate_guard', False):
        return
    original = tool.call

    async def call(context, memory, *args, **kwargs):
        event = context.context.event
        text = event.message_str
        if '[P2F_FORWARD_MATERIAL]' in text or event.get_extra('_ffa_material',False):
            return json.dumps({'memorized':False,'error':'Forwarded speakers are external material'},ensure_ascii=False)
        explicit = bool(re.search(r'记住|记一下|记下来|remember', text, re.I))
        denied = bool(re.search(r'不要.{0,8}记|别记|不用记|假设|假如|比如|角色扮演|开玩笑|测试口令|(?:只是|临时|这是).{0,4}测试', text))
        valid = explicit or (durable_content(text) and not text.rstrip().endswith(('？','?')))
        if denied or not valid or contains_secret([text, memory, args, kwargs]):
            return json.dumps({'memorized': False, 'error': 'No safe durable candidate'}, ensure_ascii=False)
        return await original(context, memory, *args, **kwargs)

    tool.call = call
    tool._ffa_candidate_guard = True


def protect_memory_engine(engine):
    """Guard all plugin writes (tools, reflection, UI) without a second store."""
    if getattr(engine, '_ffa_write_guard', False):
        return
    original_add, original_update = engine.add_memory, engine.update_memory
    write_lock = asyncio.Lock()

    @functools.wraps(original_add)
    async def add(content, session_id=None, persona_id=None, importance=0.5, metadata=None, **kwargs):
        if '[P2F_FORWARD_MATERIAL]' in json.dumps(kwargs.get('source_messages'),ensure_ascii=False,default=str):
            raise ValueError('Forwarded material cannot be stored as the current speaker memory')
        if contains_secret({'content': content, 'metadata': metadata or {}, 'sources': kwargs.get('source_messages')}):
            raise ValueError('Credential content cannot be stored in long-term memory')
        direct = (metadata or {}).get('source') == 'explicit_user' or (metadata or {}).get('memory_origin') == 'agent_memorize_tool' or 'previous_id' in (metadata or {})
        if not direct and (float(importance) < 0.65 or not durable_content(content)):
            # Raising is intentional: native tool must never report memorized=true.
            raise ValueError('No durable memory candidate')
        async with write_lock:
            # The native reflection path deduplicates, but its memorize tool
            # calls add_memory directly. Reuse its scoped search/merge APIs here.
            dedup = getattr(engine, 'config', {}).get('memory_dedup', {})
            finder = getattr(engine, 'find_similar_memory', None)
            if direct and 'previous_id' not in (metadata or {}) and finder and dedup.get('enabled', True):
                match = await finder(content, session_id=session_id, persona_id=persona_id,
                                     threshold=dedup.get('similarity_threshold', .92), limit=dedup.get('max_candidates', 3))
                if match:
                    previous = await engine.get_memory(int(match['id']))
                    old_meta = previous.get('metadata') or {}
                    if isinstance(old_meta, str):old_meta=json.loads(old_meta)
                    old_text = previous.get('content', previous.get('text', ''))
                    normalize = lambda value: ''.join(str(value).casefold().split())
                    equal = normalize(old_text)==normalize(content)
                    facts = (metadata or {}).get('key_facts')
                    equal_facts = bool(facts) and facts==old_meta.get('key_facts')
                    # Similarity alone may merge opposite preferences. A changed
                    # fact is retained for correction, never silently discarded.
                    if old_meta.get('session_id')==session_id and old_meta.get('persona_id')==persona_id and (equal or equal_facts):
                        if await engine.merge_duplicate_memory(int(match['id']), importance=importance, metadata=metadata):
                            return int(match['id'])
            return await original_add(content=content, session_id=session_id, persona_id=persona_id,
                                      importance=importance, metadata=metadata, **kwargs)

    @functools.wraps(original_update)
    async def update(memory_id, updates, **kwargs):
        if contains_secret(updates):
            raise ValueError('Credential content cannot be stored in long-term memory')
        if 'content' in updates:
            updates=dict(updates)
            metadata=dict(updates.get('metadata') or {})
            # Native recall prefers persona_summary to the indexed text. Keep
            # it in sync and discard obsolete facts when correcting a record.
            metadata.update(persona_summary=updates['content'],canonical_summary=updates['content'])
            metadata.setdefault('key_facts',[])
            updates['metadata']=metadata
        return await original_update(memory_id, updates, **kwargs)

    engine.add_memory, engine.update_memory = add, update
    original_merge = getattr(engine, 'merge_duplicate_memory', None)
    if original_merge:
        async def merge(memory_id, **kwargs):
            if contains_secret(kwargs):raise ValueError('Credential content cannot be stored in long-term memory')
            return await original_merge(memory_id, **kwargs)
        engine.merge_duplicate_memory = merge
    engine._ffa_write_guard = True


def meme_intent(text):
    requested = bool(re.search(r'(?:发|来|给|用|要|想看).{0,12}(?:表情|动图)', text) or text.strip()=='表情包')
    # '不要只用文字描述' requests an image; it does not forbid memes.
    preference = re.sub(r'(?:不要|别|不用)(?:只|仅)(?:用|发|输出|回复)?(?:纯)?文字(?:描述|回复)?', '', text)
    denied = bool(re.search(
        r'(?:不要|别|不用|不许|禁止)(?:再|给我|给|发|发送|用|配|附带|加|带|\s)*(?:任何)?(?:图片|图|动图|表情(?:包)?)'
        r'|(?:只|仅)(?:要|用|发|输出|回答|回复)?(?:纯)?文字|纯文字'
        r'|只(?:回答|输出)(?:数字|JSON)', preference, re.I))
    return requested, denied


class MemeCadence:
    def __init__(self, clock=time.monotonic):
        self.clock, self.sessions = clock, OrderedDict()

    def apply(self, event):
        selected = event.get_extra('found_emotions', [])
        if not selected:
            scope = event.unified_msg_origin
            if scope in self.sessions:
                last, skipped = self.sessions[scope]
                self.sessions[scope] = (last, skipped+1)
            return
        scope = event.unified_msg_origin
        now = self.clock()
        last, skipped = self.sessions.get(scope, (-1e9, 2))
        text = event.message_str
        requested, denied = meme_intent(text)
        requested = requested or bool(event.get_extra('_ffa_requested_meme_catalog', False))
        if not requested and event.get_extra('meme_manager_legacy_probability_hit') is False:
            return
        if denied or (not requested and (now-last < 45 or skipped < 2)):
            event.set_extra('found_emotions', [])
            event.set_extra('meme_manager_legacy_probability_hit', False)
            self.sessions[scope] = (last, skipped+1)
        else:
            event.set_extra('found_emotions', selected[:1])
            if requested:event.set_extra('meme_manager_legacy_probability_hit', True)
            self.sessions[scope] = (now, 0)
        self.sessions.move_to_end(scope)
        while len(self.sessions) > 512:
            self.sessions.popitem(last=False)

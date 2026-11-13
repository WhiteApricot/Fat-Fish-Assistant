"""Expose the existing meme library only for an explicit, authorized request.

Selection markup and sending remain owned by Meme Manager and AstrBot.
"""
import json
import re
import time
from collections import OrderedDict
from fat_fish.memory.experience import meme_intent


class MemeRequestState:
    """Short-lived request/category only; no messages or account data are archived."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.recent = OrderedDict()

    def key(self, event):
        sender = getattr(event, 'get_sender_id', lambda: '')()
        return event.unified_msg_origin, str(sender)

    def resolve(self, event):
        wanted, denied = meme_intent(event.message_str)
        key = self.key(event)
        previous = self.recent.get(key)
        fresh = previous is not None and self.clock() - previous[0] <= 180
        followup = bool(re.fullmatch(
            r'[\s，,。.!！?？]*(?:(?:请|麻烦|给我|帮我)\s*)?'
            r'(?:(?:再|重新)(?:给我)?(?:来|发|发送)(?:我)?(?:一(?:个|张)|一下)?'
            r'|没(?:有)?(?:看|收)到(?:呀|啊|呢)?[，,。\s]*(?:请)?(?:重新|再)(?:给我)?发(?:送)?(?:一下|一张|一个)?)'
            r'(?:嘛|呀|吧|啊|呗|呢|啦|~|～|[\s，,。.!！?？])*', event.message_str))
        if denied or (not wanted and not (fresh and followup)):
            self.recent.pop(key, None)
            return False
        self.recent[key] = (self.clock(), previous[1] if fresh else None)
        self.recent.move_to_end(key)
        while len(self.recent) > 512:
            self.recent.popitem(last=False)
        return True

    def category(self, event):
        return self.recent.get(self.key(event), (0, None))[1]

    def selected(self, event, category):
        self.recent[self.key(event)] = (self.clock(), category)


def expose_requested_catalog(context, event, request, state=None):
    if event.get_extra('_ffa_platform_admission') is None:
        return False
    if event.get_extra('_ffa_material', False) or '[FFA_FILE_MATERIAL]' in event.message_str:
        return False
    wanted, denied = meme_intent(event.message_str)
    if state is not None:
        wanted = state.resolve(event)
    if not wanted or denied:
        return False
    if event.get_extra('meme_manager_semantic_active', False):
        return False  # The existing semantic tool/selection path already owns this.
    metadata = context.get_registered_star('meme_manager')
    if not metadata or not metadata.activated:
        return False
    plugin = metadata.star_cls
    resolved = plugin._resolve_runtime_pack_context(event=event)
    mapping = resolved.get('category_mapping')
    if not isinstance(mapping, dict):
        mapping = plugin.category_mapping
    catalog = {}
    for category, description in mapping.items():
        if not isinstance(category, str) or not category or len(category) > 100:
            continue
        # List only categories with actual files in this event's bound packs.
        if plugin._get_category_meme_files(event, category):
            catalog[category] = str(description)[:200]
        if len(catalog) >= 64:
            break
    if not catalog:
        request.system_prompt = (request.system_prompt or '') + '\n本轮表情库中没有可发送的图片，请明确说明，不能声称已发送。'
        return False
    request.system_prompt = (request.system_prompt or '') + (
        '\n[FFA：本轮用户明确索要表情包]\n'
        '以下分类已核对确有现成图片，Meme Manager能通过AstrBot发送，'
        '无需你创建或寻找本地文件。请选一个符合本轮用户要求的分类，'
        '在回复末尾独占一行输出 &&分类键&&（分类键必须原样取自下表）。'
        '只选一个，不要输出文件路径，不要另调图片生成工具；'
        '这是发送请求标记，实际发送结果由系统处理，勿提前声称已送达。\n'
        + json.dumps(catalog, ensure_ascii=False)
        + '\n[分类表结束]\n')
    event.set_extra('_ffa_requested_meme_catalog', True)
    event.set_extra('_ffa_meme_catalog', catalog)
    event.set_extra('_ffa_previous_meme_category', state.category(event) if state else None)
    return True


def ensure_requested_selection(event, response, state=None):
    """An explicit request must not depend on the automatic 35% selection path."""
    catalog = event.get_extra('_ffa_meme_catalog', {})
    if event.get_extra('_ffa_platform_admission') is None or not catalog:
        return
    selected = [key for key in (event.get_extra('found_emotions', []) or []) if key in catalog]
    if not selected:
        text = event.message_str + ' ' + str(getattr(response, 'completion_text', '') or '')
        previous = event.get_extra('_ffa_previous_meme_category')
        def score(key):
            words = re.split(r'[\s,，、;；/]+', catalog[key])
            return sum(len(word) for word in words if 1 < len(word) < 16 and word in text)
        ranked = max(catalog, key=score)
        selected = [ranked if score(ranked) else previous if previous in catalog else next(iter(catalog))]
    event.set_extra('found_emotions', selected[:1])
    event.set_extra('meme_manager_legacy_probability_hit', True)
    if state is not None:
        state.selected(event, selected[0])

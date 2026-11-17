"""Bounded same-sender media bridge with authorized STT; no background jobs."""
import copy
import time
import asyncio


class GroupMedia:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = {}
        self.speech = {}

    def forget(self, room):
        for cache in (self.pending, self.speech):
            for key in tuple(cache):
                if key[0] == room:
                    del cache[key]

    def observe(self, event, epoch, *, mentioned, room=None):
        group, sender = room or str(event.get_group_id()), str(event.get_sender_id())
        now = self.clock()
        for key, (stamp, pending) in list(self.pending.items()):
            ttl=90 if pending and all(p.__class__.__name__=='Record' for p in pending) else 12
            if now-stamp > ttl or (key[0] == group and key[1] != epoch):
                self.pending.pop(key, None)
        key = (group, epoch, sender)
        parts = event.get_messages()
        for old,(stamp,_,_) in list(self.speech.items()):
            if now-stamp>90 or (old[0]==group and old[1]!=epoch):self.speech.pop(old,None)
        event.set_extra('_ffa_group_mentioned',mentioned)
        media = [p for p in parts if p.__class__.__name__ in {'Image', 'Record'}]
        if media:
            if mentioned:
                self.pending.pop(key, None)
            else:
                previous = self.pending.get(key, (now, []))[1]
                self.pending[key] = (now, (previous + copy.deepcopy(media))[-4:])
                while len(self.pending) > 256:
                    self.pending.pop(next(iter(self.pending)))
            return
        if mentioned and not any(p.__class__.__name__ == 'Reply' for p in parts):
            cached = self.pending.pop(key, None)
            if cached:
                parts.extend(cached[1])
                event.set_extra('_ffa_group_media_attached', True)
        elif not media and not mentioned:
            # An intervening text topic from this sender ends the voice bridge.
            self.speech.pop(key,None)
            self.pending.pop(key,None)

    async def transcribe(self,event,epoch,context,allowed,*,room=None):
        """Make authorized voice visible before smart decisions and memory capture."""
        key=(room or str(event.get_group_id()),epoch,str(event.get_sender_id()))
        parts=event.get_messages()
        records=[(i,p) for i,p in enumerate(parts) if p.__class__.__name__=='Record']
        mentioned=event.get_extra('_ffa_group_mentioned',False)
        body=''.join(p.text for p in parts if p.__class__.__name__=='Plain').strip()
        cached=self.speech.get(key) if epoch is not None else None
        if not records:
            if mentioned and not body and not any(p.__class__.__name__=='Reply' for p in parts) and cached and self.clock()-cached[0]<=90:
                from astrbot.api.message_components import Plain

                if not allowed():raise asyncio.CancelledError()
                text='[接续本人刚才的语音，请回应其内容]\n'+cached[2]
                parts.append(Plain(text));event.message_str+='\n'+text
                event.message_obj.message_str+='\n'+text
                return True
            return False
        from astrbot.api.message_components import Plain
        from fat_fish.integrations.voice_input import transcribe_record

        if not allowed():raise asyncio.CancelledError()
        changed=False
        for index,record in records:
            ref=(record.file,record.url,record.path)
            cached=self.speech.get(key) if epoch is not None else None
            if cached and self.clock()-cached[0]<=90 and cached[1]==ref:
                text=cached[2]
            else:
                text=await transcribe_record(record,context,event,allowed)
            if not allowed():raise asyncio.CancelledError()
            if not isinstance(text,str) or not text.strip():continue
            text=text.strip()[:4000]
            if epoch is not None:
                self.speech[key]=(self.clock(),ref,text)
                while len(self.speech)>256:self.speech.pop(next(iter(self.speech)))
            rendered = text
            if epoch is not None:
                rendered=('[接续本人刚才的语音，请回应其内容]\n' if mentioned and not body else '[语音转写]\n')+text
            parts[index]=Plain(rendered)
            event.message_str = (event.message_str + '\n' if event.message_str else '') + rendered
            event.message_obj.message_str = event.message_str
            changed=True
        return changed

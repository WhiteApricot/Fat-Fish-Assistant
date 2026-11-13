"""Map trusted DSH host observations. This module opens no network connections."""
from datetime import datetime
import hashlib
import re
import math

from fat_fish.agents.registry import Manifest, Risk, Tool, ToolResult
from fat_fish.communication.model import key
from fat_fish.memory.activity import ActivityKind
from fat_fish.memory.service import encode


MANIFEST = Manifest("dsh-bili-whale", "dsh",
                    ("node", "sessionController", "webServer", "connection"),
                    (Tool("browse", ActivityKind.BROWSE, Risk.READ),
                     Tool("video-content", ActivityKind.READ, Risk.READ),
                     Tool("diary", ActivityKind.READ, Risk.READ),
                     Tool("comment", ActivityKind.COMMENT, Risk.WRITE)),
                    revision="8b0009f8ce2ebdc421afcb2e727b19ac18912ec6",
                    repository="https://github.com/Alterssss/dsh-bili-whale")


class BiliWhaleObservations:
    """Adapter for locally verified tool/diary results, never notification text."""

    def __init__(self, registry, handle):
        self.registry = registry
        self.handle = handle

    def submit(self, *, command, receipt_id, bvid, occurred, title, evidence):
        if command not in {"browse", "video-content", "diary"}:
            raise ValueError("Publication outcomes must come from the operation connector")
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            raise ValueError("Invalid Bilibili video ID")
        if not isinstance(receipt_id, str) or not 0 < len(receipt_id) <= 160:
            raise ValueError("Stable host receipt required")
        event_id = hashlib.sha256(key(command, receipt_id, bvid).encode()).hexdigest()
        label = {"browse": "浏览发现", "video-content": "读取视频", "diary": "记录日记"}[command]
        return self.registry.observe(self.handle, command, event_id=event_id, occurred=occurred,
                                     summary=f"{label} {bvid} {title[:300]}", evidence=evidence)


    def submit_diary(self, row, *, receipt_id, evidence):
        # Upstream whale-log.jsonl stores at/kind/target/note. The note is
        # evidence of a diary entry only, including entries claiming a publish.
        if not isinstance(row, dict) or not {"at", "kind", "target", "note"} <= row.keys():
            raise ValueError("Invalid upstream diary record")
        at = datetime.fromisoformat(row["at"])
        if at.tzinfo is None:
            raise ValueError("Diary record must carry a timezone")
        target = row["target"]
        if not isinstance(target, str):
            raise ValueError("Missing diary video target")
        bvid = target.removeprefix("BV:")
        kind = row["kind"]
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", kind):
            kind = "event"
        return self.submit(command="diary", receipt_id=receipt_id, bvid=bvid,
                           occurred=at.timestamp(), title="类型=" + kind,
                           evidence=evidence)


def comment_result(response, *, go, exit_code, target, account, checked_at, verified_reply=None):
    # The host obtains these independently through the command and read-back API.
    if (not go or not isinstance(response, dict) or type(response.get("code")) is not int
            or not isinstance(target, str) or not re.fullmatch(r"BV[A-Za-z0-9]{10}", target)
            or not isinstance(account, str) or not account
            or not isinstance(checked_at, (int, float)) or not math.isfinite(checked_at) or checked_at <= 0):
        raise ValueError("Unverified comment outcome")
    code = response["code"]
    evidence = {"target": target, "account": account, "checked_at": checked_at}
    if code != 0:
        evidence.update(status="rejected", code=code)
        return ToolResult(False, f"视频评论被平台拒绝（{code}）", "bili:" + encode(evidence))
    data = response.get("data")
    reply = data.get("rpid_str", data.get("rpid")) if isinstance(data, dict) else None
    if (exit_code != 0 or not re.fullmatch(r"[1-9][0-9]{0,31}", str(reply))
            or verified_reply != {"target": target, "account": account, "reply": str(reply)}):
        raise ValueError("Comment outcome requires matching platform read-back")
    evidence.update(status="published", reply=str(reply))
    return ToolResult(True, "已发布视频评论", "bili:" + encode(evidence))

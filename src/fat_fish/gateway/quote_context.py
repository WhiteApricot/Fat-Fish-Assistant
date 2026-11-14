"""Present explicit quotes separately from nearby conversation history."""
from html import escape

from fat_fish.memory.experience import sensitive_information

NEIGHBORS = 6
QUOTE_DEPTH = 4


def excerpt(text, limit=1200):
    text = text.strip()
    return escape(text[:limit]) if text and not sensitive_information(text) else ""


def with_neighbors(quote, before, after, *, ancestry=(), incomplete=False):
    blocks = ["<explicit_quote>" + quote + "</explicit_quote>"]
    if ancestry:
        blocks.append("<quote_ancestors>\n" + "\n".join(ancestry) + "\n</quote_ancestors>")
    if incomplete:
        blocks.append("上游引用未完整取得。需要缺失内容时请用户补充，勿猜测。")
    for label, items in (("before_quote", before), ("after_quote", after)):
        lines = [f"<message sender=\"{escape(sender, quote=True)}\">{body}</message>"
                 for sender, text in items if (body := excerpt(text, 450))]
        lines = lines[-NEIGHBORS:] if label == "before_quote" else lines[:NEIGHBORS]
        if lines:
            blocks.append(f"<{label}>\n" + "\n".join(lines) + f"\n</{label}>")
    return "\n".join(blocks)

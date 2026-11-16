"""TEMPORARY. Mark client observations and correlate them with server traces."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

from probe import STAGES, clock, utc


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", value):
        raise ValueError("Use a test ID such as CHAT-01.")
    return value


def append(path, row):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row) + "\n")


def records(path):
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.endswith("\n"):
                yield json.loads(line)


def marker(path, test, stage, kind, wall=None):
    if wall is None:
        wall, _ = clock()
    append(path, {"test_id": identifier(test), "stage": stage, "delivery_kind": kind,
                  "utc": utc(wall), "source": "manual_client_observation"})


def window(path):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("FFA 飞书计时")
    test = tk.StringVar()
    message = tk.StringVar(value="输入唯一测试编号。")
    ttk.Entry(root, textvariable=test, width=24).grid(row=0, column=0, columnspan=3, sticky="ew")

    def observe(stage, kind, wall=None):
        try:
            marker(path, test.get().strip(), stage, kind, wall)
            message.set(f"已记录 {test.get()} {stage} {kind}")
        except (ValueError, OSError) as exc:
            message.set(str(exc))

    buttons = (
        ("T0 发送成功", "T0", "outbound", 0x78),
        ("T21 状态提示", "T21", "status", 0x79),
        ("T21 实质回答", "T21", "answer", 0x7A),
    )
    ttk.Label(root, textvariable=message).grid(row=2, column=0, columnspan=3)
    for column, (label, stage, kind, _) in enumerate(buttons):
        ttk.Button(root, text=label, command=lambda s=stage, k=kind: observe(s, k)).grid(row=1, column=column)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        from queue import Empty, SimpleQueue
        from threading import Thread

        user32 = ctypes.WinDLL("user32")
        pending = SimpleQueue()
        worker_id = []

        def receive_keys():
            worker_id.append(ctypes.WinDLL("kernel32").GetCurrentThreadId())
            registered = {}
            for ident, (_, stage, kind, key) in enumerate(buttons, 1):
                if user32.RegisterHotKey(None, ident, 0x4003, key):
                    registered[ident] = stage, kind
            pending.put(("notice", len(registered)))
            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    if msg.message == 0x0312 and msg.wParam in registered:
                        pending.put(("marker", (*registered[msg.wParam], clock()[0])))
            finally:
                for ident in registered:
                    user32.UnregisterHotKey(None, ident)

        def poll():
            try:
                while True:
                    kind, value = pending.get_nowait()
                    if kind == "marker":
                        observe(*value)
                    else:
                        message.set("全局快捷键为 Ctrl+Alt+F9 / F10 / F11" if value == 3 else
                                    "部分全局快捷键不可用，可使用窗口按钮。")
            except Empty:
                pass
            root.after(15, poll)

        worker = Thread(target=receive_keys, daemon=True)
        worker.start()
        root.after(15, poll)
        try:
            root.mainloop()
        finally:
            if worker_id:
                user32.PostThreadMessageW(worker_id[0], 0x0012, 0, 0)
    else:
        root.mainloop()


def show(server, client, test):
    entries = [row for row in records(client) if row["test_id"] == test]
    refs = {row["trace_id"] for row in entries if "trace_id" in row}
    if len(refs) != 1:
        raise ValueError("Link the test ID to exactly one trace first.")
    trace = refs.pop()
    all_rows = [row for row in records(server) if row["trace_id"] == trace]
    if not all_rows:
        raise ValueError("Linked trace is missing from the server log.")
    stages = defaultdict(list)
    for row in all_rows:
        if row["event"] != "summary":
            stages[row["stage"]].append(row)
    for row in entries:
        if "stage" in row:
            stages[row["stage"]].append(row)
    summary = next((row for row in reversed(all_rows) if row["event"] == "summary"), {})
    for stage in STAGES:
        status = summary.get("stages", {}).get(stage, "unobserved")
        print(json.dumps({"test_id": test, "trace_id": trace, "stage": stage,
                          "status": "observed" if stages[stage] else status,
                          "records": stages[stage]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, default=Path("latency-client.jsonl"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("window")
    annotate = sub.add_parser("mark")
    annotate.add_argument("test")
    annotate.add_argument("kind", choices=("outbound", "status", "answer"))
    for action in ("list", "show", "link"):
        entry = sub.add_parser(action)
        if action in {"list", "show"}:
            entry.add_argument("server", type=Path)
        if action in {"show", "link"}:
            entry.add_argument("test")
        if action == "link":
            entry.add_argument("trace")
    args = parser.parse_args()
    if args.action == "window":
        try:
            window(args.client)
        except ModuleNotFoundError as exc:
            if exc.name not in {"tkinter", "_tkinter"}:
                raise
            parser.error("Use a desktop Python with Tk for window mode, or use mark from the terminal.")
    elif args.action == "mark":
        marker(args.client, args.test, "T0" if args.kind == "outbound" else "T21", args.kind)
    elif args.action == "list":
        for row in records(args.server):
            if row["event"] == "summary":
                print(row["utc"], row["trace_id"], row["chat_type"], row["message_type"], row["status"])
    elif args.action == "link":
        if not re.fullmatch(r"[a-f0-9]{32}", args.trace):
            parser.error("Expected a 32-character trace ID from list.")
        append(args.client, {"test_id": identifier(args.test), "trace_id": args.trace})
    else:
        show(args.server, args.client, identifier(args.test))


if __name__ == "__main__":
    main()

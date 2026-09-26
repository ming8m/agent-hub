#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Comm Bus — 对话查看窗口

图形界面查看 log/chat.log 里的全部通信记录（任务 / 回复 / 消息）。
- 自动刷新：终端里跑 bus.py，窗口里实时看到新对话
- 过滤：按 agent、按类型（任务/回复/消息）
- 双击任意条目弹出全文（回复类还会带出 responses/ 里的完整输出）

用法:
  python viewer.py        # 直接启动
  python bus.py view      # 从总线启动（Windows 下脱离终端，关终端窗口不死）
"""
import json, os, sys, time
import tkinter as tk
from tkinter import ttk, messagebox

import bus

LOG_FILE = bus.LOG_FILE
AGENTS_FILE = bus.AGENTS_FILE

POLL_MS = 1500        # 自动刷新轮询间隔（毫秒）
TRUNC = 400           # 主界面单条内容截断长度

TYPE_NAME = {"task": "任务", "response": "回复", "message": "消息"}
HEAD_FONT = ("Microsoft YaHei UI", 10, "bold")
BODY_FONT = ("Microsoft YaHei UI", 10)


def load_entries():
    """读 chat.log 全部 JSONL 条目，损坏行跳过。"""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        raw = open(LOG_FILE, encoding="utf-8").read()
    except OSError:
        return []
    out = []
    for ln in raw.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") in TYPE_NAME:
            out.append(e)
    return out


def content_of(e):
    return e.get("task", "") if e["type"] == "task" else e.get("content", "")


class Viewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Agent Comm Bus — 对话查看")
        self.geometry("920x640")
        self.minsize(640, 420)
        self.entries = []
        self._stamp = object()
        self._build_ui()
        self.reload()
        self.after(POLL_MS, self.poll)

    # ---------- 界面 ----------
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Agent:").pack(side="left")
        self.agent_var = tk.StringVar(value="全部")
        self.agent_box = ttk.Combobox(bar, textvariable=self.agent_var,
                                      state="readonly", width=12)
        self.agent_box.pack(side="left", padx=(2, 12))
        self.agent_box.bind("<<ComboboxSelected>>", lambda e: self.render())

        ttk.Label(bar, text="类型:").pack(side="left")
        self.type_var = tk.StringVar(value="全部")
        self.type_box = ttk.Combobox(bar, textvariable=self.type_var,
                                     state="readonly", width=8,
                                     values=["全部", "任务", "回复", "消息"])
        self.type_box.pack(side="left", padx=(2, 12))
        self.type_box.bind("<<ComboboxSelected>>", lambda e: self.render())

        self.scroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚到底部", variable=self.scroll_var)\
            .pack(side="left", padx=(0, 12))

        self.auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动刷新", variable=self.auto_var,
                        command=self.reload).pack(side="left", padx=(0, 12))

        ttk.Button(bar, text="刷新 (F5)", command=self.reload)\
            .pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="清空日志", command=self.clear_log)\
            .pack(side="right")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.text = tk.Text(body, wrap="word", bd=0, highlightthickness=0,
                            font=BODY_FONT, background="#ffffff",
                            foreground="#333333", padx=10, pady=8,
                            state="disabled", cursor="arrow")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        t = self.text
        t.tag_configure("hdr_task", foreground="#1a5fb4", font=HEAD_FONT)
        t.tag_configure("hdr_ok", foreground="#1a7f37", font=HEAD_FONT)
        t.tag_configure("hdr_fail", foreground="#c01c28", font=HEAD_FONT)
        t.tag_configure("hdr_msg", foreground="#813d9c", font=HEAD_FONT)
        t.tag_configure("body", foreground="#333333", lmargin1=14, lmargin2=14)
        t.tag_configure("sep", foreground="#dddddd")
        t.tag_configure("hint", foreground="#999999")

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, padding=(10, 4),
                  relief="sunken", anchor="w").pack(fill="x", side="bottom")
        self.bind("<F5>", lambda e: self.reload())
        self.bind("<Escape>", lambda e: self.destroy())

    # ---------- 数据 ----------
    def reload(self):
        self.entries = load_entries()
        self._stamp = self._file_stamp()
        self._refresh_agent_filter()
        self.render()

    @staticmethod
    def _file_stamp():
        try:
            st = os.stat(LOG_FILE)
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def poll(self):
        # 唯一的定时器入口，只在这里续订，避免重复调度
        if self.auto_var.get() and self._file_stamp() != self._stamp:
            self.reload()
        self.after(POLL_MS, self.poll)

    def _refresh_agent_filter(self):
        names = set()
        try:
            with open(AGENTS_FILE, encoding="utf-8") as f:
                names |= set(json.load(f).keys())
        except Exception:
            pass
        for e in self.entries:
            names.add(e.get("from", ""))
            names.add(e.get("to", ""))
        names.discard("")
        names.discard("user")
        vals = ["全部"] + sorted(names)
        if list(self.agent_box["values"]) != vals:
            cur = self.agent_var.get()
            self.agent_box["values"] = vals
            self.agent_var.set(cur if cur in vals else "全部")

    def filtered(self):
        a, t = self.agent_var.get(), self.type_var.get()
        out = []
        for e in self.entries:
            if a != "全部" and a not in (e.get("from"), e.get("to")):
                continue
            if t != "全部" and TYPE_NAME[e["type"]] != t:
                continue
            out.append(e)
        return out

    # ---------- 渲染 ----------
    def render(self):
        txt = self.text
        yfirst = txt.yview()[0]
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        entries = self.filtered()
        if not entries:
            txt.insert("end", "（暂无日志）\n", "hint")
            if self.entries:
                txt.insert("end", "当前过滤条件下没有匹配条目\n", "hint")
        for i, e in enumerate(entries):
            self._insert_entry(txt, i, e)
        txt.configure(state="disabled")
        if self.scroll_var.get():
            txt.yview_moveto(1.0)
        else:
            txt.yview_moveto(yfirst)
        self.status.set(f"共 {len(self.entries)} 条，当前显示 {len(entries)} 条 · "
                        f"最后更新 {time.strftime('%H:%M:%S')} · 双击条目看全文")

    def _insert_entry(self, txt, i, e):
        etype = e["type"]
        name = TYPE_NAME[etype]
        head_tag = {"task": "hdr_task", "message": "hdr_msg"}.get(etype)
        if etype == "response":
            ok = bool(e.get("ok"))
            head_tag = "hdr_ok" if ok else "hdr_fail"
            name += " OK" if ok else " FAIL"
        tag = f"e{i}"
        txt.insert("end",
                   f"[{e.get('time', '')}] {e.get('from', '?')} → "
                   f"{e.get('to', '?')} · {name}\n", (head_tag, tag))
        c = content_of(e).strip()
        if len(c) > TRUNC:
            c = c[:TRUNC] + " ……（双击查看全文）"
        txt.insert("end", (c or "（空）") + "\n", ("body", tag))
        if etype == "response" and e.get("full"):
            txt.insert("end", f"全文文件: {e['full']}\n", ("hint", tag))
        txt.insert("end", "─" * 72 + "\n\n", "sep")
        txt.tag_bind(tag, "<Double-Button-1>",
                     lambda ev, ent=e: self.show_detail(ent))
        txt.tag_bind(tag, "<Enter>", lambda ev: txt.configure(cursor="hand2"))
        txt.tag_bind(tag, "<Leave>", lambda ev: txt.configure(cursor="arrow"))

    def show_detail(self, e):
        w = tk.Toplevel(self)
        w.title(f"条目详情 · {e.get('id', '')}")
        w.geometry("720x520")
        w.transient(self)
        t = tk.Text(w, wrap="word", font=BODY_FONT, padx=12, pady=10)
        sb = ttk.Scrollbar(w, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.insert("end", f"时间: {e.get('time', '')}\n")
        t.insert("end", f"类型: {TYPE_NAME[e['type']]}\n")
        t.insert("end", f"方向: {e.get('from', '?')} → {e.get('to', '?')}\n")
        if e["type"] == "response":
            t.insert("end", f"状态: {'OK' if e.get('ok') else 'FAIL'}\n")
        t.insert("end", "\n──── 内容 ────\n")
        t.insert("end", content_of(e))
        if e["type"] == "response" and e.get("full"):
            p = os.path.join(bus.DATA_DIR, e["full"])
            t.insert("end", f"\n\n──── 全文文件 {e['full']} ────\n")
            try:
                t.insert("end", open(p, encoding="utf-8").read())
            except OSError:
                t.insert("end", f"（读取失败: {p}）")
        t.configure(state="disabled")

    # ---------- 操作 ----------
    def clear_log(self):
        if not messagebox.askyesno(
                "清空日志",
                "确定清空 log/chat.log 吗？\n（inbox 和 responses 全文不会被删除）"):
            return
        try:
            if os.path.exists(LOG_FILE):
                os.remove(LOG_FILE)
        except OSError as ex:
            messagebox.showerror("清空日志", f"失败: {ex}")
            return
        self.reload()


def main():
    if sys.platform == "win32":
        try:  # 高 DPI 下文字清晰
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Viewer().mainloop()


if __name__ == "__main__":
    main()

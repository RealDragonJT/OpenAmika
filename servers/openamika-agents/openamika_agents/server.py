"""The MCP tools every agent bridge exposes: ask / status / answer / stop, plus health (is the agent up?)."""
from __future__ import annotations

import json
import time

from mcp.server.mcpserver import MCPServer as FastMCP

from .jobs import JobStore


def _dump(doc) -> str:
    return json.dumps(doc, ensure_ascii=False)


def build_server(adapter, store: JobStore | None = None, settle_s: float = 1.5) -> FastMCP:
    store = store or JobStore()
    server = FastMCP(f"openamika-agents-{adapter.name}")
    label = adapter.name

    @server.tool()
    def ask(task: str, context: str = "") -> str:
        """把一件事交给 {label} 去办（读邮件、写邮件草稿、查资料、看电脑屏幕、网上下单等）。
        task：用一两句话把要办的事说清楚（对方听得懂的自然语言）；context：有用的背景（可空）。
        立刻返回一个 job 编号，事情在后台跑；之后用 status 看进度和结果。"""
        job = store.start(adapter, task.strip(), (context or "").strip())
        deadline = time.time() + settle_s
        while time.time() < deadline and not job.terminal and job.pending is None:
            time.sleep(0.1)
        return _dump(job.snapshot())

    @server.tool()
    def status(job: str = "", cursor: int = 0) -> str:
        """看某个 job 的进度：state（running / waiting_approval / waiting_question / done / failed / stopped）、
        step（最新一步）、events（cursor 之后的新事件）、pending（它在等用户回答/批准的事）、result（完成后的报告）。
        job 留空则列出所有 job 的简况。"""
        if not job:
            return _dump({"jobs": [j.brief() for j in store.all()]})
        found = store.get(job)
        if found is None:
            return _dump({"error": f"unknown job {job!r}"})
        return _dump(found.snapshot(int(cursor or 0)))

    @server.tool()
    def answer(job: str, text: str = "", approval_id: str = "", approved: bool = True) -> str:
        """回应 {label}：它在等批准时用 approved=true/false（只在用户明确同意/拒绝后才能调用）；
        它在等信息时用 text 回答；job 已结束时 text 作为追问/后续指示继续同一段对话。"""
        found = store.get(job)
        if found is None:
            return _dump({"error": f"unknown job {job!r}"})
        try:
            outcome = adapter.answer(found, text=text or "", approval_id=approval_id or "", approved=bool(approved))
        except Exception as error:
            return _dump({"error": f"{type(error).__name__}: {str(error)[:300]}", **found.snapshot()})
        return _dump({"outcome": outcome, **found.snapshot()})

    @server.tool()
    def stop(job: str) -> str:
        """叫停一个还在跑的 job。"""
        found = store.get(job)
        if found is None:
            return _dump({"error": f"unknown job {job!r}"})
        try:
            outcome = adapter.stop(found)
        except Exception as error:
            return _dump({"error": f"{type(error).__name__}: {str(error)[:300]}", **found.snapshot()})
        return _dump({"outcome": outcome, **found.snapshot()})

    @server.tool()
    def health() -> str:
        """看 {label} 本身在不在线：它的程序开没开、钥匙对不对（ok / reason / detail）。
        不会开任何任务；系统定期自动调用，不必主动用。"""
        started = time.time()
        try:
            doc = adapter.health()
        except Exception as error:
            doc = {"ok": False, "reason": "error", "detail": f"{type(error).__name__}: {str(error)[:200]}"}
        doc = dict(doc or {})
        doc.setdefault("ok", False)
        doc.setdefault("reason", "ok" if doc["ok"] else "error")
        doc["agent"] = adapter.name
        doc["ms"] = int((time.time() - started) * 1000)
        return _dump(doc)

    # FastMCP takes the docstring as the description; fill in the agent's name.
    for tool in (ask, status, answer, stop, health):
        try:
            registered = server._tool_manager.get_tool(tool.__name__)
            registered.description = (registered.description or "").replace("{label}", label)
        except Exception:
            pass
    server._openamika_store = store  # for tests
    return server

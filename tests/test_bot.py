"""TRACE Bot agent loop. FIXTURES: the language model and the graph tools are fakes (no network); what is verified is the
loop's own rules: only ids a tool returned may be cited, follow-ups are parsed, errors reach the model as data."""
from __future__ import annotations

import json
import sys
import types

from app.bot import agent


class _Call:
    def __init__(self, name, args, id="c1"):
        self.id, self.function = id, types.SimpleNamespace(name=name, arguments=json.dumps(args))


def _msg(content=None, calls=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=calls))])


def _fake_openai(replies):
    it = iter(replies)

    class Client:
        def __init__(self, **_):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=lambda **kw: next(it)))

    return types.SimpleNamespace(OpenAI=Client)


def test_only_ids_a_tool_returned_can_be_cited(monkeypatch):
    class Tools:
        seen: dict = {}

        def __init__(self, *_a):
            Tools.seen = {"evd_" + "ab" * 8: "checkout failed"}

    monkeypatch.setattr(agent, "GraphTools", Tools)
    monkeypatch.setitem(agent.DISPATCH, "error_summary", lambda t, **kw: {"groups": [{"sample_ids": ["evd_" + "ab" * 8]}]})
    good, bad = "evd_" + "ab" * 8, "evd_" + "cd" * 8
    answer = f"One error group [{good}]. Invented claim [{bad}].\nFOLLOWUPS: What changed? | Who is affected"
    monkeypatch.setitem(sys.modules, "openai", _fake_openai([_msg(calls=[_Call("error_summary", {})]), _msg(answer)]))
    out = agent.chat(None, "db", project_ids=["p"], scope_label="t", message="what failed?", history=[], api_key="k", model="m")
    assert [c["id"] for c in out["citations"]] == [good]
    assert out["rejected_citations"] == [bad]
    assert bad not in out["answer"] and "FOLLOWUPS" not in out["answer"]
    assert out["followups"] == ["What changed?", "Who is affected?"]
    assert out["queries"][0]["tool"] == "error_summary"


def test_tool_failure_is_returned_to_the_model_not_raised(monkeypatch):
    class Tools:
        seen: dict = {}

        def __init__(self, *_a):
            pass

    monkeypatch.setattr(agent, "GraphTools", Tools)

    def boom(t, **kw):
        raise RuntimeError("down")

    monkeypatch.setitem(agent.DISPATCH, "search", boom)
    monkeypatch.setitem(sys.modules, "openai", _fake_openai([_msg(calls=[_Call("search", {"query": "x"})]), _msg("I could not query the graph.")]))
    out = agent.chat(None, "db", project_ids=["p"], scope_label="t", message="find x", history=[], api_key="k", model="m")
    assert out["queries"][0]["error"] == "graph query failed: RuntimeError"
    assert out["citations"] == []

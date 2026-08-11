"""Answer-path tests with a scripted fake OpenAI client — no network, no key."""
from src import answer


# --- fakes mirroring the openai chat.completions response shape --------------

class FakeFn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id, self.function = id, FakeFn(name, arguments)


class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class FakeCompletions:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = self.script.pop(0)
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


class FakeClient:
    def __init__(self, script):
        self.chat = type("Chat", (), {"completions": FakeCompletions(script)})()


# --- fakes for the Responses API (agent path) -------------------------------

class FakeFnCall:
    type = "function_call"

    def __init__(self, name, arguments, call_id="fc1"):
        self.name, self.arguments, self.call_id = name, arguments, call_id


class FakeResp:
    def __init__(self, output=None, output_text="", id="resp1"):
        self.output, self.output_text, self.id = output or [], output_text, id


class FakeResponsesAPI:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


class FakeRespClient:
    def __init__(self, script):
        self.responses = FakeResponsesAPI(script)


class FakeStore:
    def hybrid_query(self, query, k, where=None):
        return [{"id": "x", "title": "cnr_maps error", "source": "issues",
                 "url": "https://github.com/PennLINC/qsiprep/issues/42",
                 "gh_solved": True, "text": "add cnr_maps: true to the eddy config"}]


CHUNKS = [
    {"title": "Eddy config", "source": "docs", "url": "u1", "text": "set cnr_maps"},
    {"title": "OOM thread", "source": "neurostars", "ns_solved": True,
     "url": "u2", "text": "increase memory"},
]


# --- one-shot ----------------------------------------------------------------

def test_answer_oneshot_builds_prompt_and_returns(config):
    client = FakeClient([FakeMsg(content="Set cnr_maps: true [1].")])
    out = answer.answer_oneshot("how to fix eddy config?", CHUNKS, "qsiprep",
                                config, client=client)
    assert out == "Set cnr_maps: true [1]."
    sent = client.chat.completions.calls[0]
    assert sent["model"] == config["llm"]["oneshot_model"]
    user = sent["messages"][1]["content"]
    assert "[1] Eddy config" in user and "[2] OOM thread" in user
    assert "set cnr_maps" in user                       # chunk text included
    assert "tools" not in sent                          # one-shot never offers tools


def test_version_hint():
    assert "26.0.0" in answer._version_hint("crashes on qsiprep 26.0.0")
    assert "did not state a version" in answer._version_hint("why does eddy fail?")


def test_notes_injected_into_system_prompt(config):
    # config fixture gives qsiprep a note about reconstruction being qsirecon's
    client = FakeClient([FakeMsg(content="ok")])
    answer.answer_oneshot("can qsiprep do reconstruction?", CHUNKS, "qsiprep",
                          config, client=client)
    system = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "reconstruction is qsirecon's job" in system
    assert "(qsiprep)" in system            # note is attributed to its app


def test_notes_block_empty_when_no_notes():
    cfg = {"apps": {"cubids": {"neighbors": []}}}
    assert answer._notes_block(cfg, "cubids") == ""


# --- agent loop --------------------------------------------------------------

def test_answer_agent_runs_tool_then_answers(config):
    script = [
        FakeResp(output=[FakeFnCall("search_kb", '{"query": "cnr_maps eddy"}')]),
        FakeResp(output_text="Add cnr_maps: true — see "
                             "https://github.com/PennLINC/qsiprep/issues/42"),
    ]
    client = FakeRespClient(script)
    result = answer.answer_agent("eddy config error?", "qsiprep", config,
                                 FakeStore(), client=client)
    assert "cnr_maps" in result.answer
    assert result.iterations == 2
    assert [s["tool"] for s in result.transcript] == ["search_kb"]
    assert "issues/42" in result.transcript[0]["result"]      # tool actually ran
    assert "tools" in client.responses.calls[0]               # tools offered
    # second turn threads server state instead of resending history
    assert client.responses.calls[1]["previous_response_id"] == "resp1"
    assert client.responses.calls[1]["input"][0]["type"] == "function_call_output"


def test_answer_agent_hits_cap_then_forces_wrapup(config):
    # config fixture caps at 4 iterations; every turn asks for a tool, so the
    # loop exhausts and a final tool-less call produces the wrap-up.
    def tool_turn(i):
        return FakeResp(output=[FakeFnCall("grep_code",
                        '{"pattern": "x", "version": "26.0.0"}', call_id=f"c{i}")],
                        id=f"r{i}")
    script = [tool_turn(i) for i in range(config["llm"]["max_tool_iterations"])]
    script.append(FakeResp(output_text="Here is a GitHub issue draft: ..."))
    client = FakeRespClient(script)

    # grep_code will fail (no checkout) but the loop must keep going regardless
    result = answer.answer_agent("obscure failure", "qsiprep", config,
                                 FakeStore(), client=client)
    assert result.iterations == config["llm"]["max_tool_iterations"]
    assert "issue draft" in result.answer
    assert "tools" not in client.responses.calls[-1]          # wrap-up: no tools
    assert len(result.transcript) == config["llm"]["max_tool_iterations"]


def test_answer_agent_tolerates_bad_tool_json(config):
    script = [
        FakeResp(output=[FakeFnCall("search_kb", "{not json")]),
        FakeResp(output_text="done"),
    ]
    client = FakeRespClient(script)
    result = answer.answer_agent("q", "qsiprep", config, FakeStore(), client=client)
    assert result.answer == "done"
    assert result.transcript[0]["args"] == {}          # bad json -> empty args

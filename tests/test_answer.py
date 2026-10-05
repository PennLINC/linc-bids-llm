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


# A tester gave the version in their opening message and was asked for it on
# every follow-up: the hint read the current message alone, and once the chat
# ran past three exchanges the window no longer carried the opening message.

OPENING = ("I'm running qsiprep 1.0.0rc2 via apptainer and eddy fails with "
           "--output-resolution 1.25")
FOLLOWUP = ("Ok explain how SynthStrip + SynthSeg together compare to FAST in "
            "a 5TT pipeline specifically.")


def _chat(*user_turns):
    """A history in which the assistant answered each user turn."""
    history = []
    for q in user_turns:
        history += [{"role": "user", "content": q},
                    {"role": "assistant", "content": "Answer about that."}]
    return history


def test_version_hint_finds_version_given_earlier_in_the_chat():
    hint = answer._version_hint(FOLLOWUP, history=_chat("qsiprep 1.0.0rc2 crashes"))
    assert "Earlier in this chat" in hint and "'1.0.0rc2'" in hint
    assert "do not ask for it again" in hint
    assert "did not state" not in hint


def test_version_hint_current_message_wins():
    hint = answer._version_hint("and on 1.0.0?", history=_chat("I run qsiprep 0.21.4"))
    assert hint.startswith("The user's message mentions '1.0.0'")


def test_version_hint_lists_every_earlier_candidate():
    # '1.25' is an output resolution; the hint can't know that, so it lists
    # both and leaves the model to confirm from the turn itself
    hint = answer._version_hint(FOLLOWUP, history=_chat(OPENING))
    assert "'1.0.0rc2' and '1.25'" in hint
    assert "one of which may be the version" in hint
    assert "confirm only if it is ambiguous" in hint


def test_version_hint_reads_user_turns_only():
    history = [{"role": "user", "content": "eddy crashes, why?"},
               {"role": "assistant", "content": "That was fixed in 0.22.0."}]
    assert "did not state a version" in answer._version_hint("ok, so?", history)


def test_version_hint_tolerates_history_shapes():
    # no history, an empty one, and non-text content all read as no mention
    assert "did not state" in answer._version_hint("q", None)
    assert "did not state" in answer._version_hint("q", [])
    parts = [{"role": "user", "content": [{"type": "input_text", "text": "1.0.0"}]}]
    assert "did not state" in answer._version_hint("q", parts)


def test_version_hint_caps_candidates_and_prefers_releases():
    # an environment dump is full of x.y numbers; the x.y.z release is the one
    # worth listing first, and the list stays short
    history = _chat("Ubuntu 22.04, CUDA 12.1, python 3.10, numpy 1.26, 64.5 GB "
                    "RAM, qsiprep 1.0.0")
    hint = answer._version_hint("why?", history)
    assert hint.count("'") == 2 * answer.MAX_VERSION_MENTIONS
    assert hint.index("'1.0.0'") < hint.index("'22.04'")
    assert "'64.5'" not in hint


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


def test_answer_agent_hint_reads_the_chat(config):
    client = FakeRespClient([FakeResp(output_text="SynthStrip vs FAST: ...")])
    history = _chat(OPENING)
    answer.answer_agent(FOLLOWUP, "qsiprep", config, FakeStore(),
                        history=history, client=client)
    sent = client.responses.calls[0]["input"]
    assert sent[:-1] == history                        # the chat precedes the turn
    turn = sent[-1]
    assert turn["role"] == "user" and turn["content"].startswith(FOLLOWUP)
    assert "Earlier in this chat the user mentioned '1.0.0rc2'" in turn["content"]
    assert "did not state a version" not in turn["content"]


def test_answer_agent_tolerates_bad_tool_json(config):
    script = [
        FakeResp(output=[FakeFnCall("search_kb", "{not json")]),
        FakeResp(output_text="done"),
    ]
    client = FakeRespClient(script)
    result = answer.answer_agent("q", "qsiprep", config, FakeStore(), client=client)
    assert result.answer == "done"
    assert result.transcript[0]["args"] == {}          # bad json -> empty args


# --- history window ------------------------------------------------------------
# app.py sends agent_history(prior messages) to the agent and logs the same
# with each feedback entry, so a rated follow-up replays with the turns it had.

def _messages(n):
    """A chat of n stored messages, user/assistant alternating, with the UI's
    extra keys on them; the opening message states the version."""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"{role} {i}", "route_path": "agent"})
    msgs[0]["content"] = OPENING
    return msgs


def test_agent_history_short_chat_is_sent_whole():
    assert answer.agent_history([]) == []
    out = answer.agent_history(_messages(4))
    assert [m["content"] for m in out] == [OPENING, "assistant 1", "user 2",
                                           "assistant 3"]
    assert all(set(m) == {"role", "content"} for m in out)   # UI keys stripped


def test_agent_history_keeps_opening_message_past_the_window():
    msgs = _messages(answer.HISTORY_TURNS + 4)   # two exchanges past the window
    out = answer.agent_history(msgs)
    assert len(out) == answer.HISTORY_TURNS + 1
    assert out[0] == {"role": "user", "content": OPENING}
    assert out[1:] == [{"role": m["role"], "content": m["content"]}
                       for m in msgs[-answer.HISTORY_TURNS:]]


def test_agent_history_does_not_duplicate_an_opening_still_in_the_window():
    out = answer.agent_history(_messages(answer.HISTORY_TURNS))
    assert len(out) == answer.HISTORY_TURNS
    assert [m["content"] for m in out].count(OPENING) == 1


def test_version_survives_a_long_chat():
    # the tester's chat: version in the opening message, a follow-up several
    # exchanges past the window. The window keeps the opening turn and the
    # hint reads it.
    history = answer.agent_history(_messages(answer.HISTORY_TURNS + 2))
    hint = answer._version_hint(FOLLOWUP, history)
    assert "'1.0.0rc2'" in hint and "do not ask for it again" in hint

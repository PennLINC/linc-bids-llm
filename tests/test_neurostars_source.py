from src.sources import neurostars_source as ns


def test_html_to_text_preserves_code_blocks():
    cooked = ('<p>I get   this\nerror:</p>'
              '<pre><code class="lang-python">Traceback (most recent call last):\n'
              '  File "x.py", line 1\nValueError:   bad   value</code></pre>'
              '<p>Any ideas? Try <code>--force-syn</code> maybe.</p>')
    text = ns.html_to_text(cooked)
    assert "I get this error:" in text                      # prose whitespace collapsed
    assert "```python" in text
    assert '  File "x.py", line 1' in text                  # code verbatim
    assert "ValueError:   bad   value" in text              # incl. inner spaces
    assert "`--force-syn`" in text                          # inline code kept


def test_html_to_text_structure():
    cooked = ('<h2>Steps</h2><ul><li>one</li><li>two</li></ul>'
              '<blockquote><p>quoted advice</p></blockquote>'
              '<img src="x.png" alt="screenshot of crash"/>'
              '<img src="e.png" alt=":slight_smile:"/>')
    text = ns.html_to_text(cooked)
    assert "## Steps" in text
    assert "- one" in text and "- two" in text
    assert "> " in text and "quoted advice" in text
    assert "[image: screenshot of crash]" in text
    assert "slight_smile" not in text                       # emoji imgs dropped


def make_post(number, username, cooked, accepted=False, post_type=1):
    return {"id": 100 + number, "post_number": number, "username": username,
            "created_at": "2024-03-01T00:00:00Z", "cooked": f"<p>{cooked}</p>",
            "accepted_answer": accepted, "post_type": post_type}


def test_thread_text_accepted_answer_second():
    posts = [
        make_post(1, "asker", "My qsiprep run crashes with this long error."),
        make_post(2, "rando", "Same problem here, following this thread."),
        make_post(3, "expert", "This is fixed by increasing memory to 16GB.",
                  accepted=True),
        make_post(4, "asker", "thanks!"),  # below MIN_REPLY_CHARS -> dropped
    ]
    text = ns.thread_text("Crash during preprocessing", posts)
    assert text.startswith("# Crash during preprocessing")
    assert "asker asked on 2024-03-01" in text
    accepted_pos = text.index("Accepted answer — expert")
    rando_pos = text.index("rando replied")
    assert accepted_pos < rando_pos                         # accepted promoted
    assert "thanks!" not in text


def test_topic_record_fields():
    topic = {"id": 12345, "title": "Crash during preprocessing",
             "slug": "crash-during-preprocessing", "has_accepted_answer": True,
             "created_at": "2024-03-01T00:00:00Z",
             "bumped_at": "2024-03-05T00:00:00Z", "posts_count": 4, "views": 200}
    rec = ns.topic_record("qsiprep", topic, [make_post(1, "asker", "body text")])
    assert rec["url"] == "https://neurostars.org/t/crash-during-preprocessing/12345"
    assert rec["ns_solved"] is True
    assert rec["ns_replies"] == 3
    assert rec["app"] == "qsiprep"
    assert rec["source"] == "neurostars"


def test_fetch_posts_drains_stream_and_drops_mod_actions(monkeypatch):
    first_page = {"post_stream": {
        "posts": [make_post(1, "asker", "q"),
                  make_post(2, "mod", "closed", post_type=3)],
        "stream": [101, 102, 103]}}
    drained = {"post_stream": {"posts": [make_post(3, "late", "late reply")]}}
    calls = []

    def fake_get(path, **params):
        calls.append((path, params))
        return drained if "posts.json" in path else first_page

    monkeypatch.setattr(ns, "_get", fake_get)
    posts = ns.fetch_posts(999)
    assert [p["post_number"] for p in posts] == [1, 3]      # mod action dropped
    assert calls[1][1] == {"post_ids[]": [103]}             # only missing id fetched

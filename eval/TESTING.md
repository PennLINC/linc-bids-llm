# Battle-testing protocol (maintainers)

Goal: turn "poke at it and see" into a comparable, methodical evaluation that
feeds the regression eval. Budget ~30–60 min. Work the scenario matrix, rate
every answer in the app's feedback panel, then submit.

## Setup

Follow "For maintainer-testers" in the [README](../README.md): prebuilt index,
`python -m src.checkouts`, `streamlit run app.py`. Set your `git config
user.name` so your submitted feedback is attributed.

## Scenario matrix — cover every row

Ask at least one real question of each type (use actual cases you know the
answer to — that's what makes your rating trustworthy):

| # | Archetype | What it exercises |
|---|-----------|-------------------|
| 1 | Plain FAQ ("how do I set X?") | one-shot path, docs/thread citation |
| 2 | Pasted traceback | agent path, grep → raising code → permalink |
| 3 | Version-specific ("on 1.1.1, …") | version resolution, tag-pinned permalinks |
| 4 | Cross-version regression ("worked in X, broke in Y") | version awareness, "fixed in vZ" |
| 5 | Error string you know is in a closed issue | retrieval surfaces the known thread |
| 6 | Genuinely unanswerable / novel | refuse-to-guess, escalation to issue draft |
| 7 | A follow-up in the same chat | multi-turn context |

## Rating rubric — judge each answer on

- **Correct?** Does it reach the right resolution / actionable guidance?
- **Sources valid?** Do the links open, and are they actually relevant?
- **Version-aware?** Right tag pinned; notes when a fix landed elsewhere?
- **No hallucination?** No invented flags, APIs, or error messages?
- **Escalated right?** When it couldn't confirm a cause, did it say so (not guess)?

In the app's "Rate this answer" panel: thumbs, a **problem category** if it
wasn't good, and — most valuable — the **correct source URL** when you know it
(that turns your case into a retrieval regression test).

## Submit

- **Testing on the hosted app?** Nothing to do — everyone shares one instance,
  so your ratings already land in `.feedback/feedback.jsonl` **on the server**.
  The lead pulls that file and runs the report (below).
- **Running your own local copy?** Push your feedback as a PR:
  ```bash
  scripts/submit_feedback.sh      # opens a PR adding eval/feedback/<you>.jsonl
  ```

## Triage + improve (lead)

Gather the feedback first: copy the server's log to `.feedback/server.jsonl`
(gitignored, and separate from your own `.feedback/feedback.jsonl`), alongside
any PR'd `eval/feedback/*.jsonl`. `scp` needs port 22, which the Penn VPN
refuses — do it off-VPN:

```bash
scp -i ~/.ssh/<lightsail-key>.pem \
  ubuntu@<server-ip>:linc-bids-llm/.feedback/feedback.jsonl .feedback/server.jsonl
```

Then, against a current index (`scripts/fetch_index.sh`, so local retrieval
matches what the server served):

```bash
python -m eval.feedback_report --log .feedback/server.jsonl    # up/down by path, by model, categories, failures
python -m eval.feedback_to_cases --log .feedback/server.jsonl  # failures -> eval/regression.json
python -m eval.run_eval --heldout eval/regression.json         # retrieval: did the correct URL make the top-k?
python -m eval.run_eval --heldout eval/regression.json --answers 10   # + judged answers (costs API)
```

(`--local` adds your own log instead of / as well as `--log`.) The report
keeps one entry per rated answer (a re-click supersedes the earlier one) and
counts a problem category with no thumb as a thumbs-down. The retrieval step
lists every miss with its cause — the correct URL isn't indexed at all, it's
filed under an app outside the card's scope, or it's indexed but ranked too
low; only the last is a ranking problem. The judged step calls the OpenAI API,
which the Penn Medicine web filter intercepts: run it off-VPN or on the box.

`eval/regression.json` holds testers' questions and the flagged answers
verbatim — skim it before committing it to this public repo.

Fix prompts/retrieval/config, then re-run the `run_eval` commands: the
regression set is now the gate — a fix should pass its case without regressing
the others.

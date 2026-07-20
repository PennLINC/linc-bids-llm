"""NeuroStars (Discourse) source: tag walk -> topic fetch -> thread Records.

Thread shape per the schema: question first, accepted answer second (marked),
remaining substantive replies in order. Posts arrive as HTML ("cooked");
converted to markdown-ish text with <pre><code> blocks preserved as fences —
they hold the tracebacks that make threads findable.

Dry run (fetches N topics from each configured tag):
    python -m src.sources.neurostars_source [N]
"""
import re
import sys
import time
from html.parser import HTMLParser

import requests

from .. import common

BASE = "https://neurostars.org"
SLEEP = 1.0  # Discourse etiquette: ~1 req/s
MAX_POST_CHARS = 6000
MIN_REPLY_CHARS = 25  # drop "thanks!" / "+1" replies


def _get(path: str, **params) -> dict | None:
    """GET a Discourse JSON endpoint; None on 404."""
    headers = {"User-Agent": common.user_agent()}
    for attempt in range(4):
        try:
            resp = requests.get(f"{BASE}{path}", headers=headers,
                                params=params or None, timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        time.sleep(SLEEP)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "30"))
            print(f"  429; honoring Retry-After: {retry}s", file=sys.stderr)
            time.sleep(retry)
            continue
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()


# --- HTML -> text ----------------------------------------------------------

class _HTML2Text(HTMLParser):
    """Discourse 'cooked' HTML -> markdown-ish text, code blocks fenced."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.pre_depth = 0       # inside <pre> (verbatim mode)
        self.pending_pre = False  # just entered <pre>; next <code> opens fence
        self.inline_code = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "pre":
            self.pre_depth += 1
            self.pending_pre = True
        elif tag == "code":
            if self.pending_pre:
                lang = ""
                for cls in (attrs.get("class") or "").split():
                    if cls.startswith("lang-") and cls != "lang-auto":
                        lang = cls[5:]
                self.out.append(f"\n```{lang}\n")
                self.pending_pre = False
            elif not self.pre_depth:
                self.inline_code = True
                self.out.append("`")
        elif self.pre_depth:
            return  # nothing else inside a code block matters
        elif tag in ("p", "div", "aside", "table", "tr"):
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "img":
            alt = attrs.get("alt", "")
            if alt and not alt.startswith(":"):  # skip emoji shortcodes
                self.out.append(f"[image: {alt}]")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")

    def handle_endtag(self, tag):
        if tag == "pre":
            self.pre_depth = max(self.pre_depth - 1, 0)
            self.pending_pre = False
        elif tag == "code":
            if self.pre_depth:
                self.out.append("\n```\n")
            elif self.inline_code:
                self.inline_code = False
                self.out.append("`")
        elif tag in ("p", "div", "ul", "ol", "table", "blockquote"):
            self.out.append("\n")

    def handle_data(self, data):
        if self.pre_depth:
            self.out.append(data)  # verbatim inside code blocks
        else:
            self.out.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        joined = "".join(self.out)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(cooked: str) -> str:
    parser = _HTML2Text()
    parser.feed(cooked or "")
    return parser.text()


# --- Harvest ---------------------------------------------------------------

def list_topics(tag: str) -> list[dict]:
    """All topic summaries for a tag (paged 'latest' listing)."""
    topics, page = [], 0
    while True:
        data = _get(f"/tag/{tag}/l/latest.json", page=page)
        if data is None:
            print(f"  tag '{tag}': nothing returned", file=sys.stderr)
            return topics
        batch = data.get("topic_list", {}).get("topics", [])
        if not batch:
            return topics
        topics.extend(batch)
        page += 1


def fetch_posts(topic_id: int) -> list[dict]:
    """Every regular post in a topic, draining the post stream past page one."""
    data = _get(f"/t/{topic_id}.json")
    if data is None:
        return []
    posts = list(data.get("post_stream", {}).get("posts", []))
    stream = data.get("post_stream", {}).get("stream", [])
    missing = [pid for pid in stream if pid not in {p["id"] for p in posts}]
    for i in range(0, len(missing), 20):
        extra = _get(f"/t/{topic_id}/posts.json",
                     **{"post_ids[]": missing[i:i + 20]})
        posts.extend((extra or {}).get("post_stream", {}).get("posts", []))
    posts = [p for p in posts if p.get("post_type") == 1]  # drop mod actions
    return sorted(posts, key=lambda p: p.get("post_number", 0))


def thread_text(title: str, posts: list[dict]) -> str:
    """Question first, accepted answer second (marked), replies condensed."""
    if not posts:
        return ""

    def clip(post) -> str:
        body = html_to_text(post.get("cooked", ""))
        if len(body) > MAX_POST_CHARS:
            body = body[:MAX_POST_CHARS] + "\n[... truncated]"
        return body

    question, replies = posts[0], posts[1:]
    accepted = [p for p in replies if p.get("accepted_answer")]
    others = [p for p in replies if not p.get("accepted_answer")]
    parts = [f"# {title}\n\n"
             f"**{question.get('username', '?')} asked on "
             f"{question.get('created_at', '')[:10]}:**\n\n{clip(question)}"]
    for p in accepted:
        parts.append(f"**Accepted answer — {p.get('username', '?')}:**\n\n{clip(p)}")
    for p in others:
        body = clip(p)
        if len(body) < MIN_REPLY_CHARS:
            continue  # "thanks!" adds nothing
        parts.append(f"**{p.get('username', '?')} replied:**\n\n{body}")
    return "\n\n---\n\n".join(parts)


def topic_record(app: str, topic: dict, posts: list[dict]) -> dict:
    """Thread Record from a tag-listing topic summary + its fetched posts."""
    return {
        "text": thread_text(topic.get("title", ""), posts),
        "app": app,
        "source": "neurostars",
        "title": topic.get("title", ""),
        "url": f"{BASE}/t/{topic.get('slug', 'topic')}/{topic['id']}",
        "ns_topic_id": topic["id"],
        "ns_solved": bool(topic.get("has_accepted_answer")),
        "ns_created": topic.get("created_at", ""),
        "ns_bumped": topic.get("bumped_at", ""),
        "ns_replies": max(topic.get("posts_count", 1) - 1, 0),
        "ns_views": topic.get("views", 0),
    }


def fetch_app(app: str, app_cfg: dict, limit: int | None = None,
              old_bumped: dict | None = None) -> list[dict]:
    """Thread Records for one app's tags.

    `old_bumped` maps topic_id -> bumped_at from the previous harvest;
    unchanged topics are skipped (ingest keeps their existing chunks).
    """
    records, seen = [], set()
    for tag in app_cfg.get("neurostars_tags", []):
        topics = list_topics(tag)
        if limit:
            topics = topics[:limit]
        for topic in topics:
            if topic["id"] in seen:
                continue
            seen.add(topic["id"])
            if (old_bumped or {}).get(str(topic["id"])) == topic.get("bumped_at"):
                continue  # unchanged since last harvest
            try:
                posts = fetch_posts(topic["id"])
                if not posts:
                    continue
                records.append(topic_record(app, topic, posts))
            except Exception as e:
                print(f"  [skip] topic {topic['id']}: {e}", file=sys.stderr)
    print(f"{app} neurostars: {len(records)} thread(s)", file=sys.stderr)
    return records


def fetch(config: dict | None = None, old_bumped: dict | None = None) -> list[dict]:
    config = config or common.load_config()
    return [r for app, app_cfg in config["apps"].items()
            for r in fetch_app(app, app_cfg, old_bumped=old_bumped)]


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    config = common.load_config()
    for app, app_cfg in config["apps"].items():
        for r in fetch_app(app, app_cfg, limit=limit):
            solved = "solved" if r["ns_solved"] else "unsolved"
            print(f"\n[{solved}] {r['title']}")
            print(f"  {r['url']}")
            print(f"  {r['ns_replies']} replies, {r['ns_views']} views, "
                  f"{common.count_tokens(r['text'])} tokens")
            print(f"  head: {r['text'][:150].replace(chr(10), ' ')}...")


if __name__ == "__main__":
    main()

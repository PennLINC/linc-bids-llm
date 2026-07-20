"""Hybrid store: Chroma (dense vectors) + SQLite FTS5 (BM25 keyword) written
together at ingest, queried together and fused by reciprocal rank.

Why hybrid: error strings and identifiers are exact-match creatures that dense
embeddings blur ("RuntimeError: CUDA out of memory" vs. a paraphrase). BM25
catches the literal token; vectors catch the paraphrase; RRF merges them.

Keep the interface small (add / delete / hybrid_query / reset) so swapping a
backing store stays a one-file change. Embeddings are computed here with the
local model pinned in config — Chroma's built-in embedder is never used.
"""
import json
import re
import sqlite3
from pathlib import Path

from . import common

RRF_K = 60          # reciprocal-rank-fusion damping; 60 is the standard default
MAX_MATCH_TOKENS = 80  # cap FTS query size so a giant traceback stays cheap

# Metadata columns the FTS side can scope on. Any hybrid_query `where` is
# narrowed to this subset for BM25 (the vector side takes the full filter).
FTS_SCOPE_COLS = ("app", "source")


def _fts_match_query(text: str) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH string.

    Each token is quoted (so FTS operators, quotes and error-string punctuation
    can't break the parse) and OR-joined. BM25's IDF then rewards the rare
    tokens — exactly the identifiers and error codes we want to match on.
    """
    tokens = re.findall(r"\w+", text.lower())
    tokens = [t for t in tokens if len(t) > 1][:MAX_MATCH_TOKENS]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def _chroma_where(where: dict | None) -> dict | None:
    """Chroma needs multi-key filters wrapped in $and; single key passes through."""
    if not where:
        return None
    if len(where) == 1:
        return dict(where)
    return {"$and": [{k: v} for k, v in where.items()]}


class Store:
    def __init__(self, config: dict | None = None):
        import chromadb  # heavy; import lazily
        from chromadb.config import Settings

        self.config = config or common.load_config()
        index_path = Path(self.config["index"]["path"])
        index_path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(index_path),
            settings=Settings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(
            "chunks", metadata={"hnsw:space": "cosine"})

        self.fts_path = index_path / "fts.sqlite"
        # check_same_thread=False: Streamlit serves reruns on different threads,
        # and the query path is read-only, so sharing the connection is safe.
        # Writes (add/delete) only happen in the single-threaded ingest process.
        self.db = sqlite3.connect(self.fts_path, check_same_thread=False)
        self._init_fts()

    def _init_fts(self) -> None:
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5("
            "id UNINDEXED, text, app UNINDEXED, source UNINDEXED, "
            "tokenize='porter unicode61')")
        self.db.commit()

    # --- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        try:
            self.client.delete_collection("chunks")
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            "chunks", metadata={"hnsw:space": "cosine"})
        self.db.execute("DELETE FROM fts")
        self.db.commit()

    def count(self) -> int:
        return self.collection.count()

    def close(self) -> None:
        self.db.close()

    # --- writes ------------------------------------------------------------

    def add(self, records: list[dict], batch_size: int = 128) -> None:
        """Embed each record locally and store it in BOTH indexes together."""
        if not records:
            return
        model = common.get_embedding_model()
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            embeddings = model.encode([r["text"] for r in batch],
                                      normalize_embeddings=True)
            self.collection.add(
                ids=[r["id"] for r in batch],
                documents=[r["text"] for r in batch],
                embeddings=embeddings.tolist(),
                metadatas=[{k: v for k, v in r.items()
                            if k not in ("id", "text")} for r in batch],
            )
            self.db.executemany(
                "INSERT INTO fts (id, text, app, source) VALUES (?, ?, ?, ?)",
                [(r["id"], r["text"], r.get("app", ""), r.get("source", ""))
                 for r in batch])
        self.db.commit()

    def delete(self, where: dict) -> None:
        """Remove every chunk matching a metadata filter from both indexes.

        Ids are resolved from Chroma first so the FTS mirror can be pruned by
        the same set — keeps the FTS schema minimal (it need not carry every
        filterable field, only the scope columns).
        """
        got = self.collection.get(where=_chroma_where(where))
        ids = got["ids"]
        self.collection.delete(where=_chroma_where(where))
        if ids:
            self.db.executemany("DELETE FROM fts WHERE id = ?",
                                [(i,) for i in ids])
            self.db.commit()

    # --- reads -------------------------------------------------------------

    def _vector_ids(self, text: str, k: int, where: dict | None) -> list[str]:
        model = common.get_embedding_model()
        embedding = model.encode([common.BGE_QUERY_PREFIX + text],
                                 normalize_embeddings=True)
        result = self.collection.query(
            query_embeddings=embedding.tolist(), n_results=k,
            where=_chroma_where(where))
        return list(result["ids"][0]) if result["ids"] else []

    def _bm25_ids(self, text: str, k: int, where: dict | None) -> list[str]:
        match = _fts_match_query(text)
        if match is None:
            return []
        scope = {c: where[c] for c in FTS_SCOPE_COLS if where and c in where}
        sql = "SELECT id FROM fts WHERE fts MATCH ?"
        params: list = [match]
        for col, val in scope.items():
            sql += f" AND {col} = ?"
            params.append(val)
        sql += " ORDER BY rank LIMIT ?"
        params.append(k)
        return [row[0] for row in self.db.execute(sql, params).fetchall()]

    def _hydrate(self, ids: list[str]) -> dict[str, dict]:
        """Full chunk records (text + metadata) for a set of ids, from Chroma."""
        if not ids:
            return {}
        got = self.collection.get(ids=ids, include=["documents", "metadatas"])
        out = {}
        for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"]):
            out[cid] = {"id": cid, "text": doc, **(meta or {})}
        return out

    def hybrid_query(self, text: str, k: int, where: dict | None = None,
                     candidates: int | None = None) -> list[dict]:
        """Top-k chunk records, fusing dense + BM25 candidate lists by RRF.

        `where` scopes both halves (e.g. {"app": "qsiprep", "source": "issues"}).
        `candidates` is the per-side pool depth before fusion (defaults to the
        retrieval.candidates config value).
        """
        pool = candidates or self.config["retrieval"]["candidates"]
        vec = self._vector_ids(text, pool, where)
        bm25 = self._bm25_ids(text, pool, where)

        scores: dict[str, float] = {}
        for ranked in (vec, bm25):
            for rank, cid in enumerate(ranked):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:k]

        records = self._hydrate(ranked_ids)
        out = []
        for cid in ranked_ids:
            rec = records.get(cid)
            if rec is None:
                continue
            rec = dict(rec)
            rec["score"] = scores[cid]
            rec["in_vector"] = cid in vec
            rec["in_bm25"] = cid in bm25
            out.append(rec)
        return out

"""Phase 3: retrieval. Each enabled entry is embedded by the developer as several texts (texts(): its name and
behavior, then each example and each outcome) and kept in the catalog (operation_embeddings: float32 little-endian,
unit length, with the entry's whole searchable text's hash and its version); the CLI loads them into one matrix,
compares a request's vector with it (dot product = cosine) and ranks each entry by its best vector. A stale entry
(changed since) is left out and reported, never matched. Similarity ranks; it does not decide (selection, phase 4).
Measured (evaluation.py, 64 held-out requests): one vector per entry R@10 0.92, one per example 0.98 with MiniLM, the
whole request better than an extracted phrase; e5-base reaches 1.00 (a second, larger model: the upgrade if needed).

    embed(db, config)    the developer's: every enabled entry's vector for one model configuration
    Index(db, config)    the CLI's: the matrix, its ids, retrieve(text, k) → [(id, similarity)]
"""
import hashlib
import json

import numpy as np

CONFIGS = {   # an immutable id per model and encoding: a changed one is a new id, never mixed with another
    "minilm-2": {"model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "query": "",
                 "document": ""},
    "e5-base-2": {"model_name": "intfloat/multilingual-e5-base", "query": "query: ", "document": "passage: "},
}
DEFAULT = "minilm-2"                                          # the linker's model: already loaded
TEMPLATE = 2                                                  # the texts' layout (texts())
_MODELS = {}


def searchable(entry: dict) -> str:
    """The text an entry is found by, the same for the same entry (its lists in their order)."""
    return "\n".join([entry["name"] + ".", entry["behavior"], *entry["outcomes"], *entry["examples"],
                      *entry["distinctions"]])


def texts(entry: dict) -> list[str]:
    """The texts an entry is found by, one vector each: its name and behavior, then its examples and outcomes."""
    return [entry["name"] + ". " + entry["behavior"], *entry["examples"], *entry["outcomes"]]


def model(name: str):
    if name not in _MODELS:
        from sentence_transformers import SentenceTransformer
        _MODELS[name] = SentenceTransformer(name)
    return _MODELS[name]


def encode(config_id: str, texts: list[str], side: str) -> np.ndarray:
    c = CONFIGS[config_id]
    return model(c["model_name"]).encode([c[side] + t for t in texts], normalize_embeddings=True).astype("<f4")


def embed(db, config_id: str = DEFAULT) -> int:
    """Every enabled entry's vectors for `config_id`, written in one transaction (the catalog's revision goes up).
    → how many entries were (re)computed: one whose text and version did not change keeps its vectors."""
    from .catalog import entries
    c, todo = CONFIGS[config_id], []
    for e in entries(db):
        digest = hashlib.sha256(searchable(e).encode()).hexdigest()
        if db.execute("SELECT 1 FROM operation_embeddings WHERE operation_id = ? AND embedding_config_id = ? AND "
                      "content_hash = ? AND operation_version = ?", (e["id"], config_id, digest, e["version"])
                      ).fetchone() is None:
            todo.append((e, digest))
    if not todo:
        return 0
    flat = [(e, d, i, t) for e, d in todo for i, t in enumerate(texts(e))]
    vectors = encode(config_id, [t for *_, t in flat], "document")
    db.execute("BEGIN")
    try:
        db.execute("INSERT OR IGNORE INTO embedding_configs VALUES (?, ?, 'pinned', ?, 'float32_le', 1, ?, ?)",
                   (config_id, c["model_name"], vectors.shape[1], json.dumps(c), TEMPLATE))
        for e, _ in todo:
            db.execute("DELETE FROM operation_embeddings WHERE operation_id = ? AND embedding_config_id = ?",
                       (e["id"], config_id))
        db.executemany("INSERT INTO operation_embeddings (operation_id, embedding_config_id, position, "
                       "operation_version, searchable_text, content_hash, vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(e["id"], config_id, i, e["version"], t, d, v.tobytes())
                        for (e, d, i, t), v in zip(flat, vectors)])
        db.execute("UPDATE catalog_state SET revision = revision + 1")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return len(todo)


class Index:
    def __init__(self, db, config_id: str = DEFAULT):
        from .catalog import entries
        self.config_id, self.ids, rows, self.stale = config_id, [], [], []
        dims = db.execute("SELECT dimensions FROM embedding_configs WHERE id = ?", (config_id,)).fetchone()
        for e in entries(db):
            found = db.execute("SELECT vector, content_hash, operation_version FROM operation_embeddings WHERE "
                               "operation_id = ? AND embedding_config_id = ? ORDER BY position",
                               (e["id"], config_id)).fetchall()
            digest = hashlib.sha256(searchable(e).encode()).hexdigest()
            vs = [np.frombuffer(v, dtype="<f4") for v, h, ver in found
                  if dims and len(v) == dims[0] * 4 and h == digest and ver == e["version"]]
            if not found or len(vs) != len(found) or len(vs) != len(texts(e)) or \
                    not all(np.isfinite(v).all() and abs(float(np.linalg.norm(v)) - 1) < 1e-3 for v in vs):
                self.stale.append(e["id"])                    # missing, outdated or broken: never matched
                continue
            self.ids += [e["id"]] * len(vs)
            rows += vs
        self.matrix = np.vstack(rows) if rows else np.zeros((0, dims[0] if dims else 1), dtype="<f4")

    def retrieve(self, text: str, k: int = 10) -> list[tuple[str, float]]:
        """The k entries closest to `text`, each by its best vector."""
        if not self.ids:
            return []
        scores, best = self.matrix @ encode(self.config_id, [text], "query")[0], {}
        for i in np.argsort(-scores, kind="stable"):
            best.setdefault(self.ids[i], float(scores[i]))
            if len(best) == k:
                break
        return list(best.items())

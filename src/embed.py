"""
Embedding stage: read chunks from JSONL, embed with a multilingual model,
and store vectors + metadata in a persistent local Chroma collection.

Model: intfloat/multilingual-e5-large. E5 models are trained with an
instruction prefix — documents must be embedded as "passage: <text>" and
queries as "query: <text>". Mixing these up degrades retrieval, so the
prefixing lives here (and in retrieve.py) rather than in callers.

Typical use from the project root:

    py -3 -m src.embed --chunks data/processed/20241_sample.jsonl
    py -3 -m src.embed --chunks data/processed/20241_sample.jsonl --reset
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

# Use the Windows certificate store for TLS instead of certifi's bundle —
# the Hugging Face model download fails cert verification otherwise on
# machines where AV/network software re-signs HTTPS traffic.
import truststore

truststore.inject_into_ssl()

import chromadb
from sentence_transformers import SentenceTransformer

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHROMA_DIR = DATA_DIR / "chroma"

MODEL_NAME = "intfloat/multilingual-e5-large"
COLLECTION_NAME = "folketing_speeches"
BATCH_SIZE = 32

log = logging.getLogger("embed")

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """Load the embedding model once per process."""
    global _model
    if _model is None:
        log.info("Loading embedding model %s ...", MODEL_NAME)
        t0 = time.time()
        _model = SentenceTransformer(MODEL_NAME)
        log.info("Model loaded in %.1fs", time.time() - t0)
    return _model


def get_collection(persist_dir: Path = CHROMA_DIR) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=str(persist_dir))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def load_chunks(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def _clean_metadata(meta: dict) -> dict:
    """Chroma rejects None values; drop them and coerce to str/int/float/bool."""
    return {k: v for k, v in meta.items() if v is not None}


def embed_chunks(
    chunks: list[dict],
    collection: chromadb.Collection,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Embed and upsert chunks into Chroma. Returns number embedded.

    Chunks already present in the collection (by id) are skipped, so the
    function is safe to re-run after adding more meetings to the JSONL.
    """
    existing: set[str] = set()
    ids_all = [c["chunk_id"] for c in chunks]
    # Query existing ids in batches; collection.get with many ids is fine.
    for i in range(0, len(ids_all), 500):
        got = collection.get(ids=ids_all[i : i + 500], include=[])
        existing.update(got["ids"])

    todo = [c for c in chunks if c["chunk_id"] not in existing]
    if not todo:
        log.info("All %d chunks already embedded — nothing to do.", len(chunks))
        return 0
    log.info("Embedding %d new chunks (%d already present)", len(todo), len(existing))

    model = get_model()
    t0 = time.time()
    done = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        texts = ["passage: " + c["text"] for c in batch]
        vectors = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        collection.upsert(
            ids=[c["chunk_id"] for c in batch],
            embeddings=vectors.tolist(),
            documents=[c["text"] for c in batch],
            metadatas=[_clean_metadata(c["metadata"]) for c in batch],
        )
        done += len(batch)
        if done % (batch_size * 10) == 0 or done == len(todo):
            rate = done / (time.time() - t0)
            eta = (len(todo) - done) / rate if rate else 0
            log.info("  %d/%d chunks (%.1f/s, eta %.0fs)", done, len(todo), rate, eta)
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="Embed chunks into Chroma.")
    ap.add_argument("--chunks", type=Path, required=True,
                    help="JSONL produced by src.ingest")
    ap.add_argument("--reset", action="store_true",
                    help="Delete the collection and re-embed from scratch")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if args.reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            log.info("Deleted existing collection %s", COLLECTION_NAME)
        except Exception:
            pass
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    chunks = load_chunks(args.chunks)
    log.info("Loaded %d chunks from %s", len(chunks), args.chunks)
    n = embed_chunks(chunks, collection, batch_size=args.batch_size)
    log.info("Done. Embedded %d chunks; collection now holds %d.", n, collection.count())


if __name__ == "__main__":
    main()

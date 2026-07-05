"""
Retrieval stage: embed a Danish query and find the most similar chunks
in the Chroma collection, returning text + metadata for citation.

The E5 model requires the "query: " prefix on search queries (documents
were embedded with "passage: " in embed.py).

Typical use from the project root:

    py -3 -m src.retrieve "hvad mener partierne om ulve i Danmark?"
    py -3 -m src.retrieve "sundhedspersoner fra tredjelande" --k 3 --party EL
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from src.embed import get_collection, get_model


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float                 # cosine similarity in [0, 1], higher = closer
    metadata: dict = field(default_factory=dict)


def retrieve(
    question: str,
    k: int = 6,
    party: str | None = None,
    meeting_date: str | None = None,
) -> list[RetrievedChunk]:
    """Return the top-k chunks most similar to `question`.

    Optional filters restrict the search by exact metadata match before
    similarity ranking (Chroma applies the filter inside the index).
    """
    model = get_model()
    query_vec = model.encode(
        "query: " + question,
        normalize_embeddings=True,
    )

    where: dict | None = None
    filters = []
    if party:
        filters.append({"party_short": party})
    if meeting_date:
        filters.append({"meeting_date": meeting_date})
    if len(filters) == 1:
        where = filters[0]
    elif filters:
        where = {"$and": filters}

    collection = get_collection()
    res = collection.query(
        query_embeddings=[query_vec.tolist()],
        n_results=k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    out: list[RetrievedChunk] = []
    for cid, doc, meta, dist in zip(
        res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
    ):
        out.append(
            RetrievedChunk(
                chunk_id=cid,
                text=doc,
                # Chroma returns cosine *distance* (1 - similarity).
                score=1.0 - dist,
                metadata=meta or {},
            )
        )
    return out


def format_source(rc: RetrievedChunk) -> str:
    """One-line human-readable citation for a retrieved chunk."""
    m = rc.metadata
    speaker = m.get("speaker") or "ukendt taler"
    party = f" ({m['party_short']})" if m.get("party_short") else ""
    agenda = m.get("agenda_short_title") or ""
    return f"{speaker}{party}, {m.get('meeting_date', '?')}, {agenda}".strip().rstrip(",")


def main() -> None:
    ap = argparse.ArgumentParser(description="Query the Folketing chunk index.")
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--party", help="Filter by party short code, e.g. S, V, EL")
    ap.add_argument("--date", dest="meeting_date", help="Filter by meeting date YYYY-MM-DD")
    args = ap.parse_args()

    results = retrieve(args.question, k=args.k, party=args.party,
                       meeting_date=args.meeting_date)
    for i, rc in enumerate(results, 1):
        print(f"--- {i}. score={rc.score:.3f}  {rc.chunk_id}")
        print(f"    {format_source(rc)}")
        print(f"    {rc.text[:280]}")
        print()


if __name__ == "__main__":
    main()

"""
Generation stage: answer a question in Danish, grounded in retrieved chunks
from the Folketing debates, with numbered citations back to specific speeches.

Requires ANTHROPIC_API_KEY in the environment.

Typical use from the project root:

    py -3 -m src.generate "Hvad mener partierne om ulve i Danmark?"
    py -3 -m src.generate "Hvad er holdningen til Kina?" --k 8
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

# Same Windows cert-store fix as embed.py — the Anthropic SDK uses httpx,
# which fails cert verification on this machine without it.
import truststore

truststore.inject_into_ssl()

import anthropic

from src.retrieve import RetrievedChunk, format_source, retrieve

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096  # grounded answers are short; well under any timeout risk
DEFAULT_K = 6

# The two rules that matter most, learned during retrieval verification:
# (1) cite by number so answers are checkable, and (2) E5 similarity scores
# can't tell "no answer exists" apart from a weak match — so the refusal
# behaviour has to live here, in the instructions to the model.
SYSTEM_PROMPT = """\
Du er en assistent, der besvarer spørgsmål om debatter i Folketinget.

Du får et spørgsmål og et antal nummererede uddrag [1], [2], ... fra \
folketingsdebatter. Hvert uddrag har taler, parti, dato og dagsordenspunkt.

Regler:
- Svar på dansk.
- Basér dit svar UDELUKKENDE på de vedlagte uddrag. Brug ikke din egen viden \
om emnet.
- Citér kilder med deres nummer i firkantede parenteser, fx [1] eller [2][4], \
hver gang du gengiver et synspunkt eller et faktum.
- Angiv hvem der sagde hvad: navn og parti, fx "Peter Kofod (DF) mener at ...".
- Hvis uddragene ikke indeholder svar på spørgsmålet, så skriv "Det fremgår \
ikke af det tilgængelige materiale." og gæt ikke. Det gælder også hvis \
uddragene kun er løst relaterede til spørgsmålet.
- Opfind aldrig citater, navne eller holdninger, som ikke står i uddragene.
"""


@dataclass
class GroundedAnswer:
    question: str
    answer: str
    sources: list[dict] = field(default_factory=list)


def _format_context(chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for i, rc in enumerate(chunks, 1):
        m = rc.metadata
        header = (
            f"[{i}] {m.get('speaker') or 'Ukendt taler'}"
            f" ({m.get('party_short') or '?'})"
            f" | {m.get('meeting_date', '?')}"
            f" | {m.get('agenda_short_title') or 'ukendt dagsordenspunkt'}"
        )
        blocks.append(f"{header}\n{rc.text}")
    return "\n\n".join(blocks)


def _source_entry(index: int, rc: RetrievedChunk) -> dict:
    return {
        "n": index,
        "chunk_id": rc.chunk_id,
        "score": round(rc.score, 3),
        "label": format_source(rc),
        "speaker": rc.metadata.get("speaker"),
        "party": rc.metadata.get("party_short"),
        "meeting_date": rc.metadata.get("meeting_date"),
        "agenda": rc.metadata.get("agenda_short_title"),
        "text": rc.text,
    }


def answer_question(
    question: str,
    k: int = DEFAULT_K,
    party: str | None = None,
) -> GroundedAnswer:
    """Retrieve context and generate a grounded, cited answer in Danish."""
    chunks = retrieve(question, k=k, party=party)
    context = _format_context(chunks)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Uddrag fra folketingsdebatter:\n\n{context}\n\n"
                    f"Spørgsmål: {question}"
                ),
            }
        ],
    )

    answer = "".join(b.text for b in response.content if b.type == "text")
    return GroundedAnswer(
        question=question,
        answer=answer,
        sources=[_source_entry(i, rc) for i, rc in enumerate(chunks, 1)],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask a question about Folketinget.")
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--party", help="Filter retrieval by party short code, e.g. S, EL")
    args = ap.parse_args()

    result = answer_question(args.question, k=args.k, party=args.party)
    print(result.answer)
    print()
    print("Kilder:")
    for s in result.sources:
        print(f"  [{s['n']}] (score {s['score']}) {s['label']}")


if __name__ == "__main__":
    main()

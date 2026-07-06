"""
FastAPI wrapper around the RAG pipeline.

POST /ask  {"question": "...", "k": 6, "party": null}
        -> {"answer": "...", "sources": [...]}
GET  /health  -> {"status": "ok", "chunks": <collection size>}

The embedding model takes ~30s to load, so it is warmed once at startup
rather than on the first request.

Run from the project root:

    py -3 -m uvicorn src.api:app --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.embed import get_collection, get_model
from src.generate import answer_question

log = logging.getLogger("api")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)
    k: int = Field(default=6, ge=1, le=20)
    party: str | None = Field(default=None, max_length=10,
                              description="Party short code filter, e.g. S, V, EL")


class Source(BaseModel):
    n: int
    chunk_id: str
    score: float
    label: str
    speaker: str | None = None
    party: str | None = None
    meeting_date: str | None = None
    agenda: str | None = None
    text: str


class AskResponse(BaseModel):
    question: str
    answer: str
    sources: list[Source]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the embedding model and vector store before accepting traffic.
    log.info("Warming embedding model and Chroma collection ...")
    get_model()
    n = get_collection().count()
    log.info("Ready — %d chunks indexed.", n)
    yield


app = FastAPI(
    title="Folketing RAG API",
    description="Grounded Q&A over Danish parliamentary debates.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "chunks": get_collection().count()}


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    try:
        result = answer_question(req.question, k=req.k, party=req.party)
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=502, detail="Anthropic API key invalid or missing.")
    except anthropic.RateLimitError:
        raise HTTPException(status_code=503, detail="Anthropic API rate limited — try again shortly.")
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {exc.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=502, detail="Could not reach the Anthropic API.")
    return AskResponse(
        question=result.question,
        answer=result.answer,
        sources=[Source(**s) for s in result.sources],
    )

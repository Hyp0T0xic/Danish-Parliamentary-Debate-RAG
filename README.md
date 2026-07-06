# Danish Parliamentary Debate RAG

Ask questions about Danish parliamentary debates in plain Danish, and get answers with citations pointing back to the exact speech, speaker, and party.

**Example:** ask *"Hvad mener partierne om ulve i Danmark?"* and you get a summary of where each party stands on wolves, with every claim linked to a numbered source you can expand and read yourself.

![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## What it does

Folketinget (the Danish Parliament) publishes full transcripts of every debate as open data. This project turns those transcripts into a question-answering system:

1. **Downloads** debate transcripts from Folketinget's open FTP server
2. **Parses** the XML into individual speeches: who spoke, for which party, on which agenda item
3. **Chunks** each speech into passages and **embeds** them with a multilingual model that handles Danish well
4. **Stores** everything in a local vector database (Chroma)
5. When you ask a question, it **finds the most relevant passages** and asks Claude to write a grounded answer in Danish, with strict instructions to cite sources and to say *"det fremgår ikke af materialet"* instead of guessing when the debates don't contain an answer

No hallucinated politics: every claim in an answer traces back to a real speech you can read.

## How it's put together

```
Folketinget FTP  ──►  ingest.py  ──►  embed.py  ──►  Chroma (local vector DB)
   (XML files)      parse + chunk      e5-large           │
                                                          ▼
you ──► Streamlit UI ──► FastAPI ──► retrieve.py ──► generate.py ──► answer + sources
         (port 8501)    (port 8000)   top-k search     Claude API
```

| File | What it does |
|---|---|
| `src/ingest.py` | Downloads XML from `oda.ft.dk`, parses speeches, chunks them with metadata |
| `src/embed.py` | Embeds chunks with `intfloat/multilingual-e5-large`, stores them in Chroma |
| `src/retrieve.py` | Embeds your question, finds the top-k most similar chunks |
| `src/generate.py` | Sends question + retrieved context to Claude, gets a cited answer |
| `src/api.py` | FastAPI wrapper exposing `POST /ask` |
| `frontend/app.py` | Streamlit UI with example questions and expandable sources |

## Getting started

You'll need Python 3.12+ and an [Anthropic API key](https://console.anthropic.com/settings/keys).

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Set your API key**

```powershell
# Windows (persistent)
[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', 'sk-ant-...', 'User')
```

```bash
# macOS / Linux
export ANTHROPIC_API_KEY='sk-ant-...'
```

**3. Download and parse some debates**

```bash
python -m src.ingest --session 20241 --max-files 10
```

This grabs 10 meetings from the 2024-25 session (~4,000 speeches). Sessions are named like `20241` = first session of the parliamentary year 2024-25.

**4. Embed them**

```bash
python -m src.embed --chunks data/processed/20241.jsonl
```

**Heads up:** the first run downloads a ~2.2 GB embedding model, and embedding is slow on CPU (roughly an hour for 10 meetings). It's a one-time cost, since re-runs only embed new chunks. If you have a GPU it's minutes instead.

**5. Run it**

```bash
# Terminal 1: the API
python -m uvicorn src.api:app --port 8000

# Terminal 2: the UI
python -m streamlit run frontend/app.py
```

Open [http://localhost:8501](http://localhost:8501) and ask away.

## What can you ask?

Whatever was actually debated in the meetings you ingested. With the default 10 meetings (May–September 2025) that includes wolves in Denmark, China/Taiwan policy, weapons trade with Israel, the euro opt-out, adoption scandals, citizenship laws, tanning-bed age limits, and more.

If you ask about something the corpus doesn't cover, the system says so instead of making things up. That's by design.

## Lessons from building this (the fun bugs)

A few things that went wrong along the way and shaped the design:

- **Danish transcripts omit spaces after periods** (`"Tak for det.Jeg mener..."`), which silently broke sentence splitting and produced 1,400-word chunks. The fix only inserts a split when a letter sits on *both* sides of the period, so legal references like `stk.4` and dates like `1.september` survive.
- **One-word chunks poisoned retrieval.** A third of the raw chunks were procedural fragments like *"Ministeren."* (the chair giving someone the floor). Very short texts embed as near-generic vectors that match *everything*, so they crowded out real results. Chunks under 15 words are now dropped at ingest.
- **Similarity scores can't detect "no answer".** With E5 embeddings, a genuine hit scores ~0.87 and complete garbage scores ~0.83, which is too close for a threshold. So the "I don't know" behavior lives in the LLM prompt instead, which judges whether the retrieved context actually answers the question.
- **Corporate/AV certificates break Python HTTPS on Windows.** Model downloads and API calls failed with `CERTIFICATE_VERIFY_FAILED` because Python doesn't use the Windows certificate store by default. Fixed with [`truststore`](https://pypi.org/project/truststore/), which does proper verification via the OS store rather than disabling SSL.

## Data source

- **Transcripts:** [Folketingets open data](https://www.ft.dk/dokumenter/aabne_data), served over anonymous FTP at `oda.ft.dk/ODAXML/Referat/samling/`, one XML file per sitting, sessions from 2009 onward
- The XML schema (namespace `http://FT.PIP.Afskrift.Schemas`) nests each speech as `Tale → TaleSegment → TekstGruppe → ... → Char`, with speaker metadata (name, party, role) and agenda metadata (case number, title) attached

## Tech stack

`lxml` · `sentence-transformers` (multilingual-e5-large) · `chromadb` · `anthropic` (claude-sonnet-4-6) · `fastapi` · `streamlit`

## Ideas for where to take it

- Ingest full sessions (the pipeline is incremental and skips already-embedded chunks)
- Stream answers token-by-token so the UI feels faster
- Hybrid retrieval (BM25 + embeddings) for exact matches on bill numbers like "L 223"
- Show the full speech when a chunk is cited (`speech_id` is already tracked for this)

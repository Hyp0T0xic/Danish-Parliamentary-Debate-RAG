# Danish Parliamentary Debate RAG: A Walkthrough

This document is a narrative record of how this project was built, in the order it actually happened, including the reasoning behind each decision, the bugs that were found along the way, and questions that came up while learning the system.

## 1. Finding the data: probing before building

The project needed to pull real transcripts from the Danish Parliament (Folketinget). The prompt that kicked off the work mentioned two conflicting FTP folder names for where the data might live. Rather than guessing and hardcoding one of them, the first move was to connect directly to the FTP server at `oda.ft.dk` and manually walk the directory tree.

This is called probing: exploring an unfamiliar system's structure before writing code against it. It took about 30 seconds and revealed the real path: `/ODAXML/Referat/samling/<session>/`, different from both hints in the original prompt. A "session" turned out to mean one parliamentary year, identified by a code like `20241` (the 2024-25 session, first half).

The reasoning behind doing this first: if the path had been hardcoded incorrectly, every resulting error later in the pipeline would have looked like an XML parsing bug, when the real problem would have been a wrong URL. Verifying the address before writing a downloader avoided an entire category of confusing, misleading errors.

One clarifying question that came up here was whether the FTP counted as an "API." It does not, technically. FTP is just a file server, a folder structure exposed over the internet that you can browse and download from. There is no request/response contract the way a REST API has, just files sitting in nested directories.

## 2. Understanding the file format before parsing it

Once the correct path was known, one XML file (about 200KB) was downloaded and opened by hand. This was a distinct, deliberate step, separate from the FTP probing, and it happened only after a real file was in hand. Before writing a parser, the goal was to understand the actual schema: what tags exist, how they nest, and where the useful information lives.

Several important findings came out of this inspection:

- The root element `<Dokument>` sits inside an XML namespace, `http://FT.PIP.Afskrift.Schemas`. Without correctly handling this, every attempt to search for a tag by name would silently return nothing, with no error to explain why.
- The actual spoken text is not stored where you would expect. Every single word lives inside a `<Char>` tag, nested four levels deep: `TekstGruppe` to `Exitus` to `Linea` to `Char`. A helper function was written to walk down and collect every `Char` element's text and join it back together.
- Metadata about the speaker (first name, last name, party, role, and a stable speaker ID) lives in a different part of the tree than the metadata about the topic being debated (item number, short title, case type and number like "L 223" for a bill).

Without this inspection step, the parser would have either missed key information or been over-engineered around a guessed structure. This step answered a question that came up early: XML, in this context, is simply an alternative to JSON or CSV, a way to represent structured data as readable text, using nested tags instead of brackets or commas. Governments often standardized on XML because it was the dominant format when these systems were built, and there was no strong reason to migrate away from it since.

## 3. The shape of the data: sessions, meetings, agenda items, and speeches

Once the structure was understood, the following hierarchy became clear, and this hierarchy shaped everything downstream:

```
Session (one parliamentary year, e.g. 20241)
  Meeting (one sitting day)
    Agenda item (one topic being debated that day)
      Activity
        Speech (one continuous turn by one speaker)
          Segment (a timestamped slice of that same speech)
            Text (individual words, nested down to Char elements)
```

A recurring point of clarification during this stage was: does a lower-level element (like a segment) automatically carry the information above it (like the meeting date or the agenda topic)? The answer is no. XML nesting only gives you what is literally inside a tag. The code has to explicitly walk upward and copy the relevant metadata down into each speech as it is built. This was one of the more important realizations of the whole project: nothing is inherited for free, everything has to be assembled on purpose.

A new speaker turn is defined structurally, not interpretively: every time a `<Tale>` (speech) tag appears in the transcript, that marks a new, distinct turn, whether it is a full five-minute argument or a one-word interjection like "Ministeren." (meaning "the Minister has the floor"). The parser does not try to be clever about merging or splitting turns, it simply trusts the official transcript's own turn-taking record.

## 4. Two dataclasses, not one: Speech and Chunk

The parser produces `Speech` objects, one per speaker turn, each one a flat record combining the person's own words with every relevant piece of metadata gathered from the levels above it in the tree: session, meeting date, agenda topic, case number, speaker name, party, role, and timing.

A `Speech`, however, is not the final unit used for search. A second, deliberately separate object exists: `Chunk`. The distinction matters:

- `Speech` is the natural unit of identity: one person, one continuous turn, the thing a citation should point back to.
- `Chunk` is the retrieval unit: a slice of a speech, sized specifically for the embedding model that will later turn it into a searchable vector.

Every chunk carries a `speech_id` linking it back to its parent speech, so that if a user interface ever wants to show the full original speech for context around a specific matched chunk, it can. Splitting these into two separate concepts, rather than flattening everything into just chunks, preserves that grouping.

## 5. Turning one long speech into chunks

The chunking process happens in a specific, deliberate order, and it was walked through step by step during this project:

**Step one: split into sentences.** A speech's full text, one long string, gets broken apart at sentence-ending punctuation (period, exclamation mark, question mark), producing a list of individual sentences.

**Step two: group sentences into a buffer.** Sentences get added to a running buffer one at a time. A target size of around 320 words is used as the point where a chunk gets finalized, with a hard cap of 420 words that forces a cut even earlier if a sentence would otherwise push the buffer over that limit.

**Step three: carry one sentence of overlap.** When a chunk is finalized, its very last sentence is copied forward into the next chunk's buffer as a head start.

**Step four: repeat until the whole speech is consumed**, and finally join the sentences within each buffer back into a single continuous string per chunk.

A key clarification came up here: if the sentences get rejoined into one string at the end, what was the point of splitting them in the first place? The answer is that splitting into sentences was never about permanently breaking the text apart, it was about finding safe places to cut. For a short speech that never exceeds the word cap, the sentence-splitting is effectively inert, everything still ends up as one single chunk. It only becomes meaningful for longer speeches, where the buffer genuinely fills up multiple times, producing multiple separate `Chunk` objects, each with a different slice of the original sentences.

The overlap logic prompted a similarly useful question: if chunks are always cut cleanly between full sentences, why is the overlap sentence still necessary? The answer is that clean sentence boundaries prevent a chunk from containing a broken half-sentence, but they do not prevent a chunk from losing shared context with its neighbor. Danish, like English, is full of cross-sentence references: "he," "it," "this law." If a chunk starts immediately after such a reference was resolved in the previous chunk, the new chunk reads as ambiguous on its own once it becomes an independent, isolated vector. Carrying the last sentence forward as overlap keeps each chunk self-contained.

Once real chunk-size statistics were examined, it became clear that most individual speeches in this dataset are short. The median chunk size was only 49 words, well under the 320-word target, meaning most speeches produce exactly one chunk. The sentence-boundary and overlap logic mainly activates for the minority of longer speeches, ministerial answers or extended debate contributions that actually cross the word threshold.

## 6. Word count as a stand-in for tokens

The chunk size limits are expressed in words, not in the actual units the AI model works with, called tokens. A token is how a model internally breaks up text, not always whole words: a word like "playing" might become two tokens, "play" and "ing." Measuring the real token count for every chunk would require running the model's own tokenizer at chunking time, which is slow and ties the ingestion code to one specific model. Since Danish text averages out to roughly one word per token, word count was used instead, as a fast, model-agnostic approximation that lands the chunk size in the desired 300 to 500 token range.

## 7. A real bug: the missing space after periods

After the first chunking pass, the resulting chunk sizes were checked, and one number stood out: a maximum chunk size of 1464 words, several times over the intended cap. Investigation revealed the cause: Danish transcripts in this dataset often omit the space after a sentence-ending period, writing something like `"Tak for det.Jeg bliver nødt til..."` with no space. The original sentence-splitting rule required whitespace after the punctuation to recognize a boundary, so for any speech written this way, no sentence boundaries were ever found, and the entire speech was treated as one unbreakable chunk.

A naive fix, splitting on any period followed by a capital letter, would have introduced new problems, incorrectly breaking on things like `"1.september"` (a Danish date format) or `"stk.4"` (a legal paragraph reference). The actual fix only inserts a space when the period sits between two letters on both sides, leaving digit-adjacent periods untouched. After this fix, the maximum chunk size dropped to 380 words, safely under the cap.

This bug mattered strategically because it was caught before the embedding stage. Had it gone unnoticed, those oversized chunks would have produced blurry, unhelpful vectors, and the resulting bad search results could easily have been dismissed as an inherent limitation of semantic search rather than traced back to this specific formatting quirk.

## 8. Turning text into vectors

Once clean chunks existed, the next stage, `embed.py`, converts each chunk's text into a vector: a list of 1024 numbers produced by a specific AI model, `multilingual-e5-large`. This model was chosen for two reasons: it was trained specifically for retrieval tasks (as opposed to general-purpose language generation), and it performs well across languages, which mattered since all of the source text is in Danish.

This model has a specific convention, prefixing text with `"passage: "` before storing it, and `"query: "` before searching with it. This prefix is literally just prepended to the string before encoding. Skipping it does not cause an error but measurably lowers retrieval quality, since the model was trained expecting this distinction. To avoid ever forgetting it, the prefixing logic lives inside the shared code, not left to whoever calls it.

Encoding was run in batches of 32 chunks at a time for efficiency, and the resulting vectors were normalized, rescaled to a consistent length, so that later similarity comparisons reduce to simple, fast arithmetic.

A real-world snag appeared here too: the first attempt to download the model from Hugging Face failed with a certificate verification error. The cause turned out to be security software on the machine that intercepts and re-signs encrypted web traffic, something Windows itself trusts but Python's own separate list of trusted certificates does not recognize. The fix was to route Python's certificate checking through the operating system's own trusted certificate store instead, which is a legitimate verification method, not a workaround that disables security.

A second, unavoidable limitation surfaced during embedding: the machine had no GPU, only a CPU, and this particular model is large and accurate but slow to run without specialized hardware. Embedding around 3,927 chunks took roughly three hours. This was accepted for the current scale of the project, ten meetings, but was flagged as a real bottleneck if the dataset were ever expanded to a full parliamentary session.

## 9. Storing and searching the vectors

The resulting vectors are stored in Chroma, a database built specifically for this kind of similarity search, as opposed to a general-purpose relational database like PostgreSQL, which is built for exact row and column matching rather than "find me the closest match" style queries. PostgreSQL can be extended to do this with an add-on called pgvector, but Chroma comes with this capability built in, with far less setup, which fit a small local project well.

Chroma finds similar vectors efficiently using an algorithm called HNSW, short for Hierarchical Navigable Small World. Rather than comparing a search query against every stored vector one at a time, HNSW organizes vectors into a layered graph during insertion, with a sparse top layer offering long-distance shortcuts and denser lower layers connecting close neighbors. A search starts at the top and works down, quickly narrowing toward the closest matches without checking everything. A useful clarification here was that this graph is not built around any single "most central" vector deliberately chosen for that role, the layering emerges from a randomized process during insertion, not a calculated center of the dataset.

Similarity itself is measured using cosine similarity, which compares the angle between two vectors rather than their length or magnitude, capturing whether two pieces of text point in the same conceptual direction regardless of how much text each one contains.

## 10. Testing retrieval and catching a second bug

Once the index was built, a small, informal set of four Danish test questions was run through the retrieval code as a manual spot-check, not a formal or automated test suite. Two of them worked very well: a question about wolves in Denmark returned genuinely relevant speeches from the correct debate, and a question about China and Taiwan correctly surfaced the foreign minister's actual answer.

A third question, about foreign healthcare workers, returned something clearly wrong: three copies of the single word chunk "Ministeren.", a procedural announcement rather than real content. Investigating this revealed that very short chunks, like this one-word interjection, produce vectors that sit in a kind of generic middle ground of the vector space, making them appear moderately similar to almost any query, and crowding out genuinely relevant results. The fix was to add a minimum chunk length of 15 words at the ingestion stage, discarding anything shorter. This removed 1,307 chunks, roughly a third of the entire dataset, all of it this kind of procedural noise. Rather than repeating the costly multi-hour re-embedding process, the existing vector database was reconciled directly against the corrected list of chunks, deleting only the outdated entries.

A fourth question, deliberately about a topic never discussed in these meetings (artificial intelligence), returned results anyway, since the search always returns its best available matches regardless of whether a good match truly exists. These results scored only slightly lower than genuine matches, around 0.83 to 0.84 compared to 0.86 to 0.88 for real hits. This narrow gap led to an important design decision: no fixed numeric threshold was used to decide whether an answer exists. The margin between relevant and irrelevant scores was judged too thin and unreliable to hardcode a cutoff. Instead, that judgment was pushed to the final stage of the pipeline, where the language model itself reads the retrieved text and decides whether it genuinely answers the question, explicitly instructed to respond with a fixed Danish phrase meaning "this does not appear in the available material" when it does not.

## 11. Generating grounded answers

The final stage of the core pipeline, `generate.py`, takes the top-k retrieved chunks for a question, formats them as numbered, labeled excerpts (including speaker, party, date, and topic), and sends them to Claude alongside the original question. A Danish-language system prompt instructs the model to answer only using the provided excerpts, cite every claim by its excerpt number, correctly attribute statements to the right speaker and party, and explicitly decline to answer, using the fixed refusal phrase, when the excerpts do not actually address the question. This is the safeguard against hallucination: since similarity scores alone cannot reliably separate relevant from irrelevant results, the responsibility for that judgment sits with the model that can actually read and reason about the content.

## 12. Wrapping it in a usable interface

Beyond the core pipeline, a REST API (`api.py`, built with FastAPI) exposes a question-answering endpoint and a basic health check, and a Streamlit-based frontend provides a simple web page for asking questions, with sidebar controls for adjusting the number of results and optionally filtering by party. The embedding model is deliberately loaded once when the server starts, rather than on each incoming request, since loading it takes around thirty seconds.

## 13. Where evaluation currently stands

It is worth being honest about the current state of quality evaluation. What exists is the manual four-question spot-check described above, along with the raw similarity scores as a rough signal. This is not the same as a formal, reproducible evaluation using metrics like precision at k, recall at k, or NDCG, which would require a labeled test set: a list of questions paired with a manually verified list of the correct chunks that should be retrieved for each one. No such labeled set currently exists for this project. The current approach is qualitative and exploratory rather than statistically rigorous, and would be a natural area for future work.

## Closing note

The overarching thread across this entire build was a discipline of verifying assumptions before building on top of them: probing the file structure before downloading, inspecting one real file before writing a parser, checking chunk-size statistics before moving on to embedding, and manually spot-checking real queries before trusting the retrieval system. Both real bugs encountered in this project, the missing-space sentence splitting issue and the one-word junk chunk problem, were invisible until real output was actually examined. Neither would have been caught by assumption alone.

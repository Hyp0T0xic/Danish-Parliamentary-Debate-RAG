"""
Ingest pipeline for Folketingets debate transcripts (referater).

Stages:
  1. Download XML files from oda.ft.dk FTP for a given parliamentary session.
  2. Parse each file into a list of Speech records.
  3. Chunk speech text into ~300-500 token windows, carrying metadata.
  4. Write chunks to a JSONL file for the embedding stage to consume.

Data source: ftp://oda.ft.dk/ODAXML/Referat/samling/<session>/

Typical use from the project root:

    py -3 -m src.ingest --session 20241 --max-files 5
    py -3 -m src.ingest --session 20241          # full session
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from ftplib import FTP
from pathlib import Path
from typing import Iterable, Iterator

from lxml import etree

FTP_HOST = "oda.ft.dk"
FTP_BASE = "/ODAXML/Referat/samling"
NS = "{http://FT.PIP.Afskrift.Schemas}"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

log = logging.getLogger("ingest")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Speech:
    """A single contiguous speech by one speaker within an agenda item."""
    speech_id: str
    session: str
    meeting_number: str
    meeting_date: str             # ISO date, e.g. 2025-09-04
    agenda_item_no: str | None
    agenda_short_title: str | None
    case_type: str | None
    case_number: str | None
    speaker_first_name: str | None
    speaker_last_name: str | None
    speaker_role: str | None
    party_short: str | None       # GroupNameShort
    speaker_title: str | None     # rendered "TalerTitel"
    speech_type: str | None       # TaleType
    start_time: str | None        # ISO datetime
    end_time: str | None
    text: str


@dataclass
class Chunk:
    """A retrievable chunk: a slice of a speech with full metadata."""
    chunk_id: str
    speech_id: str
    chunk_index: int
    text: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# FTP download
# ---------------------------------------------------------------------------

def _ftp_connect() -> FTP:
    ftp = FTP(FTP_HOST, timeout=60)
    ftp.login()  # anonymous
    return ftp


def list_sessions() -> list[str]:
    with _ftp_connect() as ftp:
        ftp.cwd(FTP_BASE)
        return sorted(ftp.nlst())


def list_session_files(session: str) -> list[str]:
    with _ftp_connect() as ftp:
        ftp.cwd(f"{FTP_BASE}/{session}")
        return sorted(f for f in ftp.nlst() if f.endswith(".xml"))


def download_session(
    session: str,
    out_dir: Path = RAW_DIR,
    max_files: int | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """Download XML files for one session. Returns local paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = list_session_files(session)
    if max_files:
        files = files[:max_files]
    log.info("Session %s: %d files to consider", session, len(files))

    paths: list[Path] = []
    with _ftp_connect() as ftp:
        ftp.cwd(f"{FTP_BASE}/{session}")
        for name in files:
            local = out_dir / name
            paths.append(local)
            if local.exists() and not overwrite:
                log.debug("Skip (exists): %s", name)
                continue
            log.info("Downloading %s", name)
            with open(local, "wb") as fh:
                ftp.retrbinary(f"RETR {name}", fh.write)
    return paths


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _strip_ns(root: etree._Element) -> etree._Element:
    """Drop the Folketinget namespace so XPath queries are simple."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    etree.cleanup_namespaces(root)
    return root


def _text(el: etree._Element | None) -> str:
    """Concatenate all <Char> text inside an element, normalised."""
    if el is None:
        return ""
    parts = [(c.text or "") for c in el.iter("Char")]
    raw = "".join(parts)
    # Collapse whitespace, drop control chars.
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _findtext(el: etree._Element | None, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def parse_xml_file(path: Path) -> list[Speech]:
    """Parse one transcript file into a list of Speech records."""
    tree = etree.parse(str(path))
    root = _strip_ns(tree.getroot())

    mm = root.find("MetaMeeting")
    session = _findtext(mm, "ParliamentarySession") or ""
    meeting_no = _findtext(mm, "MeetingNumber") or ""
    date_raw = _findtext(mm, "DateOfSitting") or ""
    meeting_date = date_raw.split("T", 1)[0] if date_raw else ""

    speeches: list[Speech] = []
    for punkt in root.findall("DagsordenPunkt"):
        meta_item = punkt.find("MetaFTAgendaItem")
        item_no = _findtext(meta_item, "ItemNo")
        short_title = _findtext(meta_item, "ShortTitle")
        case_type = _findtext(meta_item, "FTCaseType")
        case_number = _findtext(meta_item, "FTCaseNumber")

        for aktivitet in punkt.findall("Aktivitet"):
            for tale in aktivitet.findall("Tale"):
                speeches.append(
                    _parse_tale(
                        tale,
                        session=session,
                        meeting_number=meeting_no,
                        meeting_date=meeting_date,
                        agenda_item_no=item_no,
                        agenda_short_title=short_title,
                        case_type=case_type,
                        case_number=case_number,
                    )
                )

    # Drop empty speeches (procedural blanks).
    return [s for s in speeches if s.text]


def _parse_tale(
    tale: etree._Element,
    *,
    session: str,
    meeting_number: str,
    meeting_date: str,
    agenda_item_no: str | None,
    agenda_short_title: str | None,
    case_type: str | None,
    case_number: str | None,
) -> Speech:
    taler = tale.find("Taler")
    meta_sp = taler.find("MetaSpeakerMP") if taler is not None else None

    first = _findtext(meta_sp, "OratorFirstName")
    last = _findtext(meta_sp, "OratorLastName")
    role = _findtext(meta_sp, "OratorRole")
    party = _findtext(meta_sp, "GroupNameShort")
    speaker_id = meta_sp.get("tingdokID") if meta_sp is not None else None
    speaker_title = _text(taler.find("TalerTitel")) if taler is not None else ""

    speech_type = _text(tale.find("TaleType")) or None

    # Concatenate text across all TaleSegment/TekstGruppe in order.
    segments = tale.findall("TaleSegment")
    text_parts: list[str] = []
    start_time = end_time = None
    for seg in segments:
        meta = seg.find("MetaSpeechSegment")
        if meta is not None:
            seg_start = _findtext(meta, "StartDateTime")
            seg_end = _findtext(meta, "EndDateTime")
            if start_time is None and seg_start:
                start_time = seg_start
            if seg_end:
                end_time = seg_end
        for tg in seg.findall("TekstGruppe"):
            chunk = _text(tg)
            if chunk:
                text_parts.append(chunk)

    text = " ".join(text_parts).strip()

    speech_id = "{sess}-M{mtg}-{item}-{sid}-{start}".format(
        sess=session,
        mtg=meeting_number,
        item=agenda_item_no or "x",
        sid=speaker_id or "anon",
        start=(start_time or "").replace(":", "").replace("-", "")[:15],
    )

    return Speech(
        speech_id=speech_id,
        session=session,
        meeting_number=meeting_number,
        meeting_date=meeting_date,
        agenda_item_no=agenda_item_no,
        agenda_short_title=agenda_short_title,
        case_type=case_type,
        case_number=case_number,
        speaker_first_name=first,
        speaker_last_name=last,
        speaker_role=role,
        party_short=party,
        speaker_title=speaker_title or None,
        speech_type=speech_type,
        start_time=start_time,
        end_time=end_time,
        text=text,
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

# Rough token estimate: Danish averages ~4 chars/token with sentencepiece-style
# tokenisers. We chunk on sentence boundaries and target a word count that lands
# in the 300-500 token range. 220-380 words ≈ 300-500 tokens for Danish prose.

TARGET_WORDS = 320
MAX_WORDS = 420
OVERLAP_SENTENCES = 1
# Procedural micro-speeches ("Ministeren.", "Værsgo.") carry no content but
# embed as near-generic vectors that pollute retrieval — drop anything shorter.
MIN_CHUNK_WORDS = 15

# The Folketinget transcripts frequently omit the space after sentence-ending
# punctuation ("Tak for det.Jeg bliver..."), so we first normalise: insert a
# space after .!? when preceded by a lowercase letter and followed by an
# uppercase letter. This preserves numeric refs ("§ 41, stk.4") and ordinals
# ("1.september") which have digits on at least one side.
_SENT_FIX_RE = re.compile(r"(?<=[a-zæøåA-ZÆØÅ])([.!?])(?=[A-ZÆØÅ])")
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÆØÅ])")


def _split_sentences(text: str) -> list[str]:
    text = _SENT_FIX_RE.sub(r"\1 ", text)
    sents = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    return sents or ([text] if text else [])


def chunk_speech(speech: Speech) -> list[Chunk]:
    sentences = _split_sentences(speech.text)
    if not sentences:
        return []

    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_words = 0
    idx = 0

    def flush() -> None:
        nonlocal buf, buf_words, idx
        if not buf:
            return
        text = " ".join(buf).strip()
        if text and len(text.split()) >= MIN_CHUNK_WORDS:
            chunks.append(
                Chunk(
                    chunk_id=f"{speech.speech_id}#{idx}",
                    speech_id=speech.speech_id,
                    chunk_index=idx,
                    text=text,
                    metadata=_chunk_metadata(speech),
                )
            )
            idx += 1
        # carry overlap
        carry = buf[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
        buf = list(carry)
        buf_words = sum(len(s.split()) for s in buf)

    for sent in sentences:
        words = len(sent.split())
        if buf_words + words > MAX_WORDS and buf:
            flush()
        buf.append(sent)
        buf_words += words
        if buf_words >= TARGET_WORDS:
            flush()
    flush()
    return chunks


def _chunk_metadata(s: Speech) -> dict:
    speaker = " ".join(p for p in (s.speaker_first_name, s.speaker_last_name) if p) or None
    return {
        "speech_id": s.speech_id,
        "session": s.session,
        "meeting_number": s.meeting_number,
        "meeting_date": s.meeting_date,
        "agenda_item_no": s.agenda_item_no,
        "agenda_short_title": s.agenda_short_title,
        "case_type": s.case_type,
        "case_number": s.case_number,
        "speaker": speaker,
        "speaker_role": s.speaker_role,
        "party_short": s.party_short,
        "speech_type": s.speech_type,
        "start_time": s.start_time,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def ingest_files(paths: Iterable[Path]) -> Iterator[Chunk]:
    for path in paths:
        log.info("Parsing %s", path.name)
        try:
            speeches = parse_xml_file(path)
        except etree.XMLSyntaxError as exc:
            log.warning("XML parse error in %s: %s", path.name, exc)
            continue
        log.info("  -> %d non-empty speeches", len(speeches))
        for sp in speeches:
            yield from chunk_speech(sp)


def write_chunks(chunks: Iterable[Chunk], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for ch in chunks:
            fh.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Folketinget transcripts.")
    ap.add_argument("--session", required=True, help="e.g. 20241")
    ap.add_argument("--max-files", type=int, default=None,
                    help="Limit number of XML files (for quick tests)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output JSONL path (default data/processed/<session>.jsonl)")
    ap.add_argument("--skip-download", action="store_true",
                    help="Use already-downloaded files in data/raw/")
    ap.add_argument("--sample", action="store_true",
                    help="Print 3 sample speeches and exit (no JSONL written)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.skip_download:
        paths = sorted(RAW_DIR.glob(f"{args.session}_M*_helemoedet.xml"))
        if args.max_files:
            paths = paths[: args.max_files]
        if not paths:
            raise SystemExit(f"No local files for session {args.session} in {RAW_DIR}")
    else:
        paths = download_session(args.session, max_files=args.max_files)

    if args.sample:
        # Parse just enough to print a sample
        for path in paths[:1]:
            speeches = parse_xml_file(path)
            print(f"\n=== {path.name}: {len(speeches)} speeches ===")
            for sp in speeches[:3]:
                print("---")
                print(f"  {sp.meeting_date} | M{sp.meeting_number} | item {sp.agenda_item_no}: "
                      f"{sp.agenda_short_title!r}")
                print(f"  speaker: {sp.speaker_first_name} {sp.speaker_last_name} "
                      f"({sp.party_short}, {sp.speaker_role})")
                print(f"  type: {sp.speech_type}  start: {sp.start_time}")
                print(f"  text[:300]: {sp.text[:300]}")
        return

    out = args.out or PROCESSED_DIR / f"{args.session}.jsonl"
    n = write_chunks(ingest_files(paths), out)
    log.info("Wrote %d chunks to %s", n, out)


if __name__ == "__main__":
    main()

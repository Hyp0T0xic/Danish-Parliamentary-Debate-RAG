"""
Minimal Streamlit UI for the Folketing RAG API.

Requires the API running locally:

    py -3 -m uvicorn src.api:app --port 8000

Then start the frontend from the project root:

    py -3 -m streamlit run frontend/app.py
"""
import requests
import streamlit as st

API_URL = "http://127.0.0.1:8000"

EXAMPLE_QUESTIONS = [
    "Hvad mener partierne om ulve i Danmark?",
    "Hvad er regeringens holdning til Kina og Taiwan?",
    "Hvad mener partierne om våbenhandel med Israel?",
    "Hvad blev der sagt om Danmarks rolle i adoptionssager?",
    "Hvad blev der diskuteret om euroforbeholdet?",
    "Skal der være aldersgrænse for solarier?",
]

st.set_page_config(page_title="Folketing RAG", page_icon="🏛️", layout="centered")

st.title("🏛️ Spørg Folketinget")
st.caption(
    "Stil et spørgsmål om folketingsdebatterne, og få et svar med kildehenvisninger "
    "til konkrete taler. Datagrundlag: 10 møder fra samling 2024-25 (maj-september 2025)."
)

# --- Sidebar: settings + corpus health ---------------------------------
with st.sidebar:
    st.header("Indstillinger")
    k = st.slider("Antal kilder (top-k)", min_value=2, max_value=12, value=6)
    party = st.text_input("Filtrér på parti (valgfrit)", placeholder="fx S, V, EL, DF")
    st.divider()
    try:
        health = requests.get(f"{API_URL}/health", timeout=5).json()
        st.success(f"API kører — {health['chunks']:,} tekststykker indekseret")
    except requests.RequestException:
        st.error(
            "API'en svarer ikke. Start den med:\n\n"
            "`py -3 -m uvicorn src.api:app --port 8000`"
        )

# --- Question input ------------------------------------------------------
question = st.text_input(
    "Dit spørgsmål",
    value=st.session_state.get("question", ""),
    placeholder="fx: Hvad mener partierne om ulve i Danmark?",
    key="question_box",
)

st.write("Eller prøv et eksempel:")
cols = st.columns(2)
for i, ex in enumerate(EXAMPLE_QUESTIONS):
    if cols[i % 2].button(ex, use_container_width=True):
        st.session_state["question"] = ex
        st.rerun()

ask_clicked = st.button("Spørg", type="primary", use_container_width=True)
active_question = question.strip() or st.session_state.get("question", "").strip()

# --- Ask + render --------------------------------------------------------
if ask_clicked and not active_question:
    st.warning("Skriv et spørgsmål først.")

if active_question and (ask_clicked or st.session_state.get("question")):
    st.session_state["question"] = ""  # consume example-button state
    with st.spinner("Søger i debatterne og formulerer svar ..."):
        try:
            resp = requests.post(
                f"{API_URL}/ask",
                json={
                    "question": active_question,
                    "k": k,
                    "party": party.strip().upper() or None,
                },
                timeout=120,
            )
        except requests.RequestException as exc:
            st.error(f"Kunne ikke nå API'en: {exc}")
            st.stop()

    if resp.status_code != 200:
        detail = resp.json().get("detail", resp.text)
        st.error(f"Fejl fra API'en ({resp.status_code}): {detail}")
        st.stop()

    data = resp.json()
    st.divider()
    st.markdown(f"**Spørgsmål:** {data['question']}")
    st.markdown(data["answer"])

    st.subheader("Kilder")
    for s in data["sources"]:
        label = (
            f"[{s['n']}] {s.get('speaker') or 'Ukendt taler'}"
            f" ({s.get('party') or '?'}) — {s.get('meeting_date', '?')}"
        )
        with st.expander(f"{label} · relevans {s['score']:.2f}"):
            if s.get("agenda"):
                st.caption(s["agenda"])
            st.write(s["text"])

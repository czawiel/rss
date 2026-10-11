# ==========================================
# Wersja: v2.5
# ==========================================
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
import html
import io
import re
import sqlite3
import time
import urllib.parse
from dateutil import parser as date_parser
import edge_tts
import feedparser
import requests
import streamlit as st
import trafilatura

st.set_page_config(
    page_title="Wiadomości - bądź na bieżąco",
    page_icon="🌍",
    layout="wide"
)

# Styl CSS powiększający główny przycisk w pasku bocznym
st.markdown("""
<style>
    div[data-testid="stSidebar"] div.stButton > button[kind="primary"] {
        font-size: 1.15rem !important;
        font-weight: 700 !important;
        padding-top: 0.75rem !important;
        padding-bottom: 0.75rem !important;
        border-radius: 8px !important;
    }
</style>
""", unsafe_allow_html=True)

# --- OBSŁUGA BAZY DANYCH SQLITE ---
DB_FILE = "news_saved.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                link TEXT PRIMARY KEY,
                source TEXT,
                scope TEXT,
                category TEXT,
                title TEXT,
                original_title TEXT,
                published TEXT,
                iso_timestamp TEXT,
                is_foreign INTEGER,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("PRAGMA table_info(bookmarks)")
        existing_cols = {col[1] for col in cursor.fetchall()}
        if "iso_timestamp" not in existing_cols:
            cursor.execute("ALTER TABLE bookmarks ADD COLUMN iso_timestamp TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        """)
        conn.commit()

init_db()

def get_saved_keywords() -> str:
    default_kw = "sztuczna inteligencja, AI, paliw, stopy procentowe"
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT setting_value FROM app_settings WHERE setting_key = 'hot_keywords'")
        row = cursor.fetchone()
        return row[0] if row else default_kw

def save_keywords(keywords_str: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO app_settings (setting_key, setting_value)
            VALUES ('hot_keywords', ?)
        """, (keywords_str,))
        conn.commit()

def get_saved_links() -> set[str]:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT link FROM bookmarks")
        return {row[0] for row in cursor.fetchall()}

def add_bookmark(art: dict):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO bookmarks 
            (link, source, scope, category, title, original_title, published, iso_timestamp, is_foreign)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            art["link"],
            art["source"],
            art["scope"],
            art["category"],
            art["title"],
            art.get("original_title"),
            art["published"],
            art.get("iso_timestamp"),
            1 if art.get("is_foreign") else 0
        ))
        conn.commit()

def remove_bookmark(link: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bookmarks WHERE link = ?", (link,))
        conn.commit()

def get_all_bookmarks() -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT link, source, scope, category, title, original_title, published, iso_timestamp, is_foreign 
            FROM bookmarks ORDER BY saved_at DESC
        """)
        rows = cursor.fetchall()
        bookmarks = []
        for r in rows:
            dt = None
            if r[7]:
                try:
                    dt = datetime.fromisoformat(r[7])
                except Exception:
                    pass
            bookmarks.append({
                "link": r[0],
                "source": r[1],
                "scope": r[2],
                "category": r[3],
                "title": r[4],
                "original_title": r[5],
                "published": r[6],
                "iso_timestamp": r[7],
                "dt": dt or datetime.min.replace(tzinfo=timezone.utc),
                "is_foreign": bool(r[8]),
                "content_len": len(r[4] or "")
            })
        return bookmarks


# --- NIEZAWODNY SYSTEM TŁUMACZEŃ ---

def _translate_via_google_api(text: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {"client": "gtx", "sl": "en", "tl": "pl", "dt": "t", "q": text}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36"}
    resp = requests.get(url, params=params, headers=headers, timeout=5)
    if resp.status_code == 200:
        data = resp.json()
        parts = [seg[0] for seg in data[0] if seg and seg[0]]
        res = "".join(parts).strip()
        if res:
            return html.unescape(res)
    return ""

def _translate_via_mymemory(text: str) -> str:
    encoded_text = urllib.parse.quote(text[:500])
    url = f"https://api.mymemory.translated.net/get?q={encoded_text}&langpair=en|pl"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=5)
    if resp.status_code == 200:
        data = resp.json()
        match = data.get("responseData", {}).get("translatedText", "")
        if match and match.strip() and not match.startswith("MYMEMORY WARNING"):
            return html.unescape(match.strip())
    return ""

def translate_to_pl(text: str) -> str:
    if not text or not text.strip():
        return text
    cleaned = text.strip()
    try:
        res = _translate_via_google_api(cleaned)
        if res and res.lower() != cleaned.lower():
            return res
    except Exception:
        pass

    try:
        res = _translate_via_mymemory(cleaned)
        if res and res.lower() != cleaned.lower():
            return res
    except Exception:
        pass

    return cleaned

def get_translated_source_url(url: str, is_foreign: bool) -> str:
    """Dla serwisów zagranicznych zwraca link przepuszczony przez tłumacza stron."""
    if is_foreign:
        safe_url = urllib.parse.quote(url, safe="")
        return f"https://translate.google.com/translate?sl=auto&tl=pl&u={safe_url}"
    return url


# --- SYNTEZA MOWY (TEXT-TO-SPEECH) ---

async def _generate_audio_stream(text: str, voice: str = "pl-PL-MarekNeural") -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    audio_buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_buffer.write(chunk["data"])
    return audio_buffer.getvalue()

@st.cache_data(show_spinner=False)
def generate_speech(text: str) -> bytes:
    sample_text = text[:3000].strip()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        audio_data = loop.run_until_complete(_generate_audio_stream(sample_text))
        loop.close()
        return audio_data
    except Exception:
        return b""


# --- OBSŁUGA CZASU I FORMATOWANIA DAT ---

def parse_datetime(raw_date_str: str, entry_parsed_tuple=None) -> tuple[datetime, str]:
    dt = None
    if raw_date_str:
        try:
            dt = date_parser.parse(raw_date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except Exception:
            pass

    if dt is None and entry_parsed_tuple:
        try:
            dt = datetime.fromtimestamp(time.mktime(entry_parsed_tuple), tz=timezone.utc)
        except Exception:
            pass

    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc), "Brak daty"

    now = datetime.now(timezone.utc)
    diff = now - dt
    total_seconds = int(diff.total_seconds())

    if total_seconds < 0:
        rel_str = "Przed chwilą"
    elif total_seconds < 60:
        rel_str = f"🟢 {total_seconds} s temu"
    elif total_seconds < 3600:
        rel_str = f"🟢 {total_seconds // 60} min temu"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        rel_str = f"🕒 {hours} godz. temu"
    else:
        days = total_seconds // 86400
        rel_str = "Wczoraj" if days == 1 else f"{days} dni temu"

    return dt, rel_str


# --- EKSTRAKCJA TREŚCI ARTYKUŁÓW ---

@st.cache_data(show_spinner=False)
def fetch_raw_content_length(url: str) -> int:
    """Pobiera i zlicza rzeczywistą liczbę znaków tekstu artykułu."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            if extracted:
                return len(extracted.strip())
    except Exception:
        pass
    return 0

@st.cache_data(show_spinner=False)
def fetch_article_content(url: str, is_foreign: bool = False) -> str:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return "Nie udało się pobrać treści artykułu. Portal może blokować pobieranie automatyczne."
        extracted = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if not extracted:
            return "Nie udało się wyodrębnić treści. Kliknij 'Pokaż źródło', aby otworzyć artykuł w serwisie."

        if is_foreign:
            paragraphs = [p.strip() for p in extracted.split("\n\n") if len(p.strip()) > 30][:5]
            translated_paragraphs = []
            for p in paragraphs:
                tr = translate_to_pl(p[:450])
                translated_paragraphs.append(tr)
                time.sleep(0.08)
            return "\n\n".join(translated_paragraphs)

        return extracted
    except Exception as e:
        return f"Błąd podczas pobierania treści: {e}"


# --- ALGORYTM GRUPOWANIA TEMATYCZNEGO I DEDUPLIKACJI ---

STOP_WORDS = {
    "i", "w", "o", "z", "na", "do", "po", "ze", "za", "od", "dla", "się", "to", "jak", "nie",
    "jest", "a", "oraz", "że", "ma", "mają", "przez", "pod", "co", "ten", "ta", "te", "tym"
}

def _extract_keywords(title: str) -> set[str]:
    words = re.findall(r"\b[a-ząćęłńóśźż]{3,}\b", title.lower())
    return {w for w in words if w not in STOP_WORDS}

def are_same_topic(t1: str, t2: str) -> bool:
    kw1 = _extract_keywords(t1)
    kw2 = _extract_keywords(t2)
    common = kw1.intersection(kw2)

    if len(common) >= 3:
        return True
    if len(common) >= 2 and (len(common) / max(len(kw1), len(kw2), 1)) >= 0.4:
        return True

    similarity = SequenceMatcher(None, t1.lower(), t2.lower()).ratio()
    return similarity >= 0.55

def deduplicate_topics(articles: list[dict]) -> list[dict]:
    """Grupuje wiadomości na ten sam temat i wybiera artykuł o najdłuższej treści."""
    clusters = []

    for art in articles:
        found_cluster = False
        for cluster in clusters:
            if any(are_same_topic(art["title"], existing["title"]) for existing in cluster):
                cluster.append(art)
                found_cluster = True
                break
        if not found_cluster:
            clusters.append([art])

    selected_articles = []
    for cluster in clusters:
        if len(cluster) == 1:
            best_art = dict(cluster[0])
            best_art["duplicate_count"] = 0
            selected_articles.append(best_art)
        else:
            # Gdy jest duplikat na ten sam temat, pobieramy faktyczną długość tekstu
            for c_art in cluster:
                if c_art.get("content_len", 0) <= 200:
                    real_len = fetch_raw_content_length(c_art["link"])
                    c_art["content_len"] = real_len if real_len > 0 else c_art.get("content_len", 0)

            best_art = max(cluster, key=lambda a: a.get("content_len", 0))
            best_art = dict(best_art)
            best_art["duplicate_count"] = len(cluster) - 1
            selected_articles.append(best_art)

    return selected_articles


# --- WYSKAKUJĄCE OKNO (DIALOG) Z ODSŁUCHIWANIEM ---

@st.dialog("📖 Treść artykułu", width="large")
def show_article_popup(article: dict):
    saved_links = get_saved_links()
    is_saved = article["link"] in saved_links

    st.subheader(article["title"])
    st.caption(f"Źródło: **{article['source']}** | Kategoria: `{article['category']}` | Opublikowano: **{article['published']}**")

    with st.spinner("Pobieranie i przygotowywanie tekstu..."):
        content = fetch_article_content(article["link"], article.get("is_foreign", False))

    st.markdown("---")
    st.write(content)
    st.markdown("---")

    col_audio_btn, col_audio_player = st.columns([1, 2])
    with col_audio_btn:
        generate_audio = st.button("🔊 Odsłuchaj artykuł", use_container_width=True)

    if generate_audio or st.session_state.get(f"audio_ready_{article['link']}"):
        st.session_state[f"audio_ready_{article['link']}"] = True
        with st.spinner("Generowanie lektora w języku polskim..."):
            tts_text = f"{article['title']}. {content}"
            audio_bytes = generate_speech(tts_text)

        if audio_bytes:
            st.audio(audio_bytes, format="audio/mp3")
        else:
            st.error("Nie udało się wygenerować dźwięku lektora.")

    st.markdown("---")

    col_src, col_save = st.columns([1, 1])
    with col_src:
        source_url = get_translated_source_url(article["link"], article.get("is_foreign", False))
        label_btn = "🔗 Źródło (po polsku) ↗" if article.get("is_foreign") else "🔗 Pokaż źródło ↗"
        st.link_button(label_btn, source_url, use_container_width=True)

    with col_save:
        if is_saved:
            if st.button(
                "🌟 Zapisano w ulubionych (kliknij, aby usunąć)", 
                key=f"modal_toggle_{article['link']}", 
                type="primary", 
                use_container_width=True
            ):
                remove_bookmark(article["link"])
                st.rerun()
        else:
            if st.button(
                "☆ Zapisz do ulubionych", 
                key=f"modal_toggle_{article['link']}", 
                type="secondary", 
                use_container_width=True
            ):
                add_bookmark(article)
                st.rerun()


# --- KANAŁY RSS ---

FEEDS = {
    # Wiadomości Kraj
    "TVN24 - Polska": {"url": "https://tvn24.pl/polska.xml", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "RMF24 - Polska": {"url": "https://www.rmf24.pl/fakty/polska/feed", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Polsat News - Kraj": {"url": "https://www.polsatnews.pl/rss/polska.xml", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Interia - Fakty": {"url": "https://fakty.interia.pl/feed", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Onet - Wiadomości": {"url": "https://wiadomosci.onet.pl/.feed", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Gazeta.pl - Kraj": {"url": "https://wiadomosci.gazeta.pl/pub/rss/kraj.htm", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Polskie Radio 24": {"url": "https://polskieradio24.pl/rss", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    "Dziennik.pl - Kraj": {"url": "https://wiadomosci.dziennik.pl/rss.xml", "scope": "Kraj", "category": "Wiadomości", "is_foreign": False},
    
    # Biznes i Gospodarka
    "Bankier - Wiadomości": {"url": "https://www.bankier.pl/rss/wiadomosci.xml", "scope": "Kraj", "category": "Biznes i Gospodarka", "is_foreign": False},
    "Money.pl": {"url": "https://www.money.pl/rss/", "scope": "Kraj", "category": "Biznes i Gospodarka", "is_foreign": False},
    "Puls Biznesu": {"url": "https://www.pb.pl/rss/", "scope": "Kraj", "category": "Biznes i Gospodarka", "is_foreign": False},

    # Technologia i IT
    "Dobreprogramy": {"url": "https://www.dobreprogramy.pl/rss/aktualnosci.xml", "scope": "Kraj", "category": "Technologia i IT", "is_foreign": False},
    "SpidersWeb": {"url": "https://spidersweb.pl/feed", "scope": "Kraj", "category": "Technologia i IT", "is_foreign": False},
    "Niebezpiecznik": {"url": "https://niebezpiecznik.pl/feed/", "scope": "Kraj", "category": "Technologia i IT", "is_foreign": False},
    
    # Motoryzacja
    "Autokult": {"url": "https://autokult.pl/rss.xml", "scope": "Kraj", "category": "Motoryzacja", "is_foreign": False},

    # Świat
    "TVN24 - Świat": {"url": "https://tvn24.pl/swiat.xml", "scope": "Świat", "category": "Wiadomości", "is_foreign": False},
    "RMF24 - Świat": {"url": "https://www.rmf24.pl/fakty/swiat/feed", "scope": "Świat", "category": "Wiadomości", "is_foreign": False},
    "Polsat News - Świat": {"url": "https://www.polsatnews.pl/rss/swiat.xml", "scope": "Świat", "category": "Wiadomości", "is_foreign": False},
    "Interia - Świat": {"url": "https://swiat.interia.pl/feed", "scope": "Świat", "category": "Wiadomości", "is_foreign": False},
    "BBC News - World": {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "scope": "Świat", "category": "Wiadomości", "is_foreign": True},
    "Bankier - Rynki": {"url": "https://www.bankier.pl/rss/rynki.xml", "scope": "Świat", "category": "Biznes i Gospodarka", "is_foreign": False},
    "TechCrunch": {"url": "https://techcrunch.com/feed/", "scope": "Świat", "category": "Technologia i IT", "is_foreign": True},
    "The Verge": {"url": "https://www.theverge.com/rss/index.xml", "scope": "Świat", "category": "Technologia i IT", "is_foreign": True},
    "Ars Technica": {"url": "https://feeds.arstechnica.com/arstechnica/index", "scope": "Świat", "category": "Technologia i IT", "is_foreign": True},
}


def fetch_feed(name: str, meta: dict, limit: int) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    articles = []
    try:
        resp = requests.get(meta["url"], headers=headers, timeout=4)
        if resp.status_code == 200:
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[:limit]:
                raw_title = entry.get("title", "").strip()
                if not raw_title:
                    continue

                raw_date = entry.get("published", entry.get("updated", ""))
                parsed_tuple = entry.get("published_parsed", entry.get("updated_parsed"))
                dt_obj, rel_date_str = parse_datetime(raw_date, parsed_tuple)

                summary_raw = entry.get("summary", entry.get("description", ""))
                clean_summary = re.sub(r"<[^>]*>", "", summary_raw).strip()
                content_len = len(clean_summary) if clean_summary else len(raw_title)

                articles.append({
                    "source": name,
                    "scope": meta["scope"],
                    "category": meta["category"],
                    "is_foreign": meta.get("is_foreign", False),
                    "raw_title": raw_title,
                    "link": entry.get("link", "#"),
                    "published": rel_date_str,
                    "iso_timestamp": dt_obj.isoformat(),
                    "dt": dt_obj,
                    "content_len": content_len
                })
    except Exception:
        pass
    return articles


@st.cache_data(ttl=180, show_spinner=False)
def get_all_articles(sources_keys: tuple, limit: int) -> list[dict]:
    all_articles = []
    with ThreadPoolExecutor(max_workers=14) as executor:
        futures = [
            executor.submit(fetch_feed, name, FEEDS[name], limit)
            for name in sources_keys if name in FEEDS
        ]
        for f in futures:
            all_articles.extend(f.result())

    for art in all_articles:
        if art.get("is_foreign"):
            art["title"] = translate_to_pl(art["raw_title"])
            art["original_title"] = art["raw_title"]
        else:
            art["title"] = art["raw_title"]
            art["original_title"] = None

    all_articles.sort(key=lambda x: x["dt"], reverse=True)
    return all_articles


def render_grid(articles: list[dict], section_id: str, is_saved_tab: bool = False):
    if not articles:
        if is_saved_tab:
            st.info("Nie masz jeszcze żadnych zapisanych artykułów. Kliknij '☆ Zapisz' na kafelku lub w oknie artykułu.")
        elif section_id == "hot":
            st.info("Brak wiadomości pasujących do Twoich słów-kluczy w sekcji Gorące tematy.")
        else:
            st.info("Brak artykułów w tej sekcji.")
        return

    saved_links = get_saved_links()
    cols = st.columns(2)

    for i, art in enumerate(articles):
        with cols[i % 2]:
            with st.container(border=True):
                is_bookmarked = art['link'] in saved_links

                matched_keyword = art.get("matched_keyword")
                badge_alert = f" | 🔥 **Alert: {matched_keyword}**" if matched_keyword else ""
                
                dup_count = art.get("duplicate_count", 0)
                badge_dup = f" | 👥 `+{dup_count} inne źródła`" if dup_count > 0 else ""

                if is_bookmarked:
                    st.caption(f"🌟 **ZAPISANE** | 📌 **{art['source']}** | `{art['category']}`{badge_alert}{badge_dup} | {art['published']}")
                else:
                    st.caption(f"📌 **{art['source']}** | `{art['category']}`{badge_alert}{badge_dup} | {art['published']}")

                st.subheader(art['title'])

                if art.get("original_title"):
                    st.caption(f"🇬🇧 Oryginał: *{art['original_title']}*")

                c1, c2, c3 = st.columns([1, 1, 1])

                with c1:
                    if st.button("📖 Czytaj", key=f"read_{section_id}_{i}_{art['link']}", use_container_width=True):
                        show_article_popup(art)

                with c2:
                    # Automatyczny link tłumaczący całą stronę dla serwisów zagranicznych
                    source_url = get_translated_source_url(art["link"], art.get("is_foreign", False))
                    btn_label = "🔗 Źródło (PL) ↗" if art.get("is_foreign") else "🔗 Źródło ↗"
                    st.link_button(btn_label, source_url, use_container_width=True)

                with c3:
                    if is_saved_tab:
                        if st.button("🗑️ Usuń", key=f"del_{section_id}_{i}_{art['link']}", use_container_width=True):
                            remove_bookmark(art['link'])
                            st.rerun()
                    else:
                        if is_bookmarked:
                            if st.button(
                                "🌟 Zapisano", 
                                key=f"toggle_saved_{section_id}_{i}_{art['link']}", 
                                type="primary", 
                                use_container_width=True,
                                help="Kliknij, aby cofnąć zapis"
                            ):
                                remove_bookmark(art['link'])
                                st.rerun()
                        else:
                            if st.button(
                                "☆ Zapisz", 
                                key=f"toggle_save_{section_id}_{i}_{art['link']}", 
                                type="secondary", 
                                use_container_width=True
                            ):
                                add_bookmark(art)
                                st.rerun()


# --- INTERFEJS GŁÓWNY ---

st.title("🌐 Wiadomości - bądź na bieżąco")

with st.sidebar:
    st.header("⚙️ Ustawienia")

    # 1. Główny przycisk odświeżania na samej górze
    if st.button("🔄 Odśwież", type="primary", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")

    # 2. Opcja deduplikacji tematów z wyborem najdłuższej treści
    dedup_enabled = st.checkbox(
        "Grupuj podobne wiadomości (1 na temat)",
        value=True,
        help="Wybiera artykuł o największej liczbie znaków spośród powtarzających się tematów."
    )

    # 3. Zasięg geograficzny
    selected_scopes = st.multiselect(
        "Zasięg:", 
        options=["Kraj", "Świat"], 
        default=["Kraj", "Świat"]
    )

    # 4. Wspólne kategorie
    all_categories = sorted(list({v["category"] for v in FEEDS.values()}))
    selected_categories = st.multiselect(
        "Wspólne kategorie:",
        options=all_categories,
        default=all_categories
    )

    # 5. Dynamiczne filtrowanie źródeł
    matching_sources = [
        k for k, v in FEEDS.items() 
        if v["scope"] in selected_scopes and v["category"] in selected_categories
    ]

    selected_sources = st.multiselect(
        "Źródła:", 
        options=matching_sources, 
        default=matching_sources
    )

    news_limit = st.slider("Liczba newsów z portalu:", 1, 8, 3)

    st.markdown("---")
    st.subheader("🔥 Śledzone tematy (Alerty)")

    stored_keywords = get_saved_keywords()

    def update_keywords():
        new_val = st.session_state.keywords_input_area
        save_keywords(new_val)

    raw_keywords = st.text_area(
        "Słowa-klucze (rozdzielaj przecinkami):",
        value=stored_keywords,
        key="keywords_input_area",
        on_change=update_keywords,
        help="Frazy zapisują się w bazie i pozostają po odświeżeniu strony."
    )
    
    keywords_list = [k.strip().lower() for k in raw_keywords.split(",") if k.strip()]

search_query = st.text_input("🔍 Filtruj po tytule:", "")

saved_articles = get_all_bookmarks()

if selected_sources:
    with st.spinner("Pobieranie, tłumaczenie i sortowanie wiadomości..."):
        articles = get_all_articles(tuple(selected_sources), news_limit)

    if search_query:
        articles = [
            a for a in articles 
            if search_query.lower() in a["title"].lower() 
            or (a.get("original_title") and search_query.lower() in a["original_title"].lower())
        ]
        saved_articles_filtered = [
            a for a in saved_articles
            if search_query.lower() in a["title"].lower() 
            or (a.get("original_title") and search_query.lower() in a["original_title"].lower())
        ]
    else:
        saved_articles_filtered = saved_articles

    saved_articles_filtered = [
        a for a in saved_articles_filtered 
        if a["category"] in selected_categories
    ]

    # Zastosowanie deduplikacji tematycznej z wyborem najdłuższej treści
    if dedup_enabled:
        articles = deduplicate_topics(articles)

    hot_articles = []
    for art in articles:
        matched = None
        for kw in keywords_list:
            if kw in art["title"].lower() or (art.get("original_title") and kw in art["original_title"].lower()):
                matched = kw
                break
        if matched:
            art_copy = dict(art)
            art_copy["matched_keyword"] = matched
            hot_articles.append(art_copy)

    kraj = [a for a in articles if a["scope"] == "Kraj"]
    swiat = [a for a in articles if a["scope"] == "Świat"]

    tab_all, tab_kraj, tab_swiat, tab_hot, tab_saved = st.tabs([
        f"📑 Wszystkie ({len(articles)})",
        f"🇵🇱 Kraj ({len(kraj)})", 
        f"🌍 Świat ({len(swiat)})", 
        f"🔥 Gorące tematy ({len(hot_articles)})",
        f"⭐ Zapisane ({len(saved_articles_filtered)})"
    ])

    with tab_all:
        render_grid(articles, "all")

    with tab_kraj:
        render_grid(kraj, "kraj")

    with tab_swiat:
        render_grid(swiat, "swiat")

    with tab_hot:
        if keywords_list:
            st.caption(f"Trwale aktywne alerty: {', '.join([f'`{k}`' for k in keywords_list])}")
        render_grid(hot_articles, "hot")

    with tab_saved:
        render_grid(saved_articles_filtered, "saved", is_saved_tab=True)
else:
    st.warning("Zaznacz co najmniej jedno źródło w panelu bocznym.")
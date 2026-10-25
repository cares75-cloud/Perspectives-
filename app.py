import os, re, time, hashlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import feedparser
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

FEEDS = [
    {"name":"3Cat","category":"Catalunya","url":"https://www.3cat.cat/3catinfo/rss/"},
    {"name":"Govern de la Generalitat","category":"Catalunya","url":"https://govern.cat/gov/notes-premsa/rss"},
    {"name":"BBC News","category":"Món","url":"https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name":"The Guardian – World","category":"Món","url":"https://www.theguardian.com/world/rss"},
    {"name":"Google News – Reuters","category":"Espanya i món","url":"https://news.google.com/rss/search?q=site%3Areuters.com&hl=ca&gl=ES&ceid=ES%3Aca"},
    {"name":"Google News – Associated Press","category":"Món","url":"https://news.google.com/rss/search?q=site%3Aapnews.com&hl=ca&gl=ES&ceid=ES%3Aca"},
]
CACHE = {"at": 0, "items": [], "errors": []}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "900"))


def clean_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def entry_date(entry):
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc) - timedelta(days=2)


def normalize_title(title):
    title = (title or "").lower()
    title = re.sub(r"[^\w\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def load_news(force=False):
    now = time.time()
    if not force and CACHE["items"] and now - CACHE["at"] < CACHE_TTL:
        return CACHE["items"], CACHE["errors"]
    items, errors = [], []
    headers = {"User-Agent": "PerspectivesNews/1.0 (RSS reader; contact: admin@example.com)"}
    for feed in FEEDS:
        try:
            response = requests.get(feed["url"], headers=headers, timeout=10)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError("El canal RSS no s'ha pogut interpretar")
            for entry in parsed.entries[:35]:
                link = entry.get("link", "")
                title = clean_html(entry.get("title", ""))
                if not title or not link.startswith(("http://", "https://")):
                    continue
                published = entry_date(entry)
                summary = clean_html(entry.get("summary", entry.get("description", "")))
                if len(summary) > 600: summary = summary[:597].rsplit(" ", 1)[0] + "…"
                items.append({
                    "id": hashlib.sha1((normalize_title(title) + link).encode()).hexdigest()[:14],
                    "title": title, "url": link, "source": feed["name"],
                    "category": feed["category"], "published": published.isoformat(),
                    "summary": summary,
                })
        except Exception as exc:
            errors.append({"source": feed["name"], "message": str(exc)[:180]})
    # Deduplicate exact/near-identical headlines while preserving the first source.
    unique, seen = [], set()
    for item in sorted(items, key=lambda x: x["published"], reverse=True):
        key = normalize_title(item["title"])
        words = set(key.split())
        duplicate = False
        for old_key in list(seen)[-250:]:
            old_words = set(old_key.split())
            if key == old_key or (len(words) >= 5 and len(old_words) >= 5 and len(words & old_words) / max(1, len(words | old_words)) > 0.82):
                duplicate = True; break
        if not duplicate:
            unique.append(item); seen.add(key)
    CACHE.update({"at": now, "items": unique, "errors": errors})
    return unique, errors


def summarize(items):
    key = os.getenv("OPENAI_API_KEY")
    if key and items:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key)
            source_text = "\n".join(f"- [{x['category']}] {x['title']} ({x['source']}): {x['summary']} URL: {x['url']}" for x in items[:35])
            result = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                temperature=0.2,
                messages=[
                    {"role":"system", "content":"Ets editor d'un briefing informatiu en català. No inventis dades. Distingeix fets verificables, afirmacions atribuïdes, interpretacions i incerteses. Agrupa temes coincidents. Prioritza rellevància per a Catalunya. Escriu HTML simple amb h3, p i ul/li; inclou enllaços només als URLs proporcionats. Indica si les fonts discrepen. No presentis una notícia RSS com a verificada independentment."},
                    {"role":"user", "content":"Prepara un resum diari a partir d'aquestes notícies recents. Estructura: 'Panorama', 'Fets i dades', 'Afirmacions i interpretacions', 'Què no sabem', 'Implicacions per a Catalunya' i 'Tres temes a seguir'. Material:\n" + source_text}
                ],
            )
            return {"html": result.choices[0].message.content, "mode":"AI"}
        except Exception:
            pass
    grouped = {}
    for item in items[:18]: grouped.setdefault(item["category"], []).append(item)
    bits = ["<p>Aquest resum automàtic ordena titulars i extractes de canals RSS. No és una verificació independent dels fets.</p>"]
    for category, entries in grouped.items():
        bits.append(f"<h3>{category}</h3><ul>")
        for x in entries[:5]:
            bits.append(f'<li><a href="{x["url"]}" target="_blank" rel="noopener noreferrer">{x["title"]}</a> <span>— {x["source"]}</span>' + (f'<p>{x["summary"]}</p>' if x["summary"] else "") + "</li>")
        bits.append("</ul>")
    bits.append("<h3>Què cal comprovar</h3><p>Contrasta les notícies rellevants amb documents originals, dades públiques i fonts independents. Els titulars o extractes, per si sols, no permeten separar sempre fets confirmats d'afirmacions.</p>")
    return {"html":"".join(bits), "mode":"RSS"}


@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/news")
def api_news():
    items, errors = load_news(force=request.args.get("refresh") == "1")
    period = request.args.get("period", "24h")
    hours = {"24h":24, "72h":72, "7d":168}.get(period, 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = [x for x in items if datetime.fromisoformat(x["published"]) >= cutoff]
    category = request.args.get("category", "Totes")
    if category != "Totes": filtered = [x for x in filtered if x["category"] == category]
    return jsonify({"items":filtered, "errors":errors, "updated_at":datetime.fromtimestamp(CACHE["at"], timezone.utc).isoformat() if CACHE["at"] else None, "period":period, "sources":len(FEEDS)})

@app.get("/api/briefing")
def api_briefing():
    items, errors = load_news(force=request.args.get("refresh") == "1")
    period = request.args.get("period", "24h")
    hours = {"24h":24, "72h":72, "7d":168}.get(period, 24)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered = [x for x in items if datetime.fromisoformat(x["published"]) >= cutoff]
    result = summarize(filtered)
    result.update({"count":len(filtered), "errors":errors, "updated_at":datetime.fromtimestamp(CACHE["at"], timezone.utc).isoformat() if CACHE["at"] else None})
    return jsonify(result)

@app.get("/health")
def health(): return jsonify({"status":"ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

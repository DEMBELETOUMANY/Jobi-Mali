"""
Robot Jobi Mali : collecte -> reformulation par IA -> base SQLite -> annonces.json
Lancement quotidien : python robot.py
Variable requise : ANTHROPIC_API_KEY
"""
import os, re, json, hashlib, sqlite3, datetime, time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import feedparser
from bs4 import BeautifulSoup
import anthropic

# ---------- CONFIGURATION ----------


SOURCES = [
    {"name": "ORTM", "url": "https://www.ortm.ml/"},
    {"name": "L'Essor", "url": "https://lessor.ml/"},
    {"name": "Malijet", "url": "https://malijet.com/"},
    {"name": "Maliweb", "url": "https://www.maliweb.net/"},
    {"name": "Journal du Mali", "url": "https://journaldumali.com/"},
    {"name": "Afrique sur 7", "url": "https://www.afrique-sur7.fr/category/bamako"},
    {"name": "Bamada", "url": "https://bamada.net/"},
    {"name": "Ici Mali", "url": "https://icimali.com/"},
    {"name": "Bamako Matin", "url": "https://bamakomatin.com/"},
    {"name": "Ministère de l'Économie et des Finances", "url": "https://www.finances.ml/"},
]
# Le robot trouve tout seul le flux RSS de chaque site (sinon il lit les liens de la page).
# Il ne garde que les pages qui parlent de recrutement, d'emploi, d'appel d'offres, etc.
KEYWORDS = ("recrut", "emploi", "offre", "appel", "avis", "concours", "consultant",
            "poste", "marche", "marché", "manifestation", "candidature", "vacance")

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
DB = "annonces.db"
OUT = "annonces.json"
MAX_PER_SOURCE = 25
KEEP_DAYS = 60
UA = {"User-Agent": "JobiMaliBot/1.0 (contact: votre@email.com)"}

PROMPT = """Tu es un éditeur d'annonces pour un site d’emploi Jobi Mali, au Mali et en Afrique de l’Ouest.
À partir du texte ci-dessous, réponds UNIQUEMENT par un objet JSON (sans Markdown).
Si le texte n'est pas une offre d'emploi, une mission rémunérée ou un appel d'offres, réponds {"keep": false}.
Sinon :
{"keep": true,
 "kind": "job" | "gig" | "ao",   // emploi, petit boulot/mission, appel d'offres
 "title": "titre clair et court, sans majuscules excessives",
 "org": "organisation ou \"Non précisé\"",
 "city": "ville ou \"Non précisé\"",
 "country": "pays",
 "type": "CDI, CDD, Freelance, Stage, Mission, Appel d'offres...",
 "pay": "salaire/rémunération si indiqué, sinon pour un appel d'offres \"Date limite : ...\", sinon \"Non précisé\"",
 "summary": "2 à 3 phrases entièrement reformulées avec tes propres mots : poste, profil, comment postuler"}
N'invente rien : si une information manque, écris "Non précisé"."""

client = anthropic.Anthropic()


def allowed(url):
    """Respecte le robots.txt du site."""
    try:
        p = urlparse(url)
        rp = RobotFileParser(f"{p.scheme}://{p.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(UA["User-Agent"], url)
    except Exception:
        return False


def get(url):
    if not allowed(url):
        print("  bloqué par robots.txt :", url)
        return None
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text


def interesting(text):
    t = text.lower()
    return any(k in t for k in KEYWORDS)


def find_feed(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for l in soup.find_all("link", rel="alternate"):
        kind = (l.get("type") or "").lower()
        if ("rss" in kind or "atom" in kind) and l.get("href"):
            return urljoin(base, l["href"])
    return None


def links_for(src):
    html = get(src["url"])
    if not html:
        return []
    out = []
    feed_url = src.get("feed") or find_feed(html, src["url"])
    if feed_url and allowed(feed_url):
        for e in feedparser.parse(feed_url).entries:
            if interesting(e.get("title", "") + " " + e.link):
                out.append(e.link)
    if not out:  # pas de flux : on lit les liens de la page
        soup = BeautifulSoup(html, "html.parser")
        host = urlparse(src["url"]).netloc
        for a in soup.find_all("a", href=True):
            u = urljoin(src["url"], a["href"])
            if urlparse(u).netloc == host and interesting(a.get_text(" ") + " " + u):
                out.append(u)
    return list(dict.fromkeys(out))[:MAX_PER_SOURCE]


def page_text(url):
    html = get(url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "aside"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:6000]


def rewrite(text):
    msg = client.messages.create(
        model=MODEL, max_tokens=700,
        system=PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    return json.loads(raw)


def main():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS ads(
        id TEXT PRIMARY KEY, url TEXT, source TEXT, added TEXT, data TEXT)""")
    today = datetime.date.today().isoformat()
    new = 0
    for src in SOURCES:
        print("Source :", src["name"])
        try:
            urls = links_for(src)
        except Exception as e:
            print("  erreur source :", e)
            continue
        for url in urls:
            uid = hashlib.sha1(url.encode()).hexdigest()
            if db.execute("SELECT 1 FROM ads WHERE id=?", (uid,)).fetchone():
                continue  # déjà traitée
            try:
                text = page_text(url)
                if len(text) < 200:
                    continue
                if not interesting(text[:2500]):
                    db.execute("INSERT INTO ads VALUES(?,?,?,?,?)",
                               (uid, url, src["name"], today, "null"))
                    continue
                ad = rewrite(text)
                if not ad.get("keep"):
                    db.execute("INSERT INTO ads VALUES(?,?,?,?,?)",
                               (uid, url, src["name"], today, "null"))
                    continue
                db.execute("INSERT INTO ads VALUES(?,?,?,?,?)",
                           (uid, url, src["name"], today, json.dumps(ad, ensure_ascii=False)))
                db.commit()
                new += 1
                print("  +", ad["title"])
                time.sleep(1)  # politesse envers les sites
            except Exception as e:
                print("  erreur annonce :", url, e)
    export(db)
    print(f"Terminé : {new} nouvelles annonces.")


def export(db):
    limit = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    rows = db.execute(
        "SELECT url, source, added, data FROM ads WHERE data!='null' AND added>=? ORDER BY added DESC",
        (limit,)).fetchall()
    out = []
    for url, source, added, data in rows:
        a = json.loads(data)
        out.append({
            "k": a["kind"], "t": a["title"], "o": a["org"], "v": a["city"],
            "p": a["country"], "x": a["type"], "pay": a["pay"], "s": source,
            "d": added, "u": url, "r": a.get("summary", ""),
        })
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

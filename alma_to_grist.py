#!/usr/bin/env python3
"""
Lit les notes de version Alma (Clarivate) et envoie les nouveautés (« Features »)
dans une table Grist.

- Une ligne par nouveauté.
- Si la ligne existe déjà (même colonne Cle), seules les colonnes remplies par
  le script sont mises à jour. Tes colonnes (Statut, Concerne, etc.) ne sont
  jamais touchées.

Variables d'environnement :
  GRIST_API_KEY   clé API Grist (obligatoire, sauf avec --dry-run)
  GRIST_DOC_ID    identifiant du document Grist (obligatoire, sauf avec --dry-run)
  GRIST_SERVER    par défaut https://grist.numerique.gouv.fr
  GRIST_TABLE     par défaut Mises_a_jour

Exemples :
  python alma_to_grist.py --dry-run          # affiche ce qui serait envoyé
  python alma_to_grist.py                    # envoie les 2 derniers mois
  python alma_to_grist.py --all              # envoie tous les mois de l'année
"""
import argparse
import datetime
import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

INDEX_URL = "https://knowledge.ag-software.clarivate.com/alma/en-us/Release_Notes/{year}/{year}.html"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; alma-grist-sync/1.0)"}

# Domaines utilisés par Alma dans les notes de version.
DOMAINS = [
    "Fulfillment and Resource Sharing",
    "Metadata & Linked Open Data",
    "Artificial Intelligence",
    "Resource Management",
    "Acquisitions",
    "Collaborative Networks",
    "User Experience, Accessibility & Infrastructure",
    "Analytics and Insights",
]

# Mots repérés dans la description pour remplir la colonne « Attention ».
ATTENTION_RULES = [
    (r"\bby default\b|\bturned on\b|\benabled by default\b", "Comportement par défaut"),
    (r"opt[\s-]?out", "Désactivation possible (opt-out)"),
    (r"opt[\s-]?in", "Activation volontaire (opt-in)"),
    (r"\bparameter\b|\bparameters\b", "Paramètre à configurer"),
    (r"contact (clarivate )?support|ask(ing)? (clarivate )?support|support to enable", "Demande au support"),
    (r"\bprivilege|\broles?\b", "Droits ou rôles"),
    (r"\bremoved\b|\bno longer\b|\brenamed\b|\bdeprecated\b|\bchanged\b", "Suppression ou changement"),
    (r"network zone|consorti", "Réseau / consortium"),
]


def norm(s):
    return re.sub(r"[^a-z]", "", s.lower().replace("&", "and"))


def clean(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")


def find_month_pages(year):
    """Retourne {numéro_de_mois: url} à partir du sommaire de l'année."""
    index = INDEX_URL.format(year=year)
    soup = get_soup(index)
    pages = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(index, a["href"]).split("#")[0]
        m = re.search(r"/Release_Notes/%d/([A-Za-z]+)_%d\.html?$" % (year, year), url)
        if m and m.group(1) in MONTHS:
            pages[MONTHS.index(m.group(1)) + 1] = url
    return pages


def find_anchor(h):
    """Cherche une ancre associée au titre."""
    if h.get("id"):
        return h["id"]
    a = h.find("a", attrs={"name": True}) or h.find("a", attrs={"id": True})
    if a:
        return a.get("name") or a.get("id")
    prev = h.find_previous_sibling()
    if prev is not None and prev.name == "a":
        return prev.get("name") or prev.get("id")
    return None


def text_of(el):
    for br in el.find_all("br"):
        br.replace_with("\n")
    return clean(el.get_text())


def is_meta(text):
    """Premier paragraphe qui ne contient que domaine / pictos / numéros URM."""
    if len(text) > 250:
        return False
    if re.search(r"URM-\d+", text):
        return True
    return len(text) < 120 and any(norm(d) in norm(text) for d in DOMAINS)


def attention_labels(title, description):
    haystack = (title + "\n" + description).lower()
    return [label for pattern, label in ATTENTION_RULES if re.search(pattern, haystack)]


def parse_features(soup, page_url, mois):
    main = (soup.select_one("#mc-main-content") or soup.find(attrs={"role": "main"})
            or soup.find("main") or soup.body or soup)
    features = []
    in_features = False
    current = None

    for el in main.find_all(["h2", "h3", "p", "ul", "ol"]):
        if el.name == "h2":
            in_features = clean(el.get_text()).lower() == "features"
            current = None
            continue
        if not in_features:
            continue
        if el.name == "h3":
            current = {"title": clean(el.get_text()), "anchor": find_anchor(el),
                       "blocks": [], "docs": []}
            features.append(current)
            continue
        if current is None:
            continue
        if el.find_parent(["ul", "ol", "table", "li"]) is not None:
            continue  # élément imbriqué : déjà inclus dans son parent

        if el.name == "p":
            text = text_of(el)
            if not text:
                continue
            if text.lower().startswith("for more information"):
                for a in el.find_all("a", href=True):
                    link = urljoin(page_url, a["href"])
                    if link not in current["docs"]:
                        current["docs"].append(link)
                continue
            current["blocks"].append(text)
        else:  # ul / ol
            lines = []
            for li in el.find_all("li", recursive=False):
                t = text_of(li)
                if t:
                    lines.append("- " + t.replace("\n", " "))
            if lines:
                current["blocks"].append("\n".join(lines))

    records = []
    seen = {}
    for f in features:
        if not f["title"]:
            continue
        blocks = list(f["blocks"])
        domain, tickets = "", []
        if blocks and is_meta(blocks[0]):
            meta = blocks.pop(0)
            tickets = list(dict.fromkeys(re.findall(r"URM-\d+", meta)))
            for d in DOMAINS:
                if norm(d) in norm(meta):
                    domain = d
                    break
        description = "\n\n".join(blocks)
        if f["docs"]:
            description += "\n\nDocumentation :\n" + "\n".join(f["docs"])
        description = description.strip()

        key = f"{mois}|{f['title']}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key += f"|{seen[key]}"

        link = page_url + ("#" + f["anchor"] if f["anchor"] else "")
        records.append({
            "require": {"Cle": key},
            "fields": {
                "Mois": mois,
                "Titre": f["title"],
                "Domaine": domain,
                "Ticket_URM": ", ".join(tickets),
                "Description": description,
                "Lien": link,
                "Attention": " ; ".join(attention_labels(f["title"], description)),
            },
        })
    return records


def push(records, server, doc_id, table, api_key):
    url = f"{server.rstrip('/')}/api/docs/{doc_id}/tables/{table}/records"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for i in range(0, len(records), 50):
        chunk = records[i:i + 50]
        r = requests.put(url, headers=headers, json={"records": chunk}, timeout=120)
        if r.status_code >= 400:
            sys.exit(f"Erreur Grist {r.status_code} : {r.text[:500]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", type=int, default=2, help="nombre de mois récents à traiter (défaut : 2)")
    ap.add_argument("--all", action="store_true", help="traiter tous les mois de l'année en cours")
    ap.add_argument("--dry-run", action="store_true", help="afficher sans envoyer à Grist")
    ap.add_argument("--file", help="fichier HTML local à analyser (pour tester)")
    ap.add_argument("--mois", help="avec --file : mois du fichier, au format AAAA-MM")
    args = ap.parse_args()

    all_records = []

    if args.file:
        if not args.mois:
            sys.exit("Avec --file, indique aussi --mois AAAA-MM")
        with open(args.file, "rb") as fh:
            soup = BeautifulSoup(fh.read(), "html.parser")
        all_records += parse_features(soup, args.file, args.mois)
    else:
        today = datetime.date.today()
        pages = []  # (année, mois, url)
        for year in (today.year, today.year - 1):
            try:
                for m, url in find_month_pages(year).items():
                    pages.append((year, m, url))
            except requests.RequestException as e:
                print(f"Sommaire {year} inaccessible : {e}", file=sys.stderr)
        if not pages:
            sys.exit("Aucune page mensuelle trouvée dans le sommaire.")
        pages.sort()
        if args.all:
            pages = [p for p in pages if p[0] == today.year]
        else:
            pages = pages[-args.months:]
        for year, m, url in pages:
            mois = f"{year}-{m:02d}"
            records = parse_features(get_soup(url), url, mois)
            print(f"{mois} : {len(records)} nouveautés trouvées ({url})")
            all_records += records

    if not all_records:
        sys.exit("Aucune nouveauté trouvée : la structure de la page a peut-être changé.")

    if args.dry_run:
        for r in all_records:
            f = r["fields"]
            print(f"- {f['Titre']} | {f['Domaine']} | {f['Ticket_URM']} | {f['Attention']}")
        print(f"Total : {len(all_records)} (rien n'a été envoyé)")
        return

    api_key = os.environ.get("GRIST_API_KEY")
    doc_id = os.environ.get("GRIST_DOC_ID")
    if not api_key or not doc_id:
        sys.exit("GRIST_API_KEY et GRIST_DOC_ID doivent être définis.")
    server = os.environ.get("GRIST_SERVER", "https://grist.numerique.gouv.fr")
    table = os.environ.get("GRIST_TABLE", "Mises_a_jour")
    push(all_records, server, doc_id, table, api_key)
    print(f"{len(all_records)} nouveautés envoyées vers Grist.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Lit les notes de version Primo VE (Ex Libris / Clarivate) et envoie les nouveautés
(« Features ») dans une table Grist.

- La page de l'année contient toutes les nouveautés ; le mois de chaque
  nouveauté est lu sous son titre (par exemple « November 2026 … URM-236255 »).
- Une ligne par nouveauté. Si la ligne existe déjà (même colonne Cle), seules
  les colonnes remplies par le script sont mises à jour. Tes colonnes (Statut,
  Concerne, etc.) ne sont jamais touchées.

Variables d'environnement :
  GRIST_API_KEY   clé API Grist (obligatoire, sauf avec --dry-run)
  GRIST_DOC_ID    identifiant du document Grist (obligatoire, sauf avec --dry-run)
  GRIST_SERVER    par défaut https://grist.numerique.gouv.fr
  GRIST_TABLE     par défaut Mises_a_jour

Exemples :
  python primo_to_grist.py --dry-run     # affiche ce qui serait envoyé
  python primo_to_grist.py               # envoie les nouveautés vers Grist
"""
import argparse
import datetime
import os
import re
import sys
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SITE = "https://knowledge.exlibrisgroup.com"
INDEX_URL = SITE + "/Primo/Release_Notes/002Primo_VE/{year}"
NOTES_URL = SITE + "/Primo/Release_Notes/002Primo_VE/{year}/010Primo_VE_{year}_Release_Notes"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; primo-grist-sync/1.0)"}
OUTIL = "Primo VE"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MONTH_RE = re.compile(r"^\s*(%s)\s+(\d{4})" % "|".join(MONTHS))

# Mots repérés dans la description pour remplir la colonne « Attention ».
ATTENTION_RULES = [
    (r"\bby default\b|\bturned on\b", "Comportement par défaut"),
    (r"opt[\s-]?out", "Désactivation possible (opt-out)"),
    (r"opt[\s-]?in", "Activation volontaire (opt-in)"),
    (r"re-?index", "Réindexation requise"),
    (r"customer setting", "Paramètre client (Customer Settings)"),
    (r"\bparameter\b|\bparameters\b", "Paramètre à configurer"),
    (r"customi[sz]ation", "Impact possible sur les personnalisations"),
    (r"contact (clarivate )?support|ask(ing)? (clarivate )?support|support to enable", "Demande au support"),
    (r"\bprivilege|\broles?\b", "Droits ou rôles"),
    (r"\bremoved\b|\bno longer\b|\brenamed\b|\bdeprecated\b|\bchanged\b", "Suppression ou changement"),
    (r"network zone|consorti", "Réseau / consortium"),
]


def clean(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def get_soup(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return BeautifulSoup(r.content, "html.parser")


def find_notes_url(year):
    """Retrouve l'adresse de la page « Release Notes » de l'année via le sommaire."""
    index = INDEX_URL.format(year=year)
    try:
        soup = get_soup(index)
        for a in soup.find_all("a", href=True):
            href = urljoin(index, a["href"])
            if re.search(r"/%d/[^/?#]*Release_Notes(\?|$)" % year, href):
                return href.split("?")[0].split("#")[0]
    except requests.RequestException:
        pass
    return NOTES_URL.format(year=year)


def anchor_map(soup):
    """Associe chaque titre du sommaire de la page à son ancre."""
    m = {}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.startswith("#") and len(h) > 1:
            m.setdefault(clean(a.get_text()), h[1:])
    return m


def find_anchor(h, amap):
    if h.get("id"):
        return h["id"]
    a = h.find("a", attrs={"name": True}) or h.find("a", attrs={"id": True})
    if a:
        return a.get("name") or a.get("id")
    return amap.get(clean(h.get_text()))


def text_of(el):
    for br in el.find_all("br"):
        br.replace_with("\n")
    return clean(el.get_text())


def table_text(table):
    lines = []
    for tr in table.find_all("tr"):
        cells = [clean(c.get_text(" ")).replace("\n", " / ") for c in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c]
        if cells:
            lines.append("- " + " | ".join(cells))
    return "\n".join(lines)


def strip_draft(url):
    parts = urlsplit(url)
    q = [kv for kv in parse_qsl(parts.query, keep_blank_values=True) if kv[0] != "mt-draft"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def parse_meta(line, title):
    """Extrait mois, tickets, origine et domaine de la ligne située sous le titre."""
    m = MONTH_RE.match(line)
    mois = f"{m.group(2)}-{MONTHS.index(m.group(1)) + 1:02d}" if m else "inconnu"
    tickets = list(dict.fromkeys(re.findall(r"(?:URM|CDI)-\d+", line)))
    origins = []
    cerv = re.search(r"CERV.*?#\s*(\d+)", line)
    if cerv:
        origins.append(f"CERV #{cerv.group(1)}")
    elif "CERV" in line:
        origins.append("CERV")
    if re.search(r"idea\s*exchange", line, re.I):
        origins.append("Idea Exchange")
    domains = []
    if title.startswith("NDE UI"):
        domains.append("NDE UI")
    if "Alma Resource Management" in line:
        domains.append("Alma Resource Management")
    if "Collaborative Networks" in line:
        domains.append("Collaborative Networks")
    return mois, tickets, origins, " / ".join(domains) if domains else OUTIL


def attention_labels(title, description):
    haystack = (title + "\n" + description).lower()
    return [label for pattern, label in ATTENTION_RULES if re.search(pattern, haystack)]


def parse_features(soup, page_url):
    main = (soup.select_one("#elm-main-content") or soup.select_one(".mt-content-container")
            or soup.find("main") or soup.body or soup)
    amap = anchor_map(soup)
    features = []
    in_features = False
    current = None

    for el in main.find_all(["h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table"]):
        if el.name == "h2":
            in_features = clean(el.get_text()).lower() == "features"
            current = None
            continue
        if not in_features:
            continue
        if el.name == "h3":
            current = {"title": clean(el.get_text()), "anchor": find_anchor(el, amap),
                       "blocks": [], "docs": []}
            features.append(current)
            continue
        if current is None:
            continue
        if el.name in ("h4", "h5", "h6"):
            t = clean(el.get_text())
            if t:
                current["blocks"].append(f"[{t}]")
            continue
        if el.find_parent(["ul", "ol", "table", "li"]) is not None:
            continue  # élément imbriqué : déjà inclus dans son parent

        if el.name == "p":
            text = text_of(el)
            if not text or text.lower().startswith("no features match"):
                continue
            if re.match(r"for (additional |more )?information", text, re.I):
                for a in el.find_all("a", href=True):
                    link = strip_draft(urljoin(page_url, a["href"]))
                    if link not in current["docs"]:
                        current["docs"].append(link)
                continue
            current["blocks"].append(text)
        elif el.name == "table":
            t = table_text(el)
            if t:
                current["blocks"].append(t)
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
        mois, tickets, origins, domain = "inconnu", [], [], (OUTIL)
        if blocks:
            first_line, _, rest = blocks[0].partition("\n")
            if MONTH_RE.match(first_line):
                mois, tickets, origins, domain = parse_meta(first_line, f["title"])
                if rest.strip():
                    blocks[0] = rest.strip()
                else:
                    blocks.pop(0)
        description = "\n\n".join(blocks)
        if f["docs"]:
            description += "\n\nDocumentation :\n" + "\n".join(f["docs"])
        description = description.strip()

        key = f"Primo|{mois}|{f['title']}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key += f"|{seen[key]}"

        link = page_url + ("#" + f["anchor"] if f["anchor"] else "")
        labels = origins + attention_labels(f["title"], description)
        records.append({
            "require": {"Cle": key},
            "fields": {
                "Outil": OUTIL,
                "Mois": mois,
                "Titre": f["title"],
                "Domaine": domain,
                "Ticket_URM": ", ".join(tickets),
                "Description": description,
                "Lien": link,
                "Attention": " ; ".join(labels),
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


def existing_keys(server, doc_id, table, api_key):
    """Retourne l'ensemble des Cle déjà présentes dans Grist (ou None si lecture impossible)."""
    url = f"{server.rstrip('/')}/api/docs/{doc_id}/tables/{table}/records"
    r = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    if r.status_code >= 400:
        print(f"Lecture des lignes existantes impossible ({r.status_code}) : pas de notification.",
              file=sys.stderr)
        return None
    return {rec["fields"].get("Cle") for rec in r.json().get("records", [])}


def write_summary(new_records, path="nouvelles_primo.md", limit=80):
    """Écrit un résumé des nouvelles lignes, utilisé pour l'e-mail de notification."""
    lines = [f"**{len(new_records)} nouvelle(s) nouveauté(s) Primo VE** ajoutée(s) à ta table Grist "
             "« Mises à jour ». Elles attendent d'être triées.", ""]
    for r in new_records[:limit]:
        f = r["fields"]
        details = " · ".join(x for x in [f["Domaine"], f["Ticket_URM"], f["Attention"]] if x)
        lines.append(f"- [{f['Titre']}]({f['Lien']}) ({f['Mois']})" + (f" — {details}" if details else ""))
    if len(new_records) > limit:
        lines.append(f"- … et {len(new_records) - limit} autres, à voir dans Grist.")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, help="année à traiter (défaut : année en cours)")
    ap.add_argument("--depuis", metavar="AAAA-MM",
                    help="ne garder que les nouveautés de ce mois et des suivants (ex. 2026-12)")
    ap.add_argument("--dry-run", action="store_true", help="afficher sans envoyer à Grist")
    ap.add_argument("--file", help="fichier HTML local à analyser (pour tester)")
    args = ap.parse_args()

    all_records = []

    if args.file:
        with open(args.file, "rb") as fh:
            soup = BeautifulSoup(fh.read(), "html.parser")
        all_records += parse_features(soup, args.file)
    else:
        today = datetime.date.today()
        if args.year:
            years = [args.year]
        else:
            years = [today.year] + ([today.year - 1] if today.month <= 2 else [])
        for year in years:
            url = find_notes_url(year)
            try:
                records = parse_features(get_soup(url), url)
            except requests.RequestException as e:
                print(f"Page {year} inaccessible : {e}", file=sys.stderr)
                continue
            print(f"{year} : {len(records)} nouveautés trouvées ({url})")
            all_records += records

    if not all_records:
        sys.exit("Aucune nouveauté trouvée : la structure de la page a peut-être changé.")

    if args.depuis:
        if not re.fullmatch(r"\d{4}-\d{2}", args.depuis):
            sys.exit("--depuis doit être au format AAAA-MM, par exemple 2026-12")
        total = len(all_records)
        all_records = [r for r in all_records if r["fields"]["Mois"] >= args.depuis]
        print(f"{len(all_records)} nouveauté(s) à partir de {args.depuis} (sur {total} lues sur la page).")
        if not all_records:
            print("Rien à envoyer pour le moment.")
            return

    if args.dry_run:
        for r in all_records:
            f = r["fields"]
            print(f"- [{f['Mois']}] {f['Titre']} | {f['Domaine']} | {f['Ticket_URM']} | {f['Attention']}")
        print(f"Total : {len(all_records)} (rien n'a été envoyé)")
        return

    api_key = os.environ.get("GRIST_API_KEY")
    doc_id = os.environ.get("GRIST_DOC_ID")
    if not api_key or not doc_id:
        sys.exit("GRIST_API_KEY et GRIST_DOC_ID doivent être définis.")
    server = os.environ.get("GRIST_SERVER", "https://grist.numerique.gouv.fr")
    table = os.environ.get("GRIST_TABLE", "Mises_a_jour")

    keys = existing_keys(server, doc_id, table, api_key)
    push(all_records, server, doc_id, table, api_key)
    print(f"{len(all_records)} nouveautés envoyées vers Grist.")

    if keys is not None:
        new_records = [r for r in all_records if r["require"]["Cle"] not in keys]
        if new_records:
            write_summary(new_records)
            print(f"{len(new_records)} nouvelles lignes : résumé écrit dans nouvelles_primo.md")
        else:
            print("Aucune nouvelle ligne depuis la dernière exécution.")


if __name__ == "__main__":
    main()

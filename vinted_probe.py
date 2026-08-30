"""Prototype prudent de lecture d'une page publique de recherche Vinted.

Ce module ne se connecte pas à un compte, ne contourne aucun blocage et ne
réessaie pas lorsqu'une réponse 401, 403, 429 ou un CAPTCHA est rencontré.
Il est volontairement limité à une exécution unique afin que l'appelant décide
explicitement de la fréquence d'utilisation.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests


BASE_URL = "https://www.vinted.fr"
SEARCH_URL = BASE_URL + "/catalog"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / ".vinted_seen.json"
HEADERS = {
    "User-Agent": "PokeStockResearch/0.1 (personal price-monitoring prototype)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
STATUTS_BLOQUANTS = {401, 403, 429}

MARQUEUR_ID = re.compile(r"^product-item-id-(\d+)--overlay-link$")
IMAGE_HD = re.compile(r'\\"photos\\":\[\{\\"url\\":\\"(.+?)\\"')
VENDEUR_PUBLIC = re.compile(
    r'\\"name\\":\\"user_info_header\\".*?'
    r'\\"data\\":\{.*?'
    r'\\"seller_id\\":(?P<seller_id>\d+).*?'
    r'\\"name\\":\\"(?P<nom>(?:\\.|[^"\\])*)\\".*?'
    r'\\"feedback_count\\":(?P<evaluations>\d+).*?'
    r'\\"feedback_reputation\\":(?P<reputation>null|\d+(?:\.\d+)?)',
    re.DOTALL,
)
MONTANT_EURO = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*€")
ETAT = re.compile(r"(?:^|,\s*)État:\s*(.+?)(?=,\s*\d+(?:[.,]\d+)?\s*€)")
MARQUE = re.compile(r"(?:^|,\s*)Marque:\s*(.+?)(?=,\s*État:)")

MARQUEURS_NON_FRANCAIS = re.compile(
    r"\b(?:japonais(?:e)?|japanese|jap|anglais(?:e)?|english|italien(?:ne)?|"
    r"italian|allemand(?:e)?|german|deutsch|chinois(?:e)?|chinese|coréen(?:ne)?|"
    r"korean|espagnol(?:e)?|spanish)\b",
    re.IGNORECASE,
)
MARQUEURS_CODES_NON_FRANCAIS = re.compile(r"\b(?:JP|EN|IT|DE|CN|KR|ES)\b")
MARQUEURS_FRANCAIS = re.compile(
    r"\b(?:français(?:e|es|s)?|french|édition\s+fr|carte\s+fr|version\s+fr)\b",
    re.IGNORECASE,
)
MARQUEUR_CODE_FRANCAIS = re.compile(r"\bFR\b")


class VintedIndisponible(RuntimeError):
    """La page publique ne peut pas être utilisée sans contournement."""


def convertir_montant(valeur):
    return float(valeur.replace(",", "."))


def langue_probable(texte):
    """Retourne un indice prudent, jamais une certification de la langue."""
    if MARQUEURS_NON_FRANCAIS.search(
        texte
    ) or MARQUEURS_CODES_NON_FRANCAIS.search(texte):
        return "non_francais"
    if MARQUEURS_FRANCAIS.search(texte) or MARQUEUR_CODE_FRANCAIS.search(texte):
        return "francais_probable"
    return "inconnue"


def extraire_titre(attribut_titre):
    separateurs = (", Marque:", ", État:")
    titre = attribut_titre
    for separateur in separateurs:
        if separateur in titre:
            titre = titre.split(separateur, 1)[0]
    return titre.strip()


def extraire_image_hd(html, identifiant):
    """Retrouve la première photo f800 associée à une annonce dans l'état SSR."""
    marqueur = f'\\"id\\":{identifiant},\\"productItem\\"'
    debut = html.find(marqueur)
    if debut < 0:
        return None

    # Une fiche produit SSR tient largement dans cette fenêtre. La borner évite
    # d'attribuer par erreur la photo de l'annonce suivante.
    extrait = html[debut : debut + 20_000]
    correspondance = IMAGE_HD.search(extrait)
    if not correspondance:
        return None
    return (
        correspondance.group(1)
        .replace(r"\/", "/")
        .replace(r"\u0026", "&")
        .replace(r"\u002F", "/")
    )


def extraire_vendeur_public(html):
    """Extrait uniquement la réputation publique affichée sur la fiche."""
    marqueur = r'\"name\":\"user_info_header\"'
    debut = html.find(marqueur)
    if debut < 0:
        return None

    # Les données utiles sont au début du composant. La fenêtre bornée évite
    # qu'une modification de page fasse correspondre un autre objet JSON.
    correspondance = VENDEUR_PUBLIC.search(html[debut : debut + 20_000])
    if not correspondance:
        return None

    reputation = correspondance.group("reputation")
    try:
        nom = json.loads(f'"{correspondance.group("nom")}"')
    except json.JSONDecodeError:
        nom = correspondance.group("nom").replace(r'\"', '"')

    seller_id = correspondance.group("seller_id")
    return {
        "id": seller_id,
        "nom": nom,
        "evaluations": int(correspondance.group("evaluations")),
        "note": round(float(reputation) * 5, 2)
        if reputation != "null"
        else None,
        "profil_url": urljoin(BASE_URL, f"/member/{seller_id}"),
    }


def vendeur_est_fiable(vendeur, note_minimum=4.8, evaluations_minimum=10):
    """Applique un seuil prudent, sans présenter la note comme une garantie."""
    if not vendeur or vendeur.get("note") is None:
        return False
    return (
        vendeur["note"] >= note_minimum
        and vendeur.get("evaluations", 0) >= evaluations_minimum
    )


def telecharger_vendeur_public(annonce, timeout=20):
    """Lit la réputation publique depuis la fiche d'une annonce rentable."""
    reponse = requests.get(annonce["url"], headers=HEADERS, timeout=timeout)
    if reponse.status_code in STATUTS_BLOQUANTS:
        raise VintedIndisponible(
            f"Vinted a refusé la fiche vendeur (HTTP {reponse.status_code})."
        )
    reponse.raise_for_status()

    texte_minuscule = reponse.text.lower()
    if "captcha" in texte_minuscule and "user_info_header" not in reponse.text:
        raise VintedIndisponible("Vinted demande un CAPTCHA ; arrêt sans contournement.")
    return extraire_vendeur_public(reponse.text)


def analyser_catalogue(html, limite=20):
    """Extrait les cartes produit rendues côté serveur dans la page publique."""
    soup = BeautifulSoup(html, "html.parser")
    annonces = []
    ids_vus = set()

    for lien in soup.select('a[data-testid$="--overlay-link"]'):
        testid = lien.get("data-testid", "")
        correspondance_id = MARQUEUR_ID.match(testid)
        if not correspondance_id:
            continue

        identifiant = correspondance_id.group(1)
        if identifiant in ids_vus:
            continue

        titre_attribut = lien.get("title", "").strip()
        href = lien.get("href", "").strip()
        montants = MONTANT_EURO.findall(titre_attribut)
        if not titre_attribut or not href or not montants:
            continue

        correspondance_etat = ETAT.search(titre_attribut)
        correspondance_marque = MARQUE.search(titre_attribut)
        prix_article = convertir_montant(montants[-2] if len(montants) >= 2 else montants[-1])
        prix_protege = convertir_montant(montants[-1])

        image = soup.find(
            "img",
            attrs={"data-testid": f"product-item-id-{identifiant}--image--img"},
        )
        titre = extraire_titre(titre_attribut)
        annonce = {
            "id": identifiant,
            "titre": titre,
            "url": urljoin(BASE_URL, href.split("?", 1)[0]),
            "image": image.get("src") if image else None,
            "image_hd": extraire_image_hd(html, identifiant),
            "marque": correspondance_marque.group(1).strip()
            if correspondance_marque
            else None,
            "etat_vinted": correspondance_etat.group(1).strip()
            if correspondance_etat
            else None,
            "prix_article": prix_article,
            "prix_avec_protection": prix_protege,
            "frais_protection": round(max(0.0, prix_protege - prix_article), 2),
            "langue_probable": langue_probable(titre_attribut),
        }
        annonces.append(annonce)
        ids_vus.add(identifiant)

        if len(annonces) >= limite:
            break

    return annonces


def telecharger_catalogue(requete, limite=20, timeout=20):
    reponse = requests.get(
        SEARCH_URL,
        params={
            "search_text": requete,
            "order": "newest_first",
            "page": 1,
            "per_page": max(1, min(96, limite)),
        },
        headers=HEADERS,
        timeout=timeout,
    )

    if reponse.status_code in STATUTS_BLOQUANTS:
        raise VintedIndisponible(
            f"Vinted a refusé la requête publique (HTTP {reponse.status_code})."
        )
    reponse.raise_for_status()

    texte_minuscule = reponse.text.lower()
    if "captcha" in texte_minuscule and "--overlay-link" not in reponse.text:
        raise VintedIndisponible("Vinted demande un CAPTCHA ; arrêt sans contournement.")

    annonces = analyser_catalogue(reponse.text, limite=limite)
    if not annonces:
        raise VintedIndisponible(
            "Aucune annonce structurée trouvée ; la page a changé ou l'accès est limité."
        )
    return annonces


def charger_ids_vus(chemin):
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    return {str(identifiant) for identifiant in donnees if str(identifiant).isdigit()}


def memoriser_ids(chemin, ids):
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    temporaire.write_text(
        json.dumps(sorted(ids, key=int), indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporaire, chemin)


def construire_parser():
    parser = argparse.ArgumentParser(
        description="Teste une recherche publique Vinted sans contournement"
    )
    parser.add_argument("--query", default="carte pokemon")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="affiche aussi les annonces déjà vues")
    parser.add_argument(
        "--remember",
        action="store_true",
        help="mémorise les identifiants pour que le prochain passage ne montre que les nouveautés",
    )
    parser.add_argument("--json", action="store_true", help="produit du JSON")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    return parser


def main():
    args = construire_parser().parse_args()
    limite = max(1, min(96, args.limit))

    try:
        annonces = telecharger_catalogue(args.query, limite=limite)
    except (requests.RequestException, VintedIndisponible) as erreur:
        print(f"❌ {erreur}", file=sys.stderr)
        return 2

    ids_vus = charger_ids_vus(args.state_file)
    nouvelles = [annonce for annonce in annonces if annonce["id"] not in ids_vus]
    resultat = annonces if args.all else nouvelles

    if args.json:
        print(json.dumps(resultat, indent=2, ensure_ascii=False))
    else:
        print(f"🔎 {len(annonces)} annonce(s) lue(s), {len(nouvelles)} nouvelle(s)")
        for annonce in resultat:
            print(f"\n{annonce['titre']}")
            print(
                f"  {annonce['prix_article']:.2f} € + protection "
                f"= {annonce['prix_avec_protection']:.2f} €"
            )
            print(f"  État : {annonce['etat_vinted'] or 'inconnu'}")
            print(f"  Langue : {annonce['langue_probable']}")
            print(f"  {annonce['url']}")

    if args.remember:
        memoriser_ids(args.state_file, ids_vus | {annonce["id"] for annonce in annonces})
        print(f"💾 {len(annonces)} identifiant(s) mémorisé(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

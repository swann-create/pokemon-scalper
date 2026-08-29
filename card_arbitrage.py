"""Analyse automatique Vinted -> TCGdex -> Cardmarket, sans achat automatique."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import requests

from cardmarket_data import (
    age_source_heures,
    charger_guide,
    indexer_guide,
    prix_reference_conservateur,
)
from tcgdex_matcher import chercher_carte, langue_depuis_ocr, normaliser
from vinted_probe import (
    VintedIndisponible,
    charger_ids_vus,
    memoriser_ids,
    telecharger_catalogue,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("POKESTOCK_DATA_DIR", str(BASE_DIR))).expanduser()
GUIDE_FILE = DATA_DIR / "cardmarket_price_guide_6.json"
ARBITRAGE_STATE_FILE = DATA_DIR / ".vinted_arbitrage_seen.json"
_MOTEUR_RAPIDOCR = None
_RAPIDOCR_INDISPONIBLE = False


COEFFICIENTS_ETAT = {
    "neuf avec etiquette": 0.90,
    "neuf sans etiquette": 0.90,
    "tres bon etat": 0.90,
    "bon etat": 0.80,
    "satisfaisant": 0.65,
}


def coefficient_etat(etat):
    return COEFFICIENTS_ETAT.get(normaliser(etat), 0.60)


def ocr_disponible():
    return importlib.util.find_spec("rapidocr") is not None or bool(shutil.which("tesseract"))


def texte_rapidocr(chemin):
    global _MOTEUR_RAPIDOCR, _RAPIDOCR_INDISPONIBLE
    if _RAPIDOCR_INDISPONIBLE:
        return None
    try:
        from rapidocr import RapidOCR
    except (ImportError, OSError):
        _RAPIDOCR_INDISPONIBLE = True
        return None

    try:
        if _MOTEUR_RAPIDOCR is None:
            _MOTEUR_RAPIDOCR = RapidOCR()
        resultat = _MOTEUR_RAPIDOCR(str(chemin))
    except Exception:
        return None

    textes = getattr(resultat, "txts", None)
    if textes:
        return "\n".join(str(texte) for texte in textes if texte)

    # Compatibilité avec les anciennes versions de RapidOCR.
    lignes = resultat[0] if isinstance(resultat, tuple) and resultat else resultat
    if isinstance(lignes, list):
        textes = [
            ligne[1]
            for ligne in lignes
            if isinstance(ligne, (list, tuple)) and len(ligne) >= 2
        ]
        return "\n".join(str(texte) for texte in textes if texte) or None
    return None


def texte_ocr_image(url, timeout=20):
    if not url:
        return None

    suffixe = Path(url.split("?", 1)[0]).suffix or ".img"
    with tempfile.TemporaryDirectory(prefix="pokestock-ocr-") as dossier:
        chemin = Path(dossier) / ("annonce" + suffixe)
        reponse = requests.get(url, timeout=timeout)
        reponse.raise_for_status()
        chemin.write_bytes(reponse.content)

        texte = texte_rapidocr(chemin)
        if texte:
            return texte

        executable = shutil.which("tesseract")
        if not executable:
            return None

        commandes = (
            [executable, str(chemin), "stdout", "-l", "fra+eng", "--psm", "6"],
            [executable, str(chemin), "stdout", "-l", "eng", "--psm", "6"],
        )
        for commande in commandes:
            resultat = subprocess.run(
                commande,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if resultat.returncode == 0 and resultat.stdout.strip():
                return resultat.stdout
    return None


def calculer_rentabilite(
    annonce,
    prix_marche,
    livraison_vinted=2.88,
    commission_cardmarket=0.05,
    reserve_risque=0.10,
    emballage=0.30,
):
    prix_etat = prix_marche * coefficient_etat(annonce.get("etat_vinted"))
    cout_achat = annonce["prix_avec_protection"] + livraison_vinted
    revenu_net = prix_etat * (1 - commission_cardmarket - reserve_risque) - emballage
    marge = revenu_net - cout_achat
    roi = (marge / cout_achat * 100) if cout_achat > 0 else 0
    return {
        "prix_marche": round(prix_marche, 2),
        "prix_revente_prudent": round(prix_etat, 2),
        "cout_achat": round(cout_achat, 2),
        "revenu_net": round(revenu_net, 2),
        "marge": round(marge, 2),
        "roi": round(roi, 1),
    }


def construire_payload_discord(opportunite):
    annonce = opportunite["annonce"]
    calcul = opportunite["calcul"]
    carte = opportunite["correspondance"]["carte"]
    return {
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "💸 Opportunité carte Pokémon",
                "description": annonce["titre"],
                "url": annonce["url"],
                "color": 0x2ECC71,
                "fields": [
                    {"name": "Édition", "value": carte["set"]["name"], "inline": True},
                    {"name": "Numéro", "value": carte["localId"], "inline": True},
                    {"name": "Langue", "value": "Français vérifié", "inline": True},
                    {"name": "Coût Vinted", "value": f"{calcul['cout_achat']:.2f} €", "inline": True},
                    {"name": "Revente prudente", "value": f"{calcul['prix_revente_prudent']:.2f} €", "inline": True},
                    {"name": "Marge / ROI", "value": f"{calcul['marge']:.2f} € / {calcul['roi']:.1f} %", "inline": True},
                ],
                "thumbnail": {"url": annonce.get("image_hd") or annonce["image"]}
                if annonce.get("image_hd") or annonce.get("image")
                else None,
                "footer": {"text": "Vérifie toujours l'authenticité et les photos avant l'achat."},
            }
        ],
        "components": [
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 5, "label": "Voir sur Vinted", "url": annonce["url"]}
                ],
            }
        ],
    }


def envoyer_discord(webhook, opportunite, timeout=15):
    payload = construire_payload_discord(opportunite)
    if payload["embeds"][0].get("thumbnail") is None:
        payload["embeds"][0].pop("thumbnail", None)
    reponse = requests.post(webhook, json=payload, timeout=timeout)
    reponse.raise_for_status()


def analyser_annonces(
    annonces,
    guide_index,
    livraison_vinted,
    marge_minimum,
    roi_minimum,
    utiliser_ocr=True,
):
    opportunites = []
    statistiques = {
        "annonces": len(annonces),
        "sans_correspondance": 0,
        "langue_non_confirmee": 0,
        "sans_prix": 0,
        "marge_insuffisante": 0,
        "opportunites": 0,
    }

    for annonce in annonces:
        correspondance = chercher_carte(annonce["titre"])
        texte_ocr = None
        if utiliser_ocr:
            texte_ocr = texte_ocr_image(
                annonce.get("image_hd") or annonce.get("image")
            )
            if not correspondance and texte_ocr:
                correspondance = chercher_carte(
                    f"{annonce['titre']}\n{texte_ocr}"
                )
        if not correspondance:
            statistiques["sans_correspondance"] += 1
            continue

        langue = langue_depuis_ocr(
            texte_ocr,
            correspondance["carte"],
            correspondance["carte_anglaise"],
        )
        if langue != "francais_confirme":
            statistiques["langue_non_confirmee"] += 1
            continue

        id_product = correspondance["id_product_cardmarket"]
        ligne_prix = guide_index.get(id_product)
        prix_marche = prix_reference_conservateur(ligne_prix or {})
        if prix_marche is None:
            statistiques["sans_prix"] += 1
            continue

        calcul = calculer_rentabilite(
            annonce,
            prix_marche,
            livraison_vinted=livraison_vinted,
        )
        if calcul["marge"] < marge_minimum or calcul["roi"] < roi_minimum:
            statistiques["marge_insuffisante"] += 1
            continue

        opportunites.append(
            {
                "annonce": annonce,
                "correspondance": correspondance,
                "langue": langue,
                "calcul": calcul,
            }
        )

    statistiques["opportunites"] = len(opportunites)
    return opportunites, statistiques


def construire_parser():
    parser = argparse.ArgumentParser(description="Détecte les cartes Vinted sous-évaluées")
    parser.add_argument("--query", default="carte pokemon")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--shipping", type=float, default=2.88)
    parser.add_argument("--min-margin", type=float, default=8.0)
    parser.add_argument("--min-roi", type=float, default=30.0)
    parser.add_argument("--discord", action="store_true")
    parser.add_argument("--remember", action="store_true")
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="secondes entre deux passages (0 = un seul passage, minimum 300)",
    )
    parser.add_argument("--no-ocr", action="store_true", help="diagnostic seulement : aucune opportunité ne sera validée")
    parser.add_argument("--json", action="store_true")
    return parser


def valider_intervalle(intervalle, memoriser):
    if intervalle < 0 or 0 < intervalle < 300:
        raise ValueError("l'intervalle minimum est de 300 secondes (5 minutes)")
    if intervalle and not memoriser:
        raise ValueError("--remember est obligatoire avec --interval")


def lire_variable_env_locale(nom):
    valeur = os.environ.get(nom, "").strip()
    if valeur:
        return valeur
    try:
        lignes = (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        if cle.strip() == nom:
            return valeur.strip().strip("'\"")
    return ""


def executer_cycle(args, webhook=""):
    guide = charger_guide(GUIDE_FILE)
    guide_index = indexer_guide(guide)
    annonces = telecharger_catalogue(args.query, limite=max(1, min(96, args.limit)))

    ids_vus = charger_ids_vus(ARBITRAGE_STATE_FILE)
    nouvelles = [annonce for annonce in annonces if annonce["id"] not in ids_vus]
    opportunites, statistiques = analyser_annonces(
        nouvelles,
        guide_index,
        livraison_vinted=max(0, args.shipping),
        marge_minimum=max(0, args.min_margin),
        roi_minimum=max(0, args.min_roi),
        utiliser_ocr=not args.no_ocr,
    )

    age_guide = age_source_heures(guide)
    resultat = {
        "guide_cardmarket_age_heures": round(age_guide, 1) if age_guide is not None else None,
        "statistiques": statistiques,
        "opportunites": opportunites,
    }
    if args.json:
        print(json.dumps(resultat, indent=2, ensure_ascii=False))
    else:
        print(f"🔎 {statistiques['annonces']} nouvelle(s) analysée(s)")
        print(f"🔗 {statistiques['sans_correspondance']} sans correspondance exacte")
        print(f"🇫🇷 {statistiques['langue_non_confirmee']} langue non confirmée")
        print(f"💶 {statistiques['marge_insuffisante']} marge insuffisante")
        print(f"✅ {statistiques['opportunites']} opportunité(s)")
        for opportunite in opportunites:
            calcul = opportunite["calcul"]
            print(f"\n{opportunite['annonce']['titre']}")
            print(f"  Marge : {calcul['marge']:.2f} € — ROI : {calcul['roi']:.1f} %")
            print(f"  {opportunite['annonce']['url']}")

    if args.discord and opportunites:
        for opportunite in opportunites:
            envoyer_discord(webhook, opportunite)

    if args.remember:
        memoriser_ids(
            ARBITRAGE_STATE_FILE,
            ids_vus | {annonce["id"] for annonce in annonces},
        )
    return resultat


def main():
    args = construire_parser().parse_args()
    try:
        valider_intervalle(args.interval, args.remember)
    except ValueError as erreur:
        raise SystemExit(str(erreur))

    if not args.no_ocr and not ocr_disponible():
        print(
            "⚠️ OCR absent : installe requirements-arbitrage.txt ; "
            "aucune langue ne pourra être validée."
        )
    webhook = lire_variable_env_locale("DISCORD_WEBHOOK_URL") if args.discord else ""
    if args.discord and not webhook:
        raise SystemExit("DISCORD_WEBHOOK_URL est manquant dans .env")

    while True:
        try:
            executer_cycle(args, webhook=webhook)
        except (requests.RequestException, VintedIndisponible) as erreur:
            print(f"❌ Arrêt prudent : {erreur}", file=sys.stderr)
            return 2

        if not args.interval:
            break
        print(f"⏳ Prochain passage dans {args.interval // 60} minutes…")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n🛑 Bot arrêté proprement.")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

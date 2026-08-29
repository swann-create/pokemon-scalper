"""Téléchargement et lecture du guide de prix public Cardmarket Pokémon."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import requests


PRICE_GUIDE_URL = (
    "https://downloads.s3.cardmarket.com/"
    "productCatalog/priceGuide/price_guide_6.json"
)
DEFAULT_MAX_AGE_SECONDS = 12 * 60 * 60


def cache_est_frais(chemin, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    try:
        age = time.time() - chemin.stat().st_mtime
    except OSError:
        return False
    return 0 <= age <= max_age_seconds


def valider_guide(donnees):
    if not isinstance(donnees, dict):
        raise ValueError("Guide Cardmarket invalide")
    guides = donnees.get("priceGuides")
    if not isinstance(guides, list) or not guides:
        raise ValueError("Le guide Cardmarket ne contient aucun prix")
    return donnees


def charger_guide(
    chemin,
    max_age_seconds=DEFAULT_MAX_AGE_SECONDS,
    timeout=60,
    session=requests,
):
    """Utilise le cache local et télécharge atomiquement lorsqu'il est périmé."""
    chemin = Path(chemin)
    if cache_est_frais(chemin, max_age_seconds=max_age_seconds):
        return valider_guide(json.loads(chemin.read_text(encoding="utf-8")))

    reponse = session.get(PRICE_GUIDE_URL, timeout=timeout)
    reponse.raise_for_status()
    donnees = valider_guide(reponse.json())

    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_suffix(chemin.suffix + ".tmp")
    temporaire.write_text(
        json.dumps(donnees, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporaire, chemin)
    return donnees


def indexer_guide(donnees):
    return {
        int(ligne["idProduct"]): ligne
        for ligne in donnees["priceGuides"]
        if ligne.get("idProduct") is not None
    }


def prix_reference_conservateur(ligne):
    """Prix revendable prudent, en incluant l'offre la moins chère disponible."""
    valeurs = []
    for cle in ("low", "avg1", "avg7", "avg30", "trend", "avg"):
        valeur = ligne.get(cle)
        if isinstance(valeur, (int, float)) and valeur > 0:
            valeurs.append(float(valeur))
    return min(valeurs) if valeurs else None


def age_source_heures(donnees):
    valeur = donnees.get("createdAt")
    if not valeur:
        return None
    try:
        date_source = datetime.strptime(valeur, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - date_source.astimezone(timezone.utc)).total_seconds() / 3600

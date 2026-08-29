"""Correspondance stricte numéro/édition/variante avec TCGdex en français."""

import re
import time
import unicodedata

import requests


API_BASE = "https://api.tcgdex.net/v2"
NUMERO_CARTE = re.compile(r"\b([A-Z]{0,5}\d{1,3})\s*/\s*(\d{1,3})\b", re.IGNORECASE)


def normaliser(texte):
    texte = unicodedata.normalize("NFKD", str(texte or ""))
    texte = "".join(caractere for caractere in texte if not unicodedata.combining(caractere))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texte.lower()).split())


def extraire_numero(titre):
    correspondance = NUMERO_CARTE.search(titre or "")
    if not correspondance:
        return None
    return correspondance.group(1).upper(), int(correspondance.group(2))


def variantes_numero(numero, total_officiel):
    variantes = [numero]
    if numero.isdigit():
        sans_zero = str(int(numero))
        avec_zero = sans_zero.zfill(max(2, len(str(total_officiel))))
        variantes.extend([sans_zero, avec_zero])
    return list(dict.fromkeys(variantes))


def obtenir_json(session, url, params=None, timeout=15, tentatives=3):
    """Réessaie brièvement une panne réseau TCGdex, jamais un refus d'accès."""
    for tentative in range(tentatives):
        try:
            reponse = session.get(url, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            if tentative + 1 >= tentatives:
                raise
            time.sleep(2**tentative)
            continue

        if reponse.status_code == 404:
            return None
        if reponse.status_code in {502, 503, 504} and tentative + 1 < tentatives:
            time.sleep(2**tentative)
            continue
        reponse.raise_for_status()
        return reponse.json()
    return None


def nom_present(titre, nom_carte):
    nom = normaliser(nom_carte)
    return bool(nom) and nom in normaliser(titre)


def descripteur_variante(variante):
    morceaux = [
        variante.get("type"),
        variante.get("subtype"),
        variante.get("size"),
        " ".join(variante.get("stamp") or []),
    ]
    return normaliser(" ".join(str(morceau or "") for morceau in morceaux))


def selectionner_variante(carte, titre):
    variantes = [
        variante
        for variante in carte.get("variants_detailed") or []
        if ((variante.get("pricing") or {}).get("cardmarket") or {}).get("idProduct")
    ]
    if not variantes:
        prix = ((carte.get("pricing") or {}).get("cardmarket") or {})
        return {"type": "inconnue", "pricing": {"cardmarket": prix}} if prix.get("idProduct") else None

    ids = {
        int(variante["pricing"]["cardmarket"]["idProduct"])
        for variante in variantes
    }
    if len(ids) == 1:
        return variantes[0]

    titre_normalise = normaliser(titre)
    marqueurs = []
    groupes = (
        (("reverse",), "reverse"),
        (("1ere edition", "1re edition", "first edition"), "1re edition"),
        (("sans ombre", "shadowless"), "sans ombre"),
        (("jumbo", "oversized"), "jumbo"),
        (("holo", "holographique"), "holo"),
    )
    for expressions, marqueur in groupes:
        if any(expression in titre_normalise for expression in expressions):
            marqueurs.append(marqueur)

    if not marqueurs:
        return None

    correspondances = []
    for variante in variantes:
        descripteur = descripteur_variante(variante)
        if all(marqueur in descripteur for marqueur in marqueurs):
            correspondances.append(variante)
    return correspondances[0] if len(correspondances) == 1 else None


def chercher_carte(titre, session=requests, timeout=15):
    numero = extraire_numero(titre)
    if not numero:
        return None
    numero_local, total_officiel = numero

    editions = obtenir_json(
        session,
        f"{API_BASE}/fr/sets",
        params={"cardCount.official": total_officiel},
        timeout=timeout,
    ) or []

    correspondances = []
    for edition in editions:
        carte = None
        for variante_numero in variantes_numero(numero_local, total_officiel):
            carte = obtenir_json(
                session,
                f"{API_BASE}/fr/cards/{edition['id']}-{variante_numero}",
                timeout=timeout,
            )
            if carte:
                break
        if not carte or not nom_present(titre, carte.get("name")):
            continue

        variante = selectionner_variante(carte, titre)
        if not variante:
            continue

        carte_anglaise = obtenir_json(
            session,
            f"{API_BASE}/en/cards/{carte['id']}",
            timeout=timeout,
        )
        prix_variante = variante["pricing"]["cardmarket"]
        correspondances.append(
            {
                "carte": carte,
                "carte_anglaise": carte_anglaise,
                "variante": variante,
                "id_product_cardmarket": int(prix_variante["idProduct"]),
            }
        )

    return correspondances[0] if len(correspondances) == 1 else None


def fragments_langue(carte):
    fragments = [carte.get("name")]
    for attaque in carte.get("attacks") or []:
        fragments.append(attaque.get("name"))
    for talent in carte.get("abilities") or []:
        fragments.append(talent.get("name"))
    return [normaliser(fragment) for fragment in fragments if len(normaliser(fragment)) >= 4]


def langue_depuis_ocr(texte_ocr, carte_francaise, carte_anglaise):
    texte = normaliser(texte_ocr)
    if not texte:
        return "inconnue"

    marqueurs_fr = ("faiblesse", "resistance", "retraite", "energie", "talent")
    marqueurs_en = ("weakness", "resistance", "retreat", "energy", "ability")
    score_fr = sum(marqueur in texte for marqueur in marqueurs_fr)
    score_en = sum(marqueur in texte for marqueur in marqueurs_en)

    fragments_fr = set(fragments_langue(carte_francaise))
    fragments_en = set(fragments_langue(carte_anglaise or {}))
    score_fr += 2 * sum(fragment in texte for fragment in fragments_fr - fragments_en)
    score_en += 2 * sum(fragment in texte for fragment in fragments_en - fragments_fr)

    if score_fr >= 2 and score_fr > score_en:
        return "francais_confirme"
    if score_en >= 1 and score_en >= score_fr:
        return "non_francais"
    return "inconnue"

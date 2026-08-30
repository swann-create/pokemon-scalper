"""Source eBay France via l'API Browse officielle.

Cette source utilise uniquement un jeton d'application OAuth et ne se connecte
pas au compte eBay de l'utilisateur. Les achats restent entièrement manuels.
"""

import os
from urllib.parse import quote

import requests

from vinted_probe import fusionner_catalogues_recents, langue_probable


TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE = "EBAY_FR"


class EbayIndisponible(RuntimeError):
    """L'API eBay n'est pas utilisable pour ce passage."""


def identifiants_ebay():
    return (
        os.environ.get("EBAY_CLIENT_ID", "").strip(),
        os.environ.get("EBAY_CLIENT_SECRET", "").strip(),
    )


def obtenir_jeton(client_id, client_secret, timeout=20):
    reponse = requests.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=timeout,
    )
    if reponse.status_code in {401, 403, 429}:
        raise EbayIndisponible(
            f"eBay a refusé l'accès API (HTTP {reponse.status_code})."
        )
    reponse.raise_for_status()
    jeton = reponse.json().get("access_token", "").strip()
    if not jeton:
        raise EbayIndisponible("eBay n'a pas fourni de jeton d'application.")
    return jeton


def _montant_eur(conteneur):
    if not conteneur or conteneur.get("currency") != "EUR":
        return None
    try:
        return float(conteneur["value"])
    except (KeyError, TypeError, ValueError):
        return None


def extraire_livraison(item):
    couts = []
    for option in item.get("shippingOptions") or []:
        cout = _montant_eur(option.get("shippingCost"))
        if cout is not None:
            couts.append(cout)
    return min(couts) if couts else None


def normaliser_annonce_ebay(item, requete):
    """Convertit un résultat eBay dans le format commun du comparateur."""
    if "FIXED_PRICE" not in (item.get("buyingOptions") or []):
        return None

    prix = _montant_eur(item.get("price"))
    livraison = extraire_livraison(item)
    vendeur_brut = item.get("seller") or {}
    nom_vendeur = str(vendeur_brut.get("username") or "").strip()
    try:
        evaluations = int(vendeur_brut.get("feedbackScore"))
        pourcentage = float(vendeur_brut.get("feedbackPercentage"))
    except (TypeError, ValueError):
        return None

    titre = str(item.get("title") or "").strip()
    url = str(item.get("itemWebUrl") or "").strip()
    item_id = str(item.get("itemId") or "").strip()
    if prix is None or livraison is None or not titre or not url or not item_id or not nom_vendeur:
        return None

    image = (item.get("image") or {}).get("imageUrl")
    return {
        "id": f"ebay:{item_id}",
        "source": "eBay",
        "titre": titre,
        "url": url,
        "image": image,
        "image_hd": image,
        "marque": "Pokémon",
        "etat_vinted": item.get("condition"),
        "prix_article": prix,
        "prix_avec_protection": prix,
        "frais_protection": 0.0,
        "livraison_estimee": livraison,
        "langue_probable": langue_probable(titre),
        "requete_source": requete,
        "date_creation": item.get("itemCreationDate"),
        "vendeur": {
            "id": nom_vendeur,
            "nom": nom_vendeur,
            "evaluations": evaluations,
            "note": round(max(0.0, min(100.0, pourcentage)) / 20, 2),
            "profil_url": f"https://www.ebay.fr/usr/{quote(nom_vendeur, safe='')}",
        },
    }


def rechercher_ebay(requete, jeton, limite=40, code_postal="75001", timeout=20):
    reponse = requests.get(
        SEARCH_URL,
        params={
            "q": requete,
            "limit": max(1, min(200, limite)),
            "sort": "newlyListed",
            "filter": "buyingOptions:{FIXED_PRICE}",
        },
        headers={
            "Authorization": f"Bearer {jeton}",
            "Accept-Language": "fr-FR",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            "X-EBAY-C-ENDUSERCTX": (
                f"contextualLocation=country=FR,zip={code_postal}"
            ),
        },
        timeout=timeout,
    )
    if reponse.status_code in {401, 403, 429}:
        raise EbayIndisponible(
            f"eBay a refusé la recherche API (HTTP {reponse.status_code})."
        )
    reponse.raise_for_status()

    annonces = []
    for item in reponse.json().get("itemSummaries") or []:
        annonce = normaliser_annonce_ebay(item, requete)
        if annonce:
            annonces.append(annonce)
    return annonces


def telecharger_catalogues_ebay(
    requetes,
    limite=40,
    code_postal=None,
    timeout=20,
):
    client_id, client_secret = identifiants_ebay()
    if not client_id or not client_secret:
        raise EbayIndisponible("clés EBAY_CLIENT_ID/EBAY_CLIENT_SECRET absentes")

    jeton = obtenir_jeton(client_id, client_secret, timeout=timeout)
    code_postal = (
        code_postal
        or os.environ.get("EBAY_DELIVERY_POSTAL_CODE", "75001").strip()
        or "75001"
    )
    catalogues = []
    for requete in dict.fromkeys(requete.strip() for requete in requetes):
        if not requete:
            continue
        annonces = rechercher_ebay(
            requete,
            jeton,
            limite=limite,
            code_postal=code_postal,
            timeout=timeout,
        )
        catalogues.append((requete, annonces))
    return fusionner_catalogues_recents(catalogues)

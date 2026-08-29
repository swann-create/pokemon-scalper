# PokéStock Alerts

Bot de veille des prix et disponibilités du JCC Pokémon. Il peut utiliser deux
types de sources :

- les pages publiques des boutiques déjà compatibles ;
- les flux produits officiels Awin, prioritaires lorsqu'ils sont configurés.

## Découverte automatique

Le bot recherche de nouveaux produits Pokémon JCC sur E.Leclerc dès son
démarrage, puis une fois par heure. Les nouveautés sont ajoutées dans
`discovered_products.json`, surveillées immédiatement et dédupliquées par URL.
Ce fichier est conservé dans le volume de données lorsque le bot tourne avec
Docker.

Les recherches et leur fréquence sont configurables dans `.env` :

```bash
POKESTOCK_AUTO_DISCOVERY=true
POKESTOCK_DISCOVERY_INTERVAL_SECONDS=3600
POKESTOCK_DISCOVERY_QUERIES=pokemon booster,pokemon coffret,pokemon cartes
POKESTOCK_DISCOVERY_LIMIT_PER_QUERY=50
POKESTOCK_DISCOVERY_MAX_NEW_PER_RUN=10
```

La fréquence minimale autorisée est de 15 minutes afin de respecter les sites.
Seules dix nouveautés sont intégrées par recherche. Les pages publiques sont
ensuite vérifiées par petits lots tournants de quatre produits par boutique,
avec une pause entre les requêtes. Tous les produits finissent donc par être
contrôlés sans envoyer une rafale susceptible de provoquer une erreur 429.

## Alertes et plafonds de prix

Au premier passage, les produits déjà présents sont enregistrés sans envoyer
une avalanche d'alertes. Ensuite, Discord est prévenu pour une nouveauté, un
retour en stock ou une baisse de prix. Les offres marketplace restent ignorées
par défaut.

Des plafonds facultatifs peuvent être ajoutés dans `.env` :

```bash
POKESTOCK_MAX_PRICE_BOOSTER=
POKESTOCK_MAX_PRICE_PACK=
POKESTOCK_MAX_PRICE_BUNDLE=
POKESTOCK_MAX_PRICE_DISPLAY=
POKESTOCK_MAX_PRICE_ETB=
POKESTOCK_MAX_PRICE_COFFRET=
POKESTOCK_MAX_PRICE_DECK=
POKESTOCK_MAX_PRICE_OTHER=
```

Une valeur vide signifie qu'aucun plafond n'est appliqué. Si un prix dépasse
son plafond, il est conservé dans l'historique mais aucune alerte n'est envoyée.
Les offres partenaires sont ignorées par défaut. Elles peuvent être autorisées
catégorie par catégorie, mais uniquement lorsqu'un plafond est présent et que
le prix le respecte :

```bash
POKESTOCK_MARKETPLACE_CATEGORIES=etb,coffret
POKESTOCK_MAX_PRICE_ETB=60
POKESTOCK_MAX_PRICE_COFFRET=50
```

Cette configuration laisse passer une ETB partenaire à 59,99 €, refuse la même
ETB à 60,01 € et continue d'annoncer normalement les boosters, packs et autres
produits vendus par les boutiques officielles.

Chaque produit de `products.json` peut aussi avoir son propre plafond, une
priorité et un lien d'achat affilié :

```json
{
  "boutique": "leclerc",
  "nom": "Pokémon ME05 : Booster",
  "url": "https://adresse-publique-utilisee-pour-la-surveillance",
  "url_affilie": "https://lien-awin-utilise-dans-discord",
  "prix_maximum": 7.0,
  "prioritaire": true
}
```

Le plafond du produit remplace celui de sa catégorie. Les produits prioritaires
sont placés au début de chaque lot de vérification. Les alertes Discord gardent
un lien texte de secours et affichent aussi un bouton **Acheter maintenant**.
Ne jamais fabriquer manuellement une URL Awin : utiliser uniquement un lien
fourni par le programme approuvé.

Pour les boutiques qui refusent l'accès automatisé, le bot ne contourne pas
leurs protections : il découvre leurs produits dans les flux officiels Awin
dès que l'URL du flux correspondant est renseignée.

## Vérification rapide

```bash
cd /Users/swann/pokemon-scalper
python3 bot.py --once
```

La commande effectue aussi une découverte automatique. Pour tester seulement
la liste existante :

```bash
python3 bot.py --once --no-auto-discovery
```

## Prototype Vinted isolé

`vinted_probe.py` teste une seule lecture d'une recherche publique Vinted. Il
ne se connecte pas à un compte, ne contourne ni CAPTCHA ni blocage et s'arrête
sur les réponses HTTP 401, 403 ou 429. Il reste séparé du bot principal.

```bash
python3 vinted_probe.py --query "carte pokemon" --limit 10
```

Pour mémoriser les annonces déjà vues et ne montrer que les nouveautés au
passage suivant :

```bash
python3 vinted_probe.py --query "carte pokemon" --limit 50 --remember
```

La langue indiquée reste seulement un indice tiré du titre. Une annonce marquée
`inconnue` ne doit jamais être considérée française sans analyse des photos.

### Comparaison stricte avec Cardmarket

`card_arbitrage.py` relie une annonce comportant un numéro de carte à l'édition
française TCGdex, sélectionne sa variante exacte, retrouve son identifiant
Cardmarket puis utilise le guide de prix public officiel. Le prix retenu est le
plus prudent parmi le prix bas et les moyennes disponibles.

Installation unique de l'OCR gratuit et local :

```bash
python3 -m pip install --user -r requirements-arbitrage.txt
```

```bash
python3 card_arbitrage.py --query "carte pokemon" --limit 30
```

Le bot utilise la photo haute définition quand Vinted l'inclut dans la page.
RapidOCR lit le nom, le numéro et le texte de la carte ; cela permet aussi de
reconnaître une carte lorsque le vendeur a oublié son numéro dans le titre. Une
opportunité n'est validée que si le texte français est confirmé, si la marge
nette atteint 8 € et si le ROI atteint 30 %. Le calcul inclut la protection
Vinted, 2,88 € de livraison par défaut, 5 % de commission Cardmarket,
l'emballage et une réserve de risque. La livraison peut être ajustée avec
`--shipping`.

Pour envoyer les opportunités validées sur Discord et mémoriser les annonces :

```bash
python3 card_arbitrage.py --limit 50 --discord --remember
```

Pour le laisser surveiller automatiquement, l'intervalle minimum est de cinq
minutes et la mémorisation est obligatoire afin de ne pas répéter les alertes :

```bash
.venv/bin/python card_arbitrage.py --limit 50 --discord --remember --interval 300
```

Le mode continu s'arrête sans réessayer si Vinted renvoie un refus d'accès, une
limitation ou un CAPTCHA. Arrêt manuel : `Ctrl+C`.

### Exécution gratuite sans utiliser le Mac

Le workflow `.github/workflows/card-arbitrage.yml` lance un passage GitHub
Actions toutes les cinq minutes. Il conserve séparément le guide Cardmarket et
les identifiants Vinted déjà vus, sans enregistrer le webhook dans le code.

Après avoir envoyé les fichiers sur le dépôt GitHub public :

1. ouvrir **Settings > Secrets and variables > Actions** ;
2. choisir **New repository secret** ;
3. nommer le secret `DISCORD_WEBHOOK_URL` ;
4. coller le webhook Discord comme valeur ;
5. ouvrir **Actions > Alertes cartes Vinted Cardmarket** ;
6. utiliser **Run workflow** pour le premier test.

Le planning GitHub ne garantit pas une exécution exactement à la minute prévue
et peut être retardé. Aucun secret ne doit être placé dans `.env.example`, le
workflow ou un autre fichier envoyé sur le dépôt public.

Tesseract est utilisé en solution de secours lorsqu'il est déjà installé. Sans
RapidOCR ni Tesseract, le programme continue son diagnostic mais refuse toutes
les annonces dont la langue ne peut pas être vérifiée.

## Fonctionnement des flux Awin

Une fois un programme approuvé dans Awin :

1. ouvrir **Toolbox > Create-a-Feed** ;
2. sélectionner la boutique et la langue française ;
3. choisir CSV, toutes les colonnes utiles et la compression gzip ;
4. copier l'URL générée ;
5. fournir cette URL au bot par une variable d'environnement.

Variables reconnues :

```bash
export AWIN_FEED_FNAC_URL='URL_GENERÉE_PAR_AWIN'
export AWIN_FEED_CULTURA_URL='URL_GENERÉE_PAR_AWIN'
export AWIN_FEED_LECLERC_URL='URL_GENERÉE_PAR_AWIN'
export AWIN_FEED_DARTY_URL='URL_GENERÉE_PAR_AWIN'
export AWIN_FEED_RAKUTEN_URL='URL_GENERÉE_PAR_AWIN'
```

L'URL Awin contient une clé privée. Ne pas la publier, ne pas l'envoyer sur
Discord et ne pas l'ajouter au code ou à `products.json`.

Le bot télécharge un flux au maximum une fois par heure, filtre le JCC Pokémon,
supprime les doublons et utilise automatiquement le flux à la place des pages
publiques de la boutique. La fréquence peut être augmentée, sans descendre sous
15 minutes :

```bash
export AWIN_FEED_REFRESH_SECONDS=3600
```

## Automatisation sur un serveur

Le fichier `compose.yaml` lance le bot dans un conteneur isolé. Il redémarre
automatiquement après un plantage et après le redémarrage du serveur, tant que
Docker est lui-même activé au démarrage.

Préparation unique :

```bash
cd /chemin/vers/pokemon-scalper
cp .env.example .env
nano .env
docker compose up --detach --build
```

Dans `.env`, renseigner au minimum `DISCORD_WEBHOOK_URL`. Les URL Awin pourront
être ajoutées plus tard sans modifier le code.

Commandes utiles :

```bash
docker compose ps
docker compose logs --follow --tail 100
docker compose restart
docker compose down
```

Les historiques sont conservés dans un volume Docker nommé `pokestock-data`,
même si le conteneur est remplacé. Les journaux sont limités à trois fichiers
de 10 Mo pour ne pas remplir le serveur.

## Lancement continu sans Docker

```bash
python3 bot.py --interval 30
```

Arrêt : `Ctrl+C`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

# Aliud — collecteur de médias

Une intégration Home Assistant qui lit chaque jour les publications les mieux
classées d'une centaine de sources, en écrit un relevé daté, et le dépose sur un
stockage objet compatible S3. Reddit est le premier média ; le contrat qui les
décrit en accueille d'autres sans rouvrir l'ordonnanceur.

L'archive est brute : rien n'est filtré ni jugé à la collecte. Ce qui en est fait
se décide en aval, sur le bucket.

## Ce qu'elle fait

| | |
|---|---|
| **Une horloge** | Un passage par jour, à l'heure réglée dans les options. Un service `aliud_collecteur.collecter` le lance à la main |
| **Un rythme tenu** | Un intervalle réglable entre deux requêtes, une gigue bornée, et un frein qui s'applique **avant** le refus, sur les en-têtes de débit de la source |
| **Un relevé qui dit ses trous** | Sources déclarées, lues, muettes avec leur motif, non lues faute de budget. Un fichier de quarante sources ne se lit pas comme un fichier complet de quarante sources |
| **Une reprise** | Les sources que le budget n'a pas laissé lire repassent en tête au passage suivant |
| **Un dépôt daté** | `reddit/2026/08/28/reddit-20260828T063012Z.json.gz`, jamais écrasé, plus une copie `reddit/dernier.json.gz` pour un lecteur en aval |

## La porte est l'enregistrement, pas la discrétion

`reddit.com/robots.txt` déclare `User-agent: *` puis `Disallow: /`, et ce refus
couvre les chemins `.rss` comme le reste. Ralentir n'y change rien : ce qui lève
le refus est une application enregistrée sur
[`reddit.com/prefs/apps`](https://www.reddit.com/prefs/apps). Sans identifiants,
l'intégration n'émet aucune requête de collecte et le dit à la configuration.

Reddit accorde **100 requêtes par minute** à un client enregistré. Cent sources
tiennent donc dans une minute de budget. Le débit par défaut, 30 par minute,
étale un passage sur trois minutes et demie et garde le reste pour les réessais.
Ce n'est pas de la dissimulation, c'est de la marge.

La réutilisation d'une session de navigateur ou de cookies exportés n'entrera
jamais ici : un `403` opposé à un client non enregistré est un contrôle d'accès
appliqué.

## Installation

Le dépôt est privé, donc il ne passe pas par le magasin HACS. Deux chemins.

**En une commande**, depuis un clone de ce dépôt :

```sh
outils/installer.sh
```

Il copie le module par SSH sur l'add-on « Advanced SSH & Web Terminal », propose
le redémarrage, et nomme ce qui manque quand SSH ne répond pas. `--help` liste
l'hôte, le port et les variables. Rien n'est configuré par le script : les
identifiants se saisissent à l'écran.

**Par HACS**, en dépôt personnalisé de catégorie *Integration*. Un dépôt privé
demande que le jeton GitHub de HACS porte la portée `repo` ; s'il la refuse, la
commande ci-dessus ne dépend d'aucun jeton.

Ensuite, **Paramètres → Appareils et services → Ajouter une intégration →
Aliud**.

## Configuration

Deux écrans, dont les valeurs sont rangées par Home Assistant dans son magasin.
Rien dans `configuration.yaml`, rien dans `secrets.yaml`, rien en dur.

**Reddit** — identifiant du client, secret, et l'agent déclaré. Ce dernier n'est
pas facultatif : un agent générique se fait brider. Forme attendue
`plateforme:identifiant:version (by /u/compte)`.

**Stockage objet** — point d'entrée, région, bucket, clés, et un préfixe
facultatif. Le point d'entrée se lit dans la console du fournisseur, sur le
bucket lui-même. Les deux écrans font un appel réel avant de ranger quoi que ce
soit : une clé fausse se voit à la saisie, pas le lendemain matin après une nuit
sans relevé.

**Le second écran se valide à vide.** Un bucket se provisionne par une chaîne
d'infrastructure qui a son propre rythme, et le greffon doit pouvoir tourner
avant : laisser les cinq champs vides installe une collecte sur disque seul. Le
stockage s'ajoute ensuite par **Reconfigurer**, sans repasser par Reddit ni
perdre l'état de reprise. Ce qui est refusé, c'est un écran à moitié rempli —
quatre champs sur cinq est une saisie interrompue, pas une intention.

Rien de tout ça n'est un mode dégradé silencieux : le capteur porte `depot` à
`non_configure`, parce que quinze relevés qui n'ont jamais quitté la machine ne
doivent pas ressembler à quinze relevés archivés.

### La liste des sources

Un fichier texte, une source par ligne, `#` pour commenter :
`/config/aliud_collecteur/sources-reddit.txt`. Il est écrit au premier passage
avec cent entrées de départ, puis jamais réécrit — la liste appartient à celui
qui l'édite. Les préfixes `r/` et les doublons sont ignorés à la lecture.

### Les options

Heure du passage, requêtes par minute, gigue, tentatives par source, budget d'un
passage, éléments par source, fenêtre du classement, nombre de relevés gardés
localement, et si la charge d'origine reste dans le fichier.

Le budget mérite un mot : passé ce délai, le relevé sort avec ce qu'il a, les
sources non lues sont nommées, et elles repassent en tête au passage suivant.

## Ce que le relevé contient

```json
{
  "schema": 1,
  "media": "reddit",
  "passage": {
    "debut": "2026-08-28T06:30:12+00:00",
    "fin": "2026-08-28T06:33:48+00:00",
    "secondes": 216.4,
    "complet": true,
    "erreur": null,
    "sources_declarees": 100,
    "sources_lues": 100,
    "sources_muettes": [],
    "sources_non_lues": [],
    "reprises_du_passage_precedent": []
  },
  "elements": [
    {
      "media": "reddit", "source": "programming", "id": "t3_abc",
      "titre": "…", "url": "…", "permalien": "…", "auteur": "…",
      "points": 1234, "commentaires": 89,
      "cree_le": "2026-08-28T04:12:00+00:00",
      "collecte_le": "2026-08-28T06:30:14+00:00",
      "brut": { "…": "la charge d'origine, telle que la source l'a rendue" }
    }
  ]
}
```

## Essayer sans bucket

```yaml
action: aliud_collecteur.collecter
data:
  sources: [programming, devops, kubernetes]
```

Trois sources, un relevé complet en quelques secondes, écrit sous
`/config/aliud_collecteur/`. `deposer: false` coupe l'envoi même quand le
stockage est configuré, ce qui sert à essayer un réglage sans rien écrire à
distance.

Le service rend son bilan : le compte d'éléments, les sources muettes avec leur
motif, le chemin du fichier, et `depot`.

## Ce qui se voit dans Home Assistant

Deux capteurs : l'état du dernier passage — `succes`, `partiel` ou `echec` — avec
en attributs le nombre d'éléments, les sources muettes et leur motif, les sources
non lues, le fichier local, la clé déposée, et `depot` ; et l'instant de fin du
dernier passage.

## Voir ce qui s'est passé

**Paramètres → Appareils et services → Aliud → ⋯ → Télécharger les
diagnostics.** Le fichier porte la configuration avec ses quatre secrets
masqués, le dernier passage en détail, et un **journal des vingt derniers
passages** — une ligne chacun, avec le résultat, le compte d'éléments et les
sources muettes.

Le journal existe parce que ce qu'on veut savoir d'un collecteur n'est pas ce
qui vient d'arriver mais la série : combien de passages ont abouti cette
semaine, quelle source se tait *toujours*. Une ligne de journal ne le dit pas.

Ce qui reste lisible dans le fichier : le point d'entrée, la région, le bucket,
le préfixe et l'agent déclaré. C'est ce qu'on regarde en premier quand un dépôt
échoue, et un diagnostic qui masque la moitié du problème oblige à en demander
un second.

Pour le détail d'un passage en cours, le journal de Home Assistant filtré sur
`custom_components.aliud_collecteur` — le niveau se règle dans **Paramètres →
Appareils et services → Aliud → ⋯ → Activer la journalisation de débogage**.

`depot` prend quatre valeurs, distinctes du verdict du passage : `envoye`,
`non_configure` (aucun stockage saisi), `desactive` (`deposer: false`, ou un
passage qui n'a rien pu ouvrir — déposer un relevé vide écraserait
`dernier.json.gz` par un fichier qui ne dit rien) et `refuse`.

`elements` est en attribut pour une raison : un capteur vert pendant quinze jours
avec quinze relevés vides est un échec, pas un succès de l'horloge.

## Ajouter un média

Un fichier dans `custom_components/aliud_collecteur/collecteurs/`, décoré par
`@enregistrer`, qui remplit trois méthodes : `sources()`, `ouvrir(session)` et
`moissonner(session, contexte, source)`. Rien d'autre ne bouge — l'ordonnanceur
ne sait pas ce qu'est un sous-reddit.

Deux exceptions portent la nuance qui compte : `SourceMuette` pour ce qui ne se
réessaie pas dans ce passage, `TropDeRequetes` pour ce qui se réessaie, avec
l'attente demandée quand la source l'a dite.

> **X n'est pas livré.** Le contrat l'accueille, mais son API répond `401` sans
> abonnement et le site ne rend rien d'exploitable. Ce qui manque est un
> abonnement, pas une implémentation.

## Développer

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python homeassistant==2026.8.3 pytest-homeassistant-custom-component==0.13.357
.venv/bin/python -m pytest
```

Quatre-vingt-neuf tests, dont la signature SigV4 recoupée contre le vecteur
publié par AWS. Chaque cas de l'ordonnanceur a rougi contre une cassure volontaire, le
28/08/2026, listée en tête de `tests/test_ordonnanceur.py` : **un test qui n'a
jamais échoué n'a rien prouvé.**

## Pourquoi pas boto3

Il se déclarerait en une ligne de `manifest.json`. Il pèse plusieurs dizaines de
mégaoctets sur une installation qui tourne souvent sur carte SD, son import coûte
une seconde au démarrage, et il est synchrone — il faudrait le pousser dans un
exécuteur pour ne pas bloquer la boucle de Home Assistant. Ce qu'on lui demande
tient en un `PUT` signé : quatre-vingts lignes de `hmac` et `hashlib`, sur
l'`aiohttp` que Home Assistant embarque déjà. `requirements` reste vide.

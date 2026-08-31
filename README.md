# Aliud — collecteur de médias

Une intégration Home Assistant qui lit chaque jour les publications les mieux
classées de quatre médias, en écrit un relevé daté par média, et le dépose sur un
stockage objet compatible S3. Le contrat qui décrit un média en accueille
d'autres sans rouvrir l'ordonnanceur.

L'archive est brute : rien n'est filtré ni jugé à la collecte. Ce qui en est fait
se décide en aval, sur le bucket.

## Les cinq médias

| Média | Identifiants | Ce qu'il rend |
|---|---|---|
| **RSS / Atom** | aucun | N'importe quel flux. Aucune porte ne s'y ferme : le publier, c'est dire « lisez-moi en automatique » |
| **Arctic Shift** | aucun | Les publications Reddit, servies par un tiers. Décalées de deux jours |
| **Hacker News** | aucun | La page d'accueil et des recherches, par l'index Algolia. Scores réels, fraîcheur immédiate |
| **Lobsters** | aucun | `hottest`, `newest`, et les fils d'étiquette, par le JSON public |
| **Reddit** | client OAuth **ou** cookie de session | En direct. Sa porte s'est refermée au client anonyme — voir ci-dessous |

Chacun a sa liste de sources, son relevé et sa clé de dépôt. Un passage les lit
l'un après l'autre, jamais en parallèle : ils partagent le budget de temps, et
deux passages simultanés rendraient le rythme de chacun illisible.

## Reddit a fermé sa porte, et Arctic Shift est le détour

Relevé le 29/08/2026 : `reddit.com` refuse un client anonyme **au niveau réseau**,
`robots.txt` compris.

> whoa there, pardner! Your request has been blocked due to a network policy. […]
> If you're running a script or application, please register or sign in with your
> developer credentials.

`403` sur `top.json`, `403` sur `.rss`. Ce n'est pas une limite de débit : un
crawl lent reçoit le même refus qu'un crawl rapide. Le rythme n'est pas la
variable, et il n'y a rien à contourner — le collecteur `reddit` reste livré, pour
qui dispose d'un client enregistré.

Deux voies restent ouvertes.

**Arctic Shift** sert les archives publiques de Reddit par sa propre API, dont le
`robots.txt` est `User-agent: *` puis `Disallow:` — vide, donc tout permis. On ne
franchit rien : on lit un autre service, qui autorise ce qu'il autorise.

**Un cookie de session** d'un compte du studio, autorisé par le board le
31/08/2026 — clause 4 de l'ADR 0034 du dépôt `aliud`. Le collecteur `reddit`
l'accepte à la place du client enregistré, qui reste préféré : un jeton ne fait
que lire, un cookie publie, vote et modère.

Il se colle dans son propre écran de configuration, **dans n'importe lequel des
trois formats** qu'un navigateur exporte : la ligne d'en-tête, un export JSON
d'extension, ou un fichier `cookies.txt`. Les cookies d'un autre site sont
écartés — coller l'export du mauvais onglet est l'erreur la plus facile à faire
et la plus pénible à diagnostiquer.

**Une session expire, et l'écran le dit.** Quand l'export porte une date, elle
est gardée : le capteur montre `valide`, `bientot` ou `expire` avec les jours
restants. Un cookie déjà périmé empêche l'intégration de démarrer, et une
session qui tombe en cours de passage arrête le collecteur — dans les deux cas
Home Assistant pose lui-même sa carte « à reconfigurer » et amène au formulaire.
C'est son mécanisme natif de réauthentification, préféré à une notification
qu'on ferme et qu'on oublie.

Ce qui n'est **pas** fait, et c'est un choix : usurper un agent de navigateur.
Le board a posé les deux techniques et a choisi le cookie. Un compte se connecte
et le studio répond de ce qu'il lit ; un agent déguisé ne répond de rien, et
c'est exactement ce que le filtre de Reddit cherche à arrêter. Le collecteur
envoie donc son agent nommé, cookie ou pas.

Un `401` ou un `403` en cours de passage **arrête** le collecteur au lieu de
réessayer : une session tombée ne revient pas seule, et insister sur cent
sources est la meilleure façon de faire remarquer le compte.

**Son score mûrit, et c'est toute la contrainte.** Il capture une publication à sa
création puis la recapture plus tard. Mesuré sur r/programming le 29/08/2026 :

| | Publications | Score maximum | Commentaires maximum |
|---|---|---|---|
| J-0 | 18 | 8 | 1 |
| J-3 | 24 | **700** | 273 |
| J-30 | 41 | 368 | 264 |

Un « top du jour » lu là serait donc un classement de zéros. La fenêtre est
décalée de deux jours par défaut, et le relevé porte ses deux bornes pour que
personne ne la prenne pour hier.

## Ce qu'elle fait

| | |
|---|---|
| **Une horloge** | Un passage par jour, à l'heure réglée dans les options. Un service `aliud_collecteur.collecter` le lance à la main, sur tous les médias ou sur un seul |
| **Un rythme tenu** | Un intervalle réglable entre deux requêtes, une gigue bornée, et un frein qui s'applique **avant** le refus, sur les en-têtes de débit de la source |
| **Un relevé qui dit ses trous** | Sources déclarées, lues, muettes avec leur motif, non lues faute de budget. Un fichier de quarante sources ne se lit pas comme un fichier complet de quarante sources |
| **Une reprise** | Les sources que le budget n'a pas laissé lire repassent en tête au passage suivant, média par média |
| **Un dépôt daté** | `hackernews/2026/08/29/hackernews-20260829T063012Z.json.gz`, jamais écrasé, plus une copie `<media>/dernier.json.gz` pour un lecteur en aval |
| **Un agrégat qui prend le pire** | Un média en échec décide de l'état du capteur. Un capteur vert se lit « tout va bien », et ce serait faux |

## Installation

**Par HACS**, en dépôt personnalisé de catégorie *Integration*, avec l'adresse
de ce dépôt. HACS pose ensuite l'entité de mise à jour, qui signale chaque
version publiée.

> HACS ne sait pas lire un dépôt privé — sa documentation est explicite,
> « Private GitHub repositories can not be used with HACS at all », quel que
> soit le jeton. Ce dépôt est public pour cette raison-là, et pour aucune autre.

**Sans HACS**, depuis un clone de ce dépôt :

```sh
outils/installer.sh
```

Il copie le module par SSH sur l'add-on « Advanced SSH & Web Terminal », propose
le redémarrage, et nomme ce qui manque quand SSH ne répond pas. `--help` liste
l'hôte, le port et les variables. Relancé, il met à jour : l'ancien module part
avant que le neuf arrive, donc un fichier disparu entre deux versions ne traîne
pas.

Rien n'est configuré par l'installation : les identifiants se saisissent à
l'écran, après le redémarrage, dans **Paramètres → Appareils et services →
Ajouter une intégration → Aliud**.

## Configuration

Deux ou trois écrans, dont les valeurs sont rangées par Home Assistant dans son
magasin. Rien dans `configuration.yaml`, rien dans `secrets.yaml`, rien en dur.

**Médias** — ce que le collecteur va lire. Trois d'entre eux n'ont besoin
d'aucun identifiant, et l'écran suivant ne s'affiche que si quelqu'un coche
Reddit : demander une clé pour un média qu'on ne lit pas est une façon sûre de
bloquer une installation.

**Reddit**, si coché — identifiant du client, secret, et l'agent déclaré. Ce
dernier n'est pas facultatif : un agent générique se fait brider. Forme attendue
`plateforme:identifiant:version (by /u/compte)`.

**Stockage objet** — point d'entrée, région, bucket, clés, et un préfixe
facultatif. Le point d'entrée se lit dans la console du fournisseur, sur le
bucket lui-même. Les deux écrans font un appel réel avant de ranger quoi que ce
soit : une clé fausse se voit à la saisie, pas le lendemain matin après une nuit
sans relevé.

**Une fois installé, tout se règle par ⋮ → Reconfigurer**, qui ouvre un menu :
médias lus, client Reddit, cookies de session, stockage S3. Chaque branche
enregistre et rend la main — changer un bucket n'oblige pas à retraverser le
reste. (« Configurer », l'engrenage, ouvre les *options* : rythme, heure, agent,
disposition.)

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

Un fichier texte par média, une source par ligne, `#` pour commenter :
`/config/aliud_collecteur/sources-<media>.txt`. Écrit au premier passage avec sa
liste de départ, puis jamais réécrit — elle appartient à celui qui l'édite. Les
doublons sont ignorés à la lecture.

| Média | Ce qu'une ligne veut dire |
|---|---|
| `rss` | Une adresse de flux, ou `<nom> <adresse>`. Sans nom, l'hôte sert d'étiquette |
| `arctic`, `reddit` | Un sous-reddit. Cent au départ ; le préfixe `r/` est toléré |
| `hackernews` | Une étiquette de l'index — `front_page`, `show_hn`, `ask_hn` — ou `q:<termes>` pour une recherche |
| `lobsters` | `hottest`, `newest`, ou `t:<etiquette>` |

**Un plancher de score par source**, suffixé par `@` : `programming@200`,
`front_page@100`, `t:devops@20`. Il vaut **zéro par défaut**, et zéro veut dire
que tout entre — l'archive est brute, un filtre par défaut contredirait ce
qu'elle promet. Ce qu'il écarte est compté dans le relevé, sous
`ecartes_par_plancher` : un filtre silencieux est un trou qu'on ne sait pas
relire.

Le séparateur est `@` et non `:`, déjà pris par `q:` et `t:`. Un plancher posé
sur une source RSS la viderait — un flux ne classe pas, ses publications valent
zéro point — donc n'en pose pas là.

### Les options

**L'agent déclaré** se règle ici, et il vaut pour RSS, Arctic Shift, Hacker News
et Lobsters. Reddit garde le sien, saisi à part : son API exige une forme précise
et refuse un agent générique.

Le défaut se nomme et porte l'adresse du dépôt — un agent joignable se fait
rarement bloquer sans qu'on lui écrive d'abord. Ce qu'on met à la place regarde
celui qui exploite l'installation.

Heure du passage, requêtes par minute, gigue, tentatives par source, budget d'un
passage, éléments par source, **décalage et fenêtre en jours** pour Arctic Shift
et Hacker News, fenêtre du classement pour Reddit, nombre de relevés gardés
localement, et si la charge d'origine reste dans le fichier.

Le budget mérite un mot : passé ce délai, le relevé sort avec ce qu'il a, les
sources non lues sont nommées, et elles repassent en tête au passage suivant.

## Ce que le relevé contient

```json
{
  "schema": 1,
  "media": "hackernews",
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
      "media": "hackernews", "source": "programming", "id": "t3_abc",
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
  medias: [lobsters]
  sources: [hottest]
```

`medias` ne réveille que ce qu'on essaie ; sans lui, tous les médias configurés
passent. Un relevé complet en quelques secondes, écrit sous
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

Cent quatre-vingt-quatorze tests, dont la signature SigV4 recoupée contre le vecteur
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

"""Reddit, par client enregistré, et jamais autrement.

DEUX PORTES, ET LE COOKIE EST LA SECONDE
Depuis le 31/08/2026, ce collecteur accepte un cookie de session d'un compte du
studio, décidé par le board — clause 4 de l'ADR 0034. Il reste préféré de lire
par client enregistré quand il y en a un : un jeton OAuth ne fait que lire,
alors qu'un cookie publie, vote et modère.

CE QUE LE COOKIE NE CHANGE PAS : L'AGENT RESTE LE NÔTRE
Le board a tranché entre deux techniques et a choisi le cookie contre
l'usurpation d'agent. Ce collecteur envoie donc son agent nommé, avec l'adresse
du dépôt, cookie ou pas. Si Reddit refuse un agent honnête muni d'un cookie
valide, c'est un fait à relever, pas quelque chose à contourner en se déguisant.

UNE SESSION TOMBÉE ARRÊTE LE PASSAGE
En mode cookie, un `401` ou un `403` lève `PassageImpossible` et non
`SourceMuette` : la session ne reviendra pas d'elle-même, et réessayer cent
sources sur une porte fermée est la meilleure façon de faire remarquer le compte.
C'est la troisième condition de la clause 4.

LA PORTE EST L'ENREGISTREMENT, PAS LA DISCRÉTION
`reddit.com/robots.txt` déclare `User-agent: *` puis `Disallow: /`, et ce refus
couvre `/r/<sub>/top/.rss` comme le reste. Un flux RSS servi par un hôte qui
refuse la collecte reste refusé. Ralentir ne change rien à ce refus : ce qui le
lève est `REDDIT_CLIENT_ID` et `REDDIT_CLIENT_SECRET`, obtenus sur
`reddit.com/prefs/apps`. Sans eux, `ouvrir` lève `PassageImpossible` et aucune
requête de collecte n'est tentée.

CE QUI N'ARRIVERA JAMAIS ICI
La réutilisation d'une session de navigateur ou de cookies exportés. Un `403`
opposé à un client non enregistré est un contrôle d'accès appliqué ; le
franchir avec des identifiants de session est un accès automatisé non autorisé.

UN JETON PAR PASSAGE, JAMAIS UN PAR SOURCE
Les jetons `client_credentials` durent une heure. Cent sources en dépenseraient
cent poignées de main, soit la moitié du budget de la minute pour rien.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import (
    decouper_plancher,
    Collecteur,
    Element,
    Moisson,
    PassageImpossible,
    Source,
    SessionTombee,
    SourceMuette,
    TropDeRequetes,
    enregistrer,
)

_LOGGER = logging.getLogger(__name__)

JETON_URL = "https://www.reddit.com/api/v1/access_token"
LISTING_URL = "https://oauth.reddit.com/r/{sub}/top"
LISTING_COOKIE_URL = "https://www.reddit.com/r/{sub}/top.json"

# Le point de départ, écrit dans `/config/aliud_collecteur/sources-reddit.txt`
# au premier passage puis jamais réécrit : la liste appartient au board, pas au
# code. Cent sources, choisies pour couvrir ce que le studio regarde — produit,
# développement, infrastructure, qualité, sécurité, données, marché.
SOURCES_PAR_DEFAUT = """\
programming
ExperiencedDevs
devops
kubernetes
sysadmin
netsec
QualityAssurance
softwaretesting
ProductManagement
SaaS
webdev
javascript
typescript
reactjs
node
golang
rust
python
django
flask
fastapi
java
kotlin
csharp
dotnet
php
laravel
ruby
rails
elixir
scala
haskell
cpp
c_programming
swift
androiddev
iOSProgramming
FlutterDev
reactnative
docker
selfhosted
homelab
terraform
ansible
aws
AZURE
googlecloud
linuxadmin
linux
debian
archlinux
networking
PFSENSE
sre
devsecops
cybersecurity
blueteamsec
AskNetsec
Malware
ReverseEngineering
crypto
dataengineering
datascience
MachineLearning
LocalLLaMA
LanguageTechnology
analytics
PowerBI
SQL
PostgreSQL
mysql
mongodb
redis
elasticsearch
apachekafka
bigdata
learnprogramming
cscareerquestions
softwarearchitecture
microservices
graphql
api
opensource
github
gitlab
vim
neovim
emacs
vscode
jetbrains
UXDesign
userexperience
web_design
Frontend
accessibility
startups
Entrepreneur
smallbusiness
agile
scrum
"""


# Combien de `403` d'affilée avant de conclure que c'est la session et non les
# sous-reddits. Trois, et le nombre a une raison : un sous-reddit fermé est un
# accident isolé dans une liste de cent, alors qu'une session tombée les ferme
# tous d'un coup. Trois portes closes à la suite ne se produisent presque jamais
# par hasard, et trois requêtes de trop sur un compte restreint ne sont pas le
# pilonnage que la clause 4 interdit.
REFUS_AVANT_ABANDON = 3


@dataclass(slots=True)
class Contexte:
    """Ce qui dure un passage : de quoi s'authentifier, et l'agent qui le porte.

    Il porte aussi de quoi distinguer une porte fermée d'une session morte, et
    c'est pour ça qu'il dure un passage et pas une requête.
    """

    agent: str
    jeton: str = ""
    cookie: str = ""
    lues: int = 0
    refus_consecutifs: int = 0

    @property
    def par_cookie(self) -> bool:
        return not self.jeton and bool(self.cookie)


@enregistrer
class Reddit:
    """Le collecteur Reddit. Une requête par sous-reddit, une seule."""

    media = "reddit"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
        noms: list[str],
        par_source: int = 25,
        fenetre: str = "day",
        cookie: str = "",
    ) -> None:
        self._id = client_id
        self._secret = client_secret
        self._agent = user_agent
        self._cookie = (cookie or "").strip()
        self._noms = noms
        self._par_source = max(1, min(int(par_source), 100))
        self._fenetre = fenetre if fenetre in ("hour", "day", "week", "month") else "day"

    def sources(self) -> list[Source]:
        sorties = []
        for ligne in self._noms:
            nom, plancher = decouper_plancher(ligne)
            sorties.append(
                Source(media=self.media, nom=nom, options={"plancher": plancher})
            )
        return sorties

    async def ouvrir(self, session: Any) -> Contexte:
        """La poignée de main, une fois par passage.

        Ce qui manque ici arrête le passage entier : cent sources refusées pour
        la même raison ne valent pas cent requêtes.
        """
        if not self._agent:
            raise PassageImpossible(
                "reddit : user_agent absent. Un agent générique se fait brider, "
                "donc il est refusé plutôt que deviné. Forme attendue : "
                "<plateforme>:<identifiant>:<version> (by /u/<compte>)"
            )

        # Le client enregistré d'abord : un jeton ne fait que lire, un cookie
        # publie, vote et modère. On ne dépense pas le second quand le premier
        # est disponible.
        if not self._id or not self._secret:
            if self._cookie:
                return Contexte(agent=self._agent, cookie=self._cookie)
            raise PassageImpossible(
                "reddit : ni client enregistré, ni cookie de session. "
                "reddit.com refuse un client anonyme au niveau réseau, "
                "robots.txt compris — il faut l'un des deux."
            )

        autorisation = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        async with session.post(
            JETON_URL,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {autorisation}",
                "User-Agent": self._agent,
            },
        ) as reponse:
            if reponse.status == 429:
                raise TropDeRequetes(
                    "reddit : la poignée de main est bridée",
                    attente=_attente(reponse.headers),
                )
            if reponse.status != 200:
                corps = (await reponse.text())[:200]
                raise PassageImpossible(
                    f"reddit : la poignée de main a rendu {reponse.status}. "
                    f"Vérifier que l'application est un client confidentiel. {corps}"
                )
            charge = await reponse.json(content_type=None)

        jeton = (charge or {}).get("access_token", "")
        if not jeton:
            raise PassageImpossible(
                "reddit : la poignée de main n'a rien rendu. Vérifier que "
                "l'application est un client confidentiel."
            )
        return Contexte(jeton=jeton, agent=self._agent)

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        url = (
            LISTING_COOKIE_URL if contexte.par_cookie else LISTING_URL
        ).format(sub=source.nom)
        parametres = {
            "t": self._fenetre,
            "limit": str(self._par_source),
            "raw_json": "1",
        }
        # L'agent reste le nôtre dans les deux modes : le board a choisi le
        # cookie contre l'usurpation d'agent, pas en plus d'elle.
        entetes = {"User-Agent": contexte.agent}
        if contexte.par_cookie:
            entetes["Cookie"] = contexte.cookie
            entetes["Accept"] = "application/json"
        else:
            entetes["Authorization"] = f"bearer {contexte.jeton}"
        async with session.get(url, params=parametres, headers=entetes) as reponse:
            restant, remise = _debit(reponse.headers)
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"reddit : r/{source.nom} bridé", attente=_attente(reponse.headers)
                )
            if reponse.status in (401, 403):
                if contexte.par_cookie:
                    # UN `403` NE DIT PAS LA MÊME CHOSE QU'UN `401`
                    # Mesuré le 01/09/2026 : `r/api` a rendu `403` au milieu
                    # d'un passage, quatre-vingt-une sources après le début, avec
                    # un cookie valide cent quatre-vingts jours de plus. Reddit
                    # rend `403` à un compte connecté sur un sous-reddit privé,
                    # restreint ou mis en quarantaine — c'est la porte de ce
                    # sous-reddit qui est fermée, pas la session.
                    #
                    # Un `401`, lui, veut dire « je ne sais pas qui tu es » :
                    # c'est la session, et elle ne reviendra pas d'elle-même.
                    #
                    # La clause 4 de l'ADR 0036 reste tenue par le compteur : on
                    # ne pilonne pas une porte fermée, on s'arrête au troisième
                    # refus d'affilée. Ce que l'ancienne version faisait perdre
                    # est mesuré : ce matin-là, six cent un éléments déjà lus.
                    contexte.refus_consecutifs += 1
                    if (reponse.status == 401
                            or contexte.refus_consecutifs >= REFUS_AVANT_ABANDON):
                        raise SessionTombee(
                            f"reddit : {reponse.status} avec le cookie sur "
                            f"r/{source.nom}, après {contexte.refus_consecutifs} "
                            "refus d'affilée. La session est tombée — le passage "
                            "s'arrête au lieu d'insister.")
                    raise SourceMuette(
                        f"reddit : r/{source.nom} a rendu 403 avec le cookie. "
                        "Sous-reddit privé, restreint ou en quarantaine — "
                        f"{contexte.refus_consecutifs} refus d'affilée sur "
                        f"{REFUS_AVANT_ABANDON} avant d'arrêter le passage.")
                # En mode jeton, un 401 sur une source isolée veut dire jeton
                # expiré en cours de passage ; rouvrir est le travail de
                # l'ordonnanceur au passage suivant.
                raise SourceMuette(f"reddit : r/{source.nom} a rendu {reponse.status}")
            if reponse.status == 404:
                raise SourceMuette(f"reddit : r/{source.nom} n'existe pas")
            if reponse.status >= 500:
                raise TropDeRequetes(f"reddit : r/{source.nom} a rendu {reponse.status}")
            if reponse.status != 200:
                raise SourceMuette(
                    f"reddit : r/{source.nom} a rendu {reponse.status}"
                )
            charge = await reponse.json(content_type=None)

        # Une source lue remet le compteur à zéro : ce qui distingue une session
        # morte d'un sous-reddit fermé est que la première ne laisse plus rien
        # passer du tout.
        contexte.lues += 1
        contexte.refus_consecutifs = 0

        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = [
            _element(enfant.get("data") or {}, source.nom, collecte_le)
            for enfant in (charge.get("data") or {}).get("children") or []
        ]
        return Moisson(
            elements=[e for e in elements if e is not None],
            restant=restant,
            remise_a_zero=remise,
        )


def _element(donnee: dict[str, Any], sous_reddit: str, collecte_le: str) -> Element | None:
    identifiant = donnee.get("name") or donnee.get("id")
    if not identifiant:
        return None
    permalien = donnee.get("permalink") or ""
    cree = donnee.get("created_utc")
    return Element(
        media="reddit",
        source=sous_reddit,
        identifiant=str(identifiant),
        titre=donnee.get("title") or "",
        url=donnee.get("url") or "",
        permalien=f"https://www.reddit.com{permalien}" if permalien else "",
        auteur=donnee.get("author") or "",
        points=int(donnee.get("score") or 0),
        commentaires=int(donnee.get("num_comments") or 0),
        cree_le=(
            datetime.fromtimestamp(float(cree), tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            if cree
            else ""
        ),
        collecte_le=collecte_le,
        brut=donnee,
    )


def _debit(entetes: Any) -> tuple[int | None, float | None]:
    """Ce que Reddit dit de son propre compteur, quand il le dit.

    Les deux en-têtes sont des flottants en texte. Absents ou illisibles, on
    rend `None` et l'ordonnanceur reste sur son intervalle de base.
    """
    restant = _flottant(entetes.get("X-Ratelimit-Remaining"))
    remise = _flottant(entetes.get("X-Ratelimit-Reset"))
    return (int(restant) if restant is not None else None, remise)


def _attente(entetes: Any) -> float | None:
    return _flottant(entetes.get("Retry-After"))


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None


assert isinstance(Reddit, type) and hasattr(Reddit, "media")
_ = Collecteur  # le contrat est importé pour être lu, pas pour être hérité

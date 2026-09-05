"""X, par la session d'un compte du studio.

CE QUI AUTORISE CE FICHIER
La clause 4 de l'ADR 0034, décidée par le board le 30/08/2026 : un cookie
exporté d'une session d'un compte du studio peut servir à lire X en automatique.
Ce que ça coûte est écrit là-bas — les conditions d'usage de X l'interdisent, et
le compte encourt la suspension.

POURQUOI PAS PLAYWRIGHT, QUE FONT LES AUTRES
La voie répandue lance un Chromium piloté. Elle demande un binaire par
architecture sur une machine qui tourne souvent sur carte SD, dans une
intégration dont le `requirements` est vide par choix. Ce fichier parle HTTP.

CE QUI REND CE COLLECTEUR FRAGILE, ET ON LE DIT AVANT DE LE DÉCOUVRIR
X ne publie pas d'API pour ça. Les points d'entrée lus ici sont ceux de sa
propre interface web, et leurs identifiants de requête — les `queryId` — changent
à chaque build de leur frontend. Quand ça arrive, la source rend `404` et le
relevé la déclare muette. **Les identifiants sont donc des réglages, pas des
constantes** : une rotation se répare dans l'écran des options, sans attendre une
version du greffon.

CE QU'UNE VRAIE SESSION A DIT
Le premier passage réel a eu lieu le 03/09/2026 : deux comptes, trente-neuf
publications, relevé déposé. Ce que ce fichier n'avait pas vu, c'est un refus
isolé — il concluait la session morte sur le premier `403`, et un compte protégé
suffisait à jeter le passage. Voir `_appeler`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from . import (
    Element,
    Moisson,
    PassageImpossible,
    SessionTombee,
    Source,
    SourceMuette,
    TropDeRequetes,
    decouper_plancher,
    enregistrer,
)

_LOGGER = logging.getLogger(__name__)

BASE = "https://x.com/i/api/graphql"
FIL = "https://x.com/{compte}/status/{identifiant}"

# Le jeton public de l'interface web de X. Il n'identifie personne : il est
# écrit en clair dans le JavaScript que tout visiteur reçoit, et il ne remplace
# pas la session — c'est le cookie qui authentifie. Réglable comme le reste,
# parce qu'il a déjà tourné.
BEARER_PAR_DEFAUT = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Les identifiants de requête au 31/08/2026. Ils tournent : ce sont des défauts,
# pas des vérités. `404` sur toutes les sources veut dire qu'ils ont bougé.
QUERY_COMPTE_DEFAUT = "G3KGOASz96M-Qu0nwmGXNg"
QUERY_FIL_DEFAUT = "V7H0Ap3_Hh2FyS75OCDO3Q"

# Celui de la recherche, relevé le 05/09/2026 sur une session du studio : le
# board a exporté le trafic d'une recherche lancée à la main, et la requête
# `SearchTimeline` y portait cet identifiant. Il tourne comme les deux autres, et
# se répare au même endroit.
#
# Il n'en avait pas la veille, et le fichier disait pourquoi : rien n'avait été
# mesuré, donc rien n'était écrit. Un identifiant inventé qui ressemble à un
# identifiant fait rendre `404` à toutes les recherches, ce qui se lit « les
# identifiants ont tourné » et envoie chercher du mauvais côté. La garde qui
# refuse une recherche sans identifiant reste : elle sert le jour où celui-ci
# tourne et où le board le vide pour le remplacer.
QUERY_RECHERCHE_DEFAUT = "hyPfJYJ_XAtDYoslQc-Rgg"

# Le préfixe d'une source de recherche, celui que `hackernews.py` emploie déjà.
# Une convention qui existe ne se réinvente pas : `q:kubernetes` se lit pareil
# d'un média à l'autre, et `sources.yaml` du studio les déclare de la même façon.
PREFIXE_RECHERCHE = "q:"

# CE QUE LA RECHERCHE RAMÈNE, ET POURQUOI « Top » PLUTÔT QUE « Latest »
# `Latest` rend l'ordre chronologique : tout ce qui a été publié, y compris ce
# que personne n'a lu. `Top` rend ce que X classe par engagement, ce qui est le
# tri que la ligne éditoriale du studio demande de toute façon en aval. Sur un
# passage par jour, prendre le chronologique reviendrait à ramener du bruit puis
# à le jeter au plancher.
PRODUIT_RECHERCHE = "Top"

SOURCES_PAR_DEFAUT = """\
# Une ligne par source. Deux formes :
#   <compte>      le fil d'un compte, sans l'arobase
#   q:<termes>    une recherche, classée par engagement
# Le plancher de score se suffixe par @ :
#   karpathy@500
#   q:agent framework@50
simonw
karpathy
"""


# Combien de `403` d'affilée avant de conclure que c'est la session et non les
# comptes. Reddit s'arrête à trois dans une liste de cent : trois portes closes
# à la suite n'y arrivent presque jamais par hasard. X se lit sur deux ou trois
# comptes, et garder trois y rendrait une session morte indétectable — chaque
# compte muet, aucun formulaire, un échec silencieux chaque matin. Le seuil est
# donc le plus petit des deux nombres, et il vaut 1 sur un compte unique : là,
# rien ne distingue la porte de la session, et le dire est plus honnête que
# choisir.
REFUS_AVANT_ABANDON = 3


@dataclass(slots=True)
class Contexte:
    agent: str
    cookie: str
    csrf: str
    bearer: str
    # De quoi distinguer un compte fermé d'une session morte, et c'est pour ça
    # que le contexte dure un passage et pas une requête.
    seuil: int = 1
    refus_consecutifs: int = 0
    # Un compte résolu une fois par passage : son identifiant ne change pas, et
    # le redemander coûterait une requête par source pour la même réponse.
    identifiants: dict[str, str] = field(default_factory=dict)


@enregistrer
class X:
    """Les publications récentes d'un compte, lues par la session du studio."""

    media = "x"

    def __init__(
        self,
        agent: str,
        noms: list[str],
        cookie: str = "",
        par_source: int = 25,
        bearer: str = "",
        query_compte: str = "",
        query_fil: str = "",
        query_recherche: str = "",
    ) -> None:
        self._agent = agent or "aliud-collecteur"
        self._noms = noms
        self._cookie = (cookie or "").strip()
        self._par_source = max(1, min(int(par_source), 100))
        self._bearer = (bearer or "").strip() or BEARER_PAR_DEFAUT
        self._query_compte = (query_compte or "").strip() or QUERY_COMPTE_DEFAUT
        self._query_fil = (query_fil or "").strip() or QUERY_FIL_DEFAUT
        self._query_recherche = (
            (query_recherche or "").strip() or QUERY_RECHERCHE_DEFAUT
        )

    def sources(self) -> list[Source]:
        """Les comptes suivis et les recherches, dans l'ordre du réglage.

        Une recherche garde son préfixe dans le nom de la source : c'est lui qui
        la distingue d'un compte à la moisson, et c'est sous `x/q:<termes>` que
        l'archive la range. Le `sources.yaml` du studio la déclare sous ce même
        nom, donc rien ne se traduit d'un bout à l'autre de la chaîne.
        """
        sorties = []
        for ligne in self._noms:
            nom, plancher = decouper_plancher(ligne)
            if nom.startswith(PREFIXE_RECHERCHE):
                # Les termes gardent leurs espaces : c'est une requête, pas un
                # identifiant, et « agent framework » n'est pas « agentframework ».
                termes = nom[len(PREFIXE_RECHERCHE):].strip()
                nom = f"{PREFIXE_RECHERCHE}{termes}" if termes else ""
            else:
                nom = nom.lstrip("@")
            if nom:
                sorties.append(
                    Source(media=self.media, nom=nom, options={"plancher": plancher})
                )
        return sorties

    async def ouvrir(self, session: Any) -> Contexte:
        """Aucune poignée de main : la session est dans le cookie.

        Ce qui est vérifié ici est que les deux cookies que X exige sont là.
        `ct0` sert deux fois — comme cookie et comme en-tête anti-CSRF — et son
        absence produit un `403` que rien ne rattache à une saisie incomplète.
        """
        if not self._cookie:
            raise PassageImpossible(
                "x : aucun cookie de session. x.com refuse un client anonyme, "
                "et la clause 4 de l'ADR 0034 n'autorise que cette porte-là."
            )
        morceaux = dict(
            m.strip().split("=", 1)
            for m in self._cookie.split(";")
            if "=" in m
        )
        csrf = morceaux.get("ct0", "").strip()
        if not csrf or not morceaux.get("auth_token", "").strip():
            raise PassageImpossible(
                "x : le cookie ne porte pas auth_token et ct0. Refaire l'export "
                "depuis un onglet connecté, pas depuis une fenêtre privée."
            )
        return Contexte(
            agent=self._agent,
            cookie=self._cookie,
            csrf=csrf,
            bearer=self._bearer,
            seuil=min(REFUS_AVANT_ABANDON, max(1, len(self.sources()))),
        )

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        if source.nom.startswith(PREFIXE_RECHERCHE):
            return await self._rechercher(session, contexte, source)
        identifiant = contexte.identifiants.get(source.nom)
        if not identifiant:
            identifiant = await self._identifiant(session, contexte, source.nom)
            contexte.identifiants[source.nom] = identifiant

        charge = await self._appeler(
            session,
            contexte,
            self._query_fil,
            "UserTweets",
            {
                "userId": identifiant,
                "count": self._par_source,
                "includePromotedContent": False,
                "withVoice": False,
            },
            source.nom,
        )
        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = [
            e
            for e in (
                _element(brut, source.nom, collecte_le)
                for brut in _publications(charge)
            )
            if e is not None
        ]
        return Moisson(elements=elements[: self._par_source])

    async def _rechercher(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        """Une page de résultats pour une requête, et une seule.

        UNE PAGE, PAS UN PARCOURS, ET C'EST LE POINT DE CE COLLECTEUR
        `SearchTimeline` rend un curseur : le suivre ramènerait des centaines de
        publications, au prix d'une requête par page et d'un motif de trafic que
        rien ne distingue d'un aspirateur. Le studio passe une fois par jour et
        cherche de la matière, pas un corpus. Une page de `count` résultats,
        classée par engagement, suffit à ça et coûte une requête.

        Elle en coûte même une de moins qu'un compte suivi : une recherche n'a
        pas d'identifiant à résoudre, donc pas d'appel à `UserByScreenName`.

        Le rythme, lui, n'est pas ici. L'ordonnanceur tient l'intervalle entre
        deux requêtes, sa gigue et son frein quand X annonce son compteur, et il
        vaut pour les recherches comme pour les fils.
        """
        if not self._query_recherche:
            raise SourceMuette(
                f"x : « {source.nom} » demande l'identifiant de requête de "
                "SearchTimeline, qui n'a pas de défaut. Il se relève une fois "
                "dans l'inspecteur d'un onglet connecté, onglet Réseau, sur une "
                "recherche lancée à la main : la requête SearchTimeline le porte "
                "dans son chemin. Il se colle ensuite dans les options."
            )
        termes = source.nom[len(PREFIXE_RECHERCHE):]
        charge = await self._appeler(
            session,
            contexte,
            self._query_recherche,
            "SearchTimeline",
            {
                "rawQuery": termes,
                "count": self._par_source,
                "product": PRODUIT_RECHERCHE,
                "querySource": "typed_query",
            },
            source.nom,
        )
        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = [
            e
            for e in (
                _element(brut, source.nom, collecte_le)
                for brut in _publications(charge)
            )
            if e is not None
        ]
        return Moisson(elements=elements[: self._par_source])

    async def _identifiant(self, session: Any, contexte: Contexte, compte: str) -> str:
        charge = await self._appeler(
            session,
            contexte,
            self._query_compte,
            "UserByScreenName",
            {"screen_name": compte},
            compte,
        )
        trouve = _chercher(charge, "rest_id")
        if not trouve:
            raise SourceMuette(f"x : @{compte} est introuvable ou suspendu")
        return str(trouve)

    async def _appeler(
        self,
        session: Any,
        contexte: Contexte,
        query: str,
        operation: str,
        variables: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        url = f"{BASE}/{query}/{operation}"
        # UNE RECHERCHE N'EST PAS UN COMPTE, ET LE MESSAGE NE DOIT PAS LE DIRE
        # « x : @q:agent framework a rendu 403 » enverrait chercher un compte
        # qui n'existe pas. L'arobase ne se pose que devant un compte.
        ou = source if source.startswith(PREFIXE_RECHERCHE) else f"@{source}"
        entetes = {
            "User-Agent": contexte.agent,
            "Authorization": contexte.bearer,
            "Cookie": contexte.cookie,
            # `ct0` porté deux fois, c'est ce que X attend : le cookie prouve la
            # session, l'en-tête prouve que la requête vient de la page.
            "x-csrf-token": contexte.csrf,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }
        async with session.get(
            url, params={"variables": json.dumps(variables)}, headers=entetes
        ) as reponse:
            if reponse.status in (401, 403):
                # UN `403` NE DIT PAS LA MÊME CHOSE QU'UN `401`
                # Un `401` veut dire « je ne sais pas qui tu es » : c'est la
                # session, et elle ne reviendra pas d'elle-même. Un `403` arrive
                # sur un compte protégé, suspendu ou restreint, et aussi sur un
                # `ct0` que X n'accepte plus — la porte de ce compte, pas la
                # session. Reddit a coûté un relevé de mille cinq cents éléments
                # à confondre les deux le 01/09/2026 ; le même défaut était ici.
                contexte.refus_consecutifs += 1
                if (reponse.status == 401
                        or contexte.refus_consecutifs >= contexte.seuil):
                    raise SessionTombee(
                        f"x : {reponse.status} sur {ou}, après "
                        f"{contexte.refus_consecutifs} refus d'affilée. La "
                        "session est tombée — le passage s'arrête au lieu "
                        "d'insister."
                    )
                raise SourceMuette(
                    f"x : {ou} a rendu 403. Compte protégé, suspendu ou "
                    f"restreint — {contexte.refus_consecutifs} refus d'affilée "
                    f"sur {contexte.seuil} avant d'arrêter le passage."
                )
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"x : {ou} bridé",
                    attente=_flottant(reponse.headers.get("x-rate-limit-reset")),
                )
            if reponse.status == 404:
                raise SourceMuette(
                    f"x : {operation} a rendu 404. L'identifiant de requête a "
                    "probablement tourné — il se corrige dans les options."
                )
            if reponse.status >= 500:
                raise TropDeRequetes(f"x : {ou} a rendu {reponse.status}")
            if reponse.status != 200:
                raise SourceMuette(f"x : {ou} a rendu {reponse.status}")
            charge = await reponse.json(content_type=None)

        # Une requête qui aboutit prouve la session : ce qui précède n'était pas
        # elle. Sans cette remise à zéro, deux comptes protégés séparés par un
        # compte qui répond finiraient par passer pour une session morte.
        contexte.refus_consecutifs = 0

        erreurs = charge.get("errors") if isinstance(charge, dict) else None
        if erreurs and not charge.get("data"):
            message = str(erreurs[0].get("message", ""))[:120]
            raise SourceMuette(f"x : {ou}, {message}")
        return charge


def _publications(charge: Any) -> list[dict[str, Any]]:
    """Les publications, cherchées par leur forme et non par leur chemin.

    La réponse est un empilement d'instructions dont la disposition change d'une
    version à l'autre de leur interface. Chercher `tweet_results.result` partout
    survit à un niveau de plus ; suivre un chemin écrit en dur, non.
    """
    trouves: list[dict[str, Any]] = []

    def descendre(noeud: Any) -> None:
        if isinstance(noeud, dict):
            resultat = (noeud.get("tweet_results") or {}).get("result")
            if isinstance(resultat, dict):
                trouves.append(resultat)
            for valeur in noeud.values():
                descendre(valeur)
        elif isinstance(noeud, list):
            for valeur in noeud:
                descendre(valeur)

    descendre(charge)
    return trouves


def _chercher(noeud: Any, cle: str) -> Any:
    """La première valeur portant cette clé, à n'importe quelle profondeur."""
    if isinstance(noeud, dict):
        if cle in noeud:
            return noeud[cle]
        for valeur in noeud.values():
            trouve = _chercher(valeur, cle)
            if trouve is not None:
                return trouve
    elif isinstance(noeud, list):
        for valeur in noeud:
            trouve = _chercher(valeur, cle)
            if trouve is not None:
                return trouve
    return None


def _element(brut: dict[str, Any], source: str, collecte_le: str) -> Element | None:
    # Une publication protégée ou supprimée arrive sans son bloc `legacy`.
    legacy = brut.get("legacy") or {}
    identifiant = legacy.get("id_str") or brut.get("rest_id")
    if not identifiant:
        return None
    auteur = (
        _chercher(brut.get("core") or {}, "screen_name")
        or _chercher(brut.get("core") or {}, "name")
        or source
    )
    fil = FIL.format(compte=auteur, identifiant=identifiant)
    return Element(
        media="x",
        source=source,
        identifiant=str(identifiant),
        titre=(legacy.get("full_text") or legacy.get("text") or "").replace("\n", " "),
        url=fil,
        permalien=fil,
        auteur=str(auteur),
        points=int(legacy.get("favorite_count") or 0),
        # Réponses et reprises comptent ensemble : ce qui intéresse une archive
        # est combien de gens ont réagi, pas comment.
        commentaires=int(legacy.get("reply_count") or 0)
        + int(legacy.get("retweet_count") or 0),
        cree_le=_date(legacy.get("created_at")),
        collecte_le=collecte_le,
        brut=brut,
    )


def _date(brut: Any) -> str:
    """X date en RFC 822 (« Thu Aug 28 15:17:09 +0000 2026 »), pas en ISO."""
    if not brut:
        return ""
    try:
        return parsedate_to_datetime(str(brut)).astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError):
        return str(brut)


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None

"""Hacker News, par l'index Algolia, qui est fait pour ça.

POURQUOI CELUI-CI PLUTÔT QUE L'API OFFICIELLE
`hacker-news.firebaseio.com` rend une liste d'identifiants, puis un appel par
publication : cinq cents requêtes pour une page d'accueil. L'index Algolia rend
la même chose en une, avec le score et le compte de commentaires déjà dedans.
Il est public, documenté, sans jeton.

CE QU'UNE SOURCE VEUT DIRE ICI
Hacker News n'a pas de sous-forums. Une ligne du fichier de sources est donc
soit une étiquette de l'index — `front_page`, `show_hn`, `ask_hn` —, soit une
recherche préfixée par `q:`. Deux formes, et la seconde existe parce qu'un
studio qui suit trois métiers ne lit pas la même page d'accueil que tout le
monde.

LE SCORE EST RÉEL, LUI
Contrairement aux archives Reddit, l'index rend le score courant. Pas de fenêtre
décalée : `front_page` est la page d'accueil de maintenant.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import Element, Moisson, Source, SourceMuette, TropDeRequetes, enregistrer

_LOGGER = logging.getLogger(__name__)

RECHERCHE_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={}"

ETIQUETTES = ("front_page", "story", "show_hn", "ask_hn", "poll")

SOURCES_PAR_DEFAUT = """\
# Une ligne par source. Deux formes :
#   <etiquette>   front_page, story, show_hn, ask_hn, poll
#   q:<termes>    une recherche sur la fenêtre, classée par pertinence
front_page
show_hn
q:kubernetes
q:postgres
q:incident postmortem
q:developer experience
"""


@dataclass(slots=True)
class Contexte:
    agent: str
    depuis: int


@enregistrer
class HackerNews:
    """Une requête par source, sur une fenêtre de N jours."""

    media = "hackernews"

    def __init__(
        self,
        agent: str,
        noms: list[str],
        par_source: int = 25,
        fenetre_jours: int = 1,
    ) -> None:
        self._agent = agent or "aliud-collecteur"
        self._noms = noms
        self._par_source = max(1, min(int(par_source), 1000))
        self._fenetre = max(1, int(fenetre_jours))

    def sources(self) -> list[Source]:
        return [Source(media=self.media, nom=nom) for nom in self._noms]

    async def ouvrir(self, session: Any) -> Contexte:
        return Contexte(
            agent=self._agent,
            depuis=int(time.time()) - self._fenetre * 86400,
        )

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        parametres = _parametres(source.nom, contexte.depuis, self._par_source)
        async with session.get(
            RECHERCHE_URL, params=parametres, headers={"User-Agent": contexte.agent}
        ) as reponse:
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"hackernews : {source.nom} bridé",
                    attente=_flottant(reponse.headers.get("Retry-After")),
                )
            if reponse.status >= 500:
                raise TropDeRequetes(
                    f"hackernews : {source.nom} a rendu {reponse.status}"
                )
            if reponse.status != 200:
                raise SourceMuette(
                    f"hackernews : {source.nom} a rendu {reponse.status}"
                )
            charge = await reponse.json(content_type=None)

        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = [
            e
            for e in (
                _element(h or {}, source.nom, collecte_le)
                for h in (charge.get("hits") or [])
            )
            if e is not None
        ]
        return Moisson(elements=elements)


def _parametres(nom: str, depuis: int, combien: int) -> dict[str, str]:
    """`front_page` ignore la fenêtre : la page d'accueil est déjà d'aujourd'hui.

    Lui imposer `created_at_i>` la viderait des publications remontées le
    lendemain, qui sont exactement celles qui ont pris du score.
    """
    base = {"hitsPerPage": str(combien)}
    if nom.startswith("q:"):
        return {
            **base,
            "query": nom[2:].strip(),
            "tags": "story",
            "numericFilters": f"created_at_i>{depuis}",
        }
    etiquette = nom if nom in ETIQUETTES else "front_page"
    if etiquette == "front_page":
        return {**base, "tags": etiquette}
    return {**base, "tags": etiquette, "numericFilters": f"created_at_i>{depuis}"}


def _element(hit: dict[str, Any], source: str, collecte_le: str) -> Element | None:
    identifiant = hit.get("objectID")
    if not identifiant:
        return None
    # Un « Ask HN » n'a pas d'URL externe : son fil EST la publication.
    fil = ITEM_URL.format(identifiant)
    return Element(
        media="hackernews",
        source=source,
        identifiant=str(identifiant),
        titre=hit.get("title") or hit.get("story_title") or "",
        url=hit.get("url") or fil,
        permalien=fil,
        auteur=hit.get("author") or "",
        points=int(hit.get("points") or 0),
        commentaires=int(hit.get("num_comments") or 0),
        cree_le=hit.get("created_at") or "",
        collecte_le=collecte_le,
        brut={c: v for c, v in hit.items() if not c.startswith("_")},
    )


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None

"""Lobsters, par son JSON public.

POURQUOI CE MÉDIA À CÔTÉ DE HACKER NEWS
Le même genre de lecteur, un ordre de grandeur plus petit, et une modération qui
écarte ce qui n'est pas technique. Ce qui y monte à cinquante points a souvent
été lu, pas seulement cliqué. Il coûte trois requêtes par passage.

CE QU'UNE SOURCE VEUT DIRE ICI
`hottest`, `newest`, ou `t:<étiquette>` pour un fil d'étiquette. Lobsters expose
chacune de ses pages en JSON en ajoutant `.json` — c'est documenté et c'est
stable.

CE QUI EST DÉLIBÉRÉMENT ABSENT
Aucune pagination. Le site est petit, la première page suffit, et une seconde
coûterait une requête pour des publications sous n'importe quel plancher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import (
    Element,
    Moisson,
    Source,
    SourceMuette,
    TropDeRequetes,
    decouper_plancher,
    enregistrer,
)

_LOGGER = logging.getLogger(__name__)

BASE = "https://lobste.rs"

SOURCES_PAR_DEFAUT = """\
# Une ligne par source. Deux formes :
#   hottest, newest
#   t:<etiquette>   un fil d'étiquette, par exemple t:devops
hottest
t:practices
t:devops
"""


@dataclass(slots=True)
class Contexte:
    agent: str


@enregistrer
class Lobsters:
    """Une page, une requête, pas de pagination."""

    media = "lobsters"

    def __init__(self, agent: str, noms: list[str], par_source: int = 25) -> None:
        self._agent = agent or "aliud-collecteur"
        self._noms = noms
        self._par_source = max(1, int(par_source))

    def sources(self) -> list[Source]:
        sorties = []
        for ligne in self._noms:
            nom, plancher = decouper_plancher(ligne)
            sorties.append(
                Source(media=self.media, nom=nom, options={"plancher": plancher})
            )
        return sorties

    async def ouvrir(self, session: Any) -> Contexte:
        return Contexte(agent=self._agent)

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        async with session.get(
            _url(source.nom), headers={"User-Agent": contexte.agent}
        ) as reponse:
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"lobsters : {source.nom} bridé",
                    attente=_flottant(reponse.headers.get("Retry-After")),
                )
            if reponse.status >= 500:
                raise TropDeRequetes(
                    f"lobsters : {source.nom} a rendu {reponse.status}"
                )
            if reponse.status != 200:
                raise SourceMuette(f"lobsters : {source.nom} a rendu {reponse.status}")
            charge = await reponse.json(content_type=None)

        if not isinstance(charge, list):
            raise SourceMuette(f"lobsters : {source.nom} n'a pas rendu une liste")

        collecte_le = datetime.now(_utc()).isoformat(timespec="seconds")
        elements = [
            e
            for e in (
                _element(p or {}, source.nom, collecte_le) for p in charge
            )
            if e is not None
        ]
        return Moisson(elements=elements[: self._par_source])


def _url(nom: str) -> str:
    if nom.startswith("t:"):
        return f"{BASE}/t/{nom[2:].strip()}.json"
    page = nom if nom in ("hottest", "newest") else "hottest"
    return f"{BASE}/{page}.json"


def _element(donnee: dict[str, Any], source: str, collecte_le: str) -> Element | None:
    identifiant = donnee.get("short_id")
    if not identifiant:
        return None
    fil = donnee.get("comments_url") or f"{BASE}/s/{identifiant}"
    return Element(
        media="lobsters",
        source=source,
        identifiant=str(identifiant),
        titre=donnee.get("title") or "",
        # Une publication sans URL externe est un fil : son adresse est le fil.
        url=donnee.get("url") or fil,
        permalien=fil,
        auteur=donnee.get("submitter_user") or "",
        points=int(donnee.get("score") or 0),
        commentaires=int(donnee.get("comment_count") or 0),
        cree_le=_iso(donnee.get("created_at")),
        collecte_le=collecte_le,
        brut=donnee,
    )


def _iso(valeur: Any) -> str:
    """Lobsters date en heure locale avec décalage ; on garde l'instant en UTC."""
    if not valeur:
        return ""
    try:
        return datetime.fromisoformat(str(valeur)).astimezone(_utc()).isoformat(
            timespec="seconds"
        )
    except ValueError:
        return str(valeur)


def _utc():
    from datetime import timezone

    return timezone.utc


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None

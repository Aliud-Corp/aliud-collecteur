"""Reddit, lu chez un tiers, parce que reddit.com a fermé sa porte.

CE QUI A CHANGÉ, ET POURQUOI CE FICHIER EXISTE
`reddit.com` ne répond plus à un client anonyme — y compris sur `robots.txt`
lui-même, relevé le 29/08/2026 : « Your request has been blocked due to a network
policy ». Ce n'est pas une limite de débit, c'est un blocage à l'entrée, et un
crawl lent reçoit le même `403` qu'un crawl rapide. La porte reste
l'enregistrement, que le studio ne peut plus obtenir.

Arctic Shift sert les archives publiques de Reddit par sa propre API. Son
`robots.txt` est `User-agent: *` puis `Disallow:` — vide, donc tout permis, relevé
le même jour. On ne contourne rien : on lit un autre service, qui autorise ce
qu'il autorise.

LE SCORE MÛRIT, ET C'EST TOUTE LA CONTRAINTE
Arctic Shift capture une publication à sa création, puis la recapture plus tard.
Mesuré sur r/programming le 29/08/2026 : à J-0, dix-sept publications sur dix-huit
sont à un point ; à J-3, le maximum est à 700 points et 273 commentaires.

Un « top du jour » lu ici serait donc un classement de zéros. La fenêtre est
décalée : on lit ce qui a été publié il y a `decalage` à `decalage + fenetre`
jours, quand les scores veulent dire quelque chose. Le relevé porte les deux
bornes, pour que personne ne prenne cette fenêtre pour hier.

L'API NE SAIT PAS TRIER PAR SCORE
`sort_type` n'accepte que `default` et `created_utc` — vérifié en recevant un
`400` qui le dit. Le tri se fait donc ici, sur ce que la fenêtre a rendu.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import (
    Element,
    Moisson,
    Source,
    SourceMuette,
    TropDeRequetes,
    enregistrer,
)

_LOGGER = logging.getLogger(__name__)

RECHERCHE_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"

# La même liste que celle que la veille lisait sur Reddit : ce qui change est la
# porte, pas les sujets.
from .reddit import SOURCES_PAR_DEFAUT as SOURCES_PAR_DEFAUT  # noqa: E402


@dataclass(slots=True)
class Contexte:
    agent: str
    apres: int
    avant: int


@enregistrer
class ArcticShift:
    """Les publications d'un sous-reddit sur une fenêtre décalée."""

    media = "arctic"

    def __init__(
        self,
        agent: str,
        noms: list[str],
        par_source: int = 25,
        decalage_jours: int = 2,
        fenetre_jours: int = 2,
        plafond_brut: int = 100,
    ) -> None:
        self._agent = agent or "aliud-collecteur"
        self._noms = noms
        self._par_source = max(1, int(par_source))
        self._decalage = max(0, int(decalage_jours))
        self._fenetre = max(1, int(fenetre_jours))
        # Ce que l'API rend avant qu'on trie. Au-delà de cent, elle pagine, et
        # une seconde page coûterait une requête par source pour des
        # publications que le plancher écarterait de toute façon.
        self._plafond = max(1, min(int(plafond_brut), 100))

    def sources(self) -> list[Source]:
        return [Source(media=self.media, nom=nom) for nom in self._noms]

    async def ouvrir(self, session: Any) -> Contexte:
        """Aucune poignée de main : l'API est ouverte. On fixe la fenêtre.

        Elle est calculée une fois par passage et pas une fois par source : cent
        sources lues sur cent fenêtres légèrement différentes rendraient un
        relevé dont les bornes ne veulent rien dire.
        """
        maintenant = int(time.time())
        avant = maintenant - self._decalage * 86400
        return Contexte(
            agent=self._agent,
            apres=avant - self._fenetre * 86400,
            avant=avant,
        )

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        parametres = {
            "subreddit": source.nom,
            "after": str(contexte.apres),
            "before": str(contexte.avant),
            "limit": str(self._plafond),
        }
        async with session.get(
            RECHERCHE_URL,
            params=parametres,
            headers={"User-Agent": contexte.agent},
        ) as reponse:
            restant, remise = _debit(reponse.headers)
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"arctic : r/{source.nom} bridé", attente=_flottant(
                        reponse.headers.get("Retry-After")
                    )
                )
            if reponse.status >= 500:
                raise TropDeRequetes(
                    f"arctic : r/{source.nom} a rendu {reponse.status}"
                )
            if reponse.status != 200:
                detail = (await reponse.text())[:120]
                raise SourceMuette(
                    f"arctic : r/{source.nom} a rendu {reponse.status} ({detail})"
                )
            charge = await reponse.json(content_type=None)

        if charge.get("error"):
            raise SourceMuette(f"arctic : {charge['error']}")

        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = [
            e
            for e in (
                _element(p or {}, source.nom, collecte_le)
                for p in (charge.get("data") or [])
            )
            if e is not None
        ]
        # Le tri vit ici, l'API ne sachant classer que par date.
        elements.sort(key=lambda e: (e.points, e.commentaires), reverse=True)
        return Moisson(
            elements=elements[: self._par_source],
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
        media="arctic",
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
    """Arctic Shift publie sa remise à zéro, rarement son reste.

    Sans le reste, l'ordonnanceur garde son intervalle de base — c'est le
    comportement prudent, et il est écrit dans `Rythme.informer`.
    """
    return (
        _entier(entetes.get("X-Ratelimit-Remaining")),
        _flottant(entetes.get("X-Ratelimit-Reset")),
    )


def _entier(valeur: Any) -> int | None:
    f = _flottant(valeur)
    return int(f) if f is not None else None


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None

"""Le rythme d'un passage, et ce qu'il fait quand une source refuse.

CE QUE CE FICHIER SAIT, ET CE QU'IL IGNORE
Il connaît une file de sources, un budget de temps, et le contrat de
`collecteurs/`. Il ne sait pas ce qu'est un sous-reddit ni un jeton OAuth.
Ajouter un média ne le rouvre pas.

ON RALENTIT AVANT D'ÊTRE BRIDÉ, ON NE RÉAGIT PAS AU 429
Reddit publie à chaque réponse ce qu'il lui reste de requêtes et dans combien de
secondes son compteur repart. Quand ce reste tombe sous un seuil, l'intervalle
s'étire pour étaler ce qui reste sur la fenêtre annoncée. Un 429 traité proprement
reste un 429 : il coûte une attente imposée, alors que le frein coûte quelques
secondes choisies.

UN RELEVÉ PARTIEL DIT CE QU'IL N'A PAS PU LIRE
Le budget épuisé ne fait pas échouer le passage. Les sources non lues sont
nommées dans le résultat, gardées pour le passage suivant, et le relevé porte
`complet: false`. Un fichier qui se tait sur ses trous est un fichier qu'on
croira complet dans six mois.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .collecteurs import (
    Collecteur,
    Element,
    PassageImpossible,
    Source,
    SourceMuette,
    TropDeRequetes,
)

_LOGGER = logging.getLogger(__name__)

# Une attente imposée plus longue que cela ne s'attend pas : elle se reprend au
# passage suivant, qui a un budget neuf.
ATTENTE_MAX = 120.0


@dataclass(slots=True)
class Reglages:
    """Ce que le board règle depuis les options de l'intégration."""

    debit_par_minute: int = 30
    gigue_min: float = 0.5
    gigue_max: float = 2.0
    tentatives: int = 3
    budget_secondes: float = 1800.0
    seuil_de_frein: int = 20

    def __post_init__(self) -> None:
        self.debit_par_minute = max(1, int(self.debit_par_minute))
        self.gigue_min = max(0.0, float(self.gigue_min))
        self.gigue_max = max(self.gigue_min, float(self.gigue_max))
        self.tentatives = max(1, int(self.tentatives))
        # Le plancher n'est pas une valeur raisonnable, c'est une garde : un
        # budget à zéro rendrait tous les passages vides sans rien dire.
        self.budget_secondes = max(1.0, float(self.budget_secondes))


@dataclass(slots=True)
class Resultat:
    """Ce qu'un passage rend, trous compris."""

    elements: list[Element] = field(default_factory=list)
    debut: str = ""
    fin: str = ""
    secondes: float = 0.0
    sources_declarees: int = 0
    sources_lues: list[str] = field(default_factory=list)
    sources_muettes: list[dict[str, str]] = field(default_factory=list)
    sources_non_lues: list[str] = field(default_factory=list)
    reprises: list[str] = field(default_factory=list)
    erreur: str | None = None

    @property
    def complet(self) -> bool:
        return (
            self.erreur is None
            and not self.sources_non_lues
            and not self.sources_muettes
            and len(self.sources_lues) == self.sources_declarees
        )


class Rythme:
    """L'intervalle entre deux requêtes, et le frein qui l'étire.

    Le frein est la seule chose qui distingue ce rythme d'un `sleep` en boucle.
    """

    def __init__(self, reglages: Reglages) -> None:
        self._base = 60.0 / reglages.debit_par_minute
        self._gigue = (reglages.gigue_min, reglages.gigue_max)
        self._seuil = reglages.seuil_de_frein
        self._plancher = self._base
        self._precedent: float | None = None

    @property
    def intervalle(self) -> float:
        return self._plancher

    def informer(self, restant: int | None, remise_a_zero: float | None) -> None:
        """Ce que la source vient de dire de son compteur.

        Sous le seuil, l'intervalle devient celui qui étale `restant` requêtes
        sur les `remise_a_zero` secondes annoncées. Au-dessus, il revient à sa
        valeur de base : un frein qui ne se relâche jamais est un débit mal réglé.
        """
        if restant is None or remise_a_zero is None:
            return
        if restant > self._seuil:
            self._plancher = self._base
            return
        if restant <= 0:
            self._plancher = max(self._base, min(remise_a_zero, ATTENTE_MAX))
            return
        self._plancher = max(self._base, remise_a_zero / restant)

    async def attendre(self) -> None:
        """Tient l'intervalle depuis la requête précédente, gigue comprise."""
        cible = self._plancher + random.uniform(*self._gigue)
        if self._precedent is None:
            self._precedent = time.monotonic()
            return
        reste = cible - (time.monotonic() - self._precedent)
        if reste > 0:
            await asyncio.sleep(reste)
        self._precedent = time.monotonic()


class Ordonnanceur:
    """Un passage : une file, un budget, et un résultat qui dit ses trous."""

    def __init__(self, reglages: Reglages) -> None:
        self._reglages = reglages

    async def passage(
        self,
        collecteur: Collecteur,
        session: Any,
        reprises: list[str] | None = None,
    ) -> Resultat:
        reprises = list(reprises or [])
        declarees = collecteur.sources()
        file = _ordonner(declarees, reprises)

        resultat = Resultat(
            debut=_maintenant(),
            sources_declarees=len(declarees),
            reprises=[s.nom for s in file if s.cle in reprises],
        )
        depart = time.monotonic()

        try:
            contexte = await collecteur.ouvrir(session)
        except PassageImpossible as exc:
            resultat.erreur = str(exc)
            resultat.sources_non_lues = [s.nom for s in file]
            return _clore(resultat, depart)
        except TropDeRequetes as exc:
            resultat.erreur = f"la poignée de main est bridée : {exc}"
            resultat.sources_non_lues = [s.nom for s in file]
            return _clore(resultat, depart)
        except Exception as exc:  # noqa: BLE001 — un passage ne casse pas HA
            resultat.erreur = f"ouverture impossible : {exc}"
            resultat.sources_non_lues = [s.nom for s in file]
            return _clore(resultat, depart)

        rythme = Rythme(self._reglages)

        for indice, source in enumerate(file):
            reste_de_budget = self._reglages.budget_secondes - (
                time.monotonic() - depart
            )
            if reste_de_budget <= 0:
                resultat.sources_non_lues = [s.nom for s in file[indice:]]
                _LOGGER.warning(
                    "aliud_collecteur : budget épuisé, %d sources non lues",
                    len(resultat.sources_non_lues),
                )
                break

            await rythme.attendre()
            issue = await self._une_source(
                collecteur, session, contexte, source, rythme, depart
            )
            if issue.elements is not None:
                resultat.elements.extend(issue.elements)
                resultat.sources_lues.append(source.nom)
            else:
                resultat.sources_muettes.append(
                    {"source": source.nom, "raison": issue.raison or "inconnue"}
                )

        return _clore(resultat, depart)

    async def _une_source(
        self,
        collecteur: Collecteur,
        session: Any,
        contexte: Any,
        source: Source,
        rythme: Rythme,
        depart: float,
    ) -> _Issue:
        """Une source, ses réessais, et le mot qui dit pourquoi elle s'est tue."""
        derniere = "inconnue"
        for tentative in range(self._reglages.tentatives):
            try:
                moisson = await collecteur.moissonner(session, contexte, source)
            except SourceMuette as exc:
                return _Issue(None, str(exc))
            except TropDeRequetes as exc:
                derniere = str(exc) or "bridée"
                attente = self._attente(exc.attente, tentative, rythme)
                reste = self._reglages.budget_secondes - (time.monotonic() - depart)
                if tentative == self._reglages.tentatives - 1 or attente >= reste:
                    return _Issue(None, f"bridée : {derniere}")
                _LOGGER.debug(
                    "aliud_collecteur : %s bridée, attente de %.1f s (tentative %d)",
                    source.cle,
                    attente,
                    tentative + 1,
                )
                await asyncio.sleep(attente)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — réseau, JSON, timeout
                derniere = f"{type(exc).__name__}: {exc}"
                if tentative == self._reglages.tentatives - 1:
                    return _Issue(None, derniere)
                await asyncio.sleep(self._attente(None, tentative, rythme))
                continue

            rythme.informer(moisson.restant, moisson.remise_a_zero)
            return _Issue(moisson.elements, None)

        return _Issue(None, derniere)

    def _attente(
        self, demandee: float | None, tentative: int, rythme: Rythme
    ) -> float:
        """Ce que la source a demandé, sinon un repli exponentiel borné."""
        if demandee is not None and demandee > 0:
            return min(demandee, ATTENTE_MAX)
        return min(rythme.intervalle * (2 ** (tentative + 1)), ATTENTE_MAX)


@dataclass(slots=True)
class _Issue:
    """`elements` à `None` veut dire muette ; une liste vide reste un succès."""

    elements: list[Element] | None
    raison: str | None


def _ordonner(declarees: list[Source], reprises: list[str]) -> list[Source]:
    """Les reprises d'abord, dans l'ordre déclaré, puis le reste.

    Trier plutôt que concaténer : une source reprise reste à sa place relative,
    donc deux passages successifs lisent la même liste dans le même ordre, aux
    reprises près. Un ordre qui change à chaque passage rendrait les trous
    illisibles d'un fichier à l'autre.
    """
    if not reprises:
        return list(declarees)
    attendues = set(reprises)
    return [s for s in declarees if s.cle in attendues] + [
        s for s in declarees if s.cle not in attendues
    ]


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clore(resultat: Resultat, depart: float) -> Resultat:
    resultat.fin = _maintenant()
    resultat.secondes = round(time.monotonic() - depart, 2)
    return resultat

"""Ce que l'ordonnanceur fait quand une source refuse, tarde, ou ment.

CHAQUE CAS ICI A ROUGI CONTRE UNE CASSURE VOLONTAIRE, LE 28/08/2026 :
- retirer le `continue` après une attente sur 429 → cas 2 et 3
- remplacer `raise SourceMuette` par un réessai → cas 4
- ne pas remplir `sources_non_lues` sur budget épuisé → cas 5
- concaténer les reprises au lieu de trier → cas 6
- garder le frein après un reste redevenu large → cas 9
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.aliud_collecteur.collecteurs import (
    Element,
    Moisson,
    PassageImpossible,
    Source,
    SourceMuette,
    TropDeRequetes,
)
from custom_components.aliud_collecteur.ordonnanceur import (
    Ordonnanceur,
    Reglages,
    Rythme,
    _ordonner,
)

VITE = dict(debit_par_minute=6000, gigue_min=0.0, gigue_max=0.0, budget_secondes=30)


def _element(nom: str) -> Element:
    return Element(
        media="essai", source=nom, identifiant=f"t3_{nom}", titre=nom, url="",
        permalien="", auteur="", points=0, commentaires=0, cree_le="", collecte_le="",
    )


class CollecteurFactice:
    """Un média d'essai : chaque source rend ce que le scénario lui dit de rendre."""

    media = "essai"

    def __init__(self, scenario: dict[str, list], ouverture=None) -> None:
        self._scenario = {n: list(v) for n, v in scenario.items()}
        self._ouverture = ouverture
        self.appels: list[str] = []

    def sources(self):
        return [Source(media=self.media, nom=n) for n in self._scenario]

    async def ouvrir(self, session):
        if self._ouverture is not None:
            raise self._ouverture
        return {"jeton": "factice"}

    async def moissonner(self, session, contexte, source):
        self.appels.append(source.nom)
        restants = self._scenario[source.nom]
        issue = restants.pop(0) if restants else Moisson([_element(source.nom)])
        if isinstance(issue, Exception):
            raise issue
        return issue


async def _passage(scenario, reglages=None, reprises=None, ouverture=None):
    collecteur = CollecteurFactice(scenario, ouverture)
    ordonnanceur = Ordonnanceur(Reglages(**{**VITE, **(reglages or {})}))
    resultat = await ordonnanceur.passage(collecteur, session=None, reprises=reprises)
    return resultat, collecteur


# 1 ─────────────────────────────────────────────────────────────────────────
async def test_toutes_les_sources_repondent_le_passage_est_complet():
    resultat, _ = await _passage({"a": [], "b": [], "c": []})
    assert resultat.complet is True
    assert resultat.sources_lues == ["a", "b", "c"]
    assert len(resultat.elements) == 3
    assert resultat.sources_muettes == []
    assert resultat.erreur is None


# 2 ─────────────────────────────────────────────────────────────────────────
async def test_un_429_avec_retry_after_attend_puis_reessaie():
    resultat, collecteur = await _passage(
        {"a": [TropDeRequetes("bridée", attente=0.01)]}
    )
    assert collecteur.appels == ["a", "a"]
    assert resultat.sources_lues == ["a"]
    assert resultat.complet is True


# 3 ─────────────────────────────────────────────────────────────────────────
async def test_429_repetes_classent_la_source_muette_sans_arreter_le_passage():
    bridee = [TropDeRequetes("bridée", attente=0.001)] * 5
    resultat, collecteur = await _passage({"a": bridee, "b": []}, {"tentatives": 3})
    assert collecteur.appels.count("a") == 3
    assert resultat.sources_lues == ["b"]
    assert [m["source"] for m in resultat.sources_muettes] == ["a"]
    assert resultat.sources_muettes[0]["raison"].startswith("bridée")
    assert resultat.erreur is None          # un trou n'est pas un échec
    assert resultat.complet is False        # mais ce n'est pas complet non plus


# 4 ─────────────────────────────────────────────────────────────────────────
async def test_une_source_muette_ne_coute_qu_une_requete():
    resultat, collecteur = await _passage(
        {"a": [SourceMuette("r/a n'existe pas")], "b": []}, {"tentatives": 3}
    )
    assert collecteur.appels == ["a", "b"]
    assert resultat.sources_muettes == [
        {"source": "a", "raison": "r/a n'existe pas"}
    ]


# 5 ─────────────────────────────────────────────────────────────────────────
async def test_budget_epuise_nomme_les_sources_non_lues():
    class Lente(CollecteurFactice):
        async def moissonner(self, session, contexte, source):
            await asyncio.sleep(0.35)
            return await super().moissonner(session, contexte, source)

    collecteur = Lente({n: [] for n in ("a", "b", "c", "d", "e", "f")})
    resultat = await Ordonnanceur(
        Reglages(**{**VITE, "budget_secondes": 1})
    ).passage(collecteur, session=None)

    assert resultat.sources_non_lues, "le budget devait couper avant la fin"
    assert resultat.complet is False
    assert set(resultat.sources_lues) & set(resultat.sources_non_lues) == set()
    assert len(resultat.sources_lues) + len(resultat.sources_non_lues) == 6


# 6 ─────────────────────────────────────────────────────────────────────────
def test_les_reprises_passent_en_tete_sans_changer_l_ordre_relatif():
    declarees = [Source("essai", n) for n in ("a", "b", "c", "d")]
    ordre = [s.nom for s in _ordonner(declarees, ["essai:d", "essai:b"])]
    assert ordre == ["b", "d", "a", "c"]


def test_sans_reprise_l_ordre_declare_est_conserve():
    declarees = [Source("essai", n) for n in ("a", "b", "c")]
    assert [s.nom for s in _ordonner(declarees, [])] == ["a", "b", "c"]


# 7 ─────────────────────────────────────────────────────────────────────────
async def test_une_ouverture_impossible_rend_toutes_les_sources_non_lues():
    resultat, collecteur = await _passage(
        {"a": [], "b": []}, ouverture=PassageImpossible("client_id absent")
    )
    assert collecteur.appels == []
    assert resultat.erreur == "client_id absent"
    assert resultat.sources_non_lues == ["a", "b"]
    assert resultat.complet is False


# 8 et 9 ────────────────────────────────────────────────────────────────────
def test_le_frein_etire_l_intervalle_sous_le_seuil():
    rythme = Rythme(Reglages(debit_par_minute=60, seuil_de_frein=20))
    assert rythme.intervalle == pytest.approx(1.0)
    rythme.informer(restant=5, remise_a_zero=50.0)
    assert rythme.intervalle == pytest.approx(10.0)


def test_le_frein_se_relache_quand_le_reste_redevient_large():
    rythme = Rythme(Reglages(debit_par_minute=60, seuil_de_frein=20))
    rythme.informer(restant=5, remise_a_zero=50.0)
    rythme.informer(restant=90, remise_a_zero=30.0)
    assert rythme.intervalle == pytest.approx(1.0)


def test_un_media_muet_sur_son_debit_laisse_l_intervalle_de_base():
    rythme = Rythme(Reglages(debit_par_minute=30))
    rythme.informer(restant=None, remise_a_zero=None)
    assert rythme.intervalle == pytest.approx(2.0)

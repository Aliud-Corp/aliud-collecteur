"""Un passage de bout en bout : collecte, fichier local, dépôt, capteur.

CE QUE CES TESTS TIENNENT, ET QUE LES AUTRES NE TIENNENT PAS
Les modules ont chacun leurs tests. Ici on vérifie l'ordre dans lequel ils
s'appellent, et il porte une décision : **le disque avant le réseau.** Un `PUT`
refusé ne doit pas coûter trois minutes de requêtes déjà dépensées.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aliud_collecteur.const import (
    DOMAIN,
    OPT_DEBIT,
    OPT_GIGUE_MAX,
    OPT_GIGUE_MIN,
    SERVICE_COLLECTER,
)
from tests.faux_reseau import Reponse, Session, listing, publication

DONNEES = {
    "reddit_client_id": "ID",
    "reddit_client_secret": "SECRET",
    "reddit_user_agent": "aliud:collecteur:0.1.0 (by /u/board)",
    "s3_endpoint": "https://s3.example.net",
    "s3_region": "gra",
    "s3_bucket": "aliud-collecte",
    "s3_access_key": "AK",
    "s3_secret_key": "SK",
    "s3_prefixe": "archives",
}
# Pas de gigue, débit maximal : ces tests mesurent l'enchaînement, pas le rythme.
OPTIONS = {OPT_DEBIT: 6000, OPT_GIGUE_MIN: 0, OPT_GIGUE_MAX: 0}


async def _monter(hass: HomeAssistant, session: Session, sources: str) -> MockConfigEntry:
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text(sources, encoding="utf-8")

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data=DONNEES, options=OPTIONS
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    return entree


def _session_nominale(nombre_de_sources: int, puts: list[Reponse] | None = None):
    reponses = [Reponse(200, {"access_token": "j"})]
    for i in range(nombre_de_sources):
        reponses.append(
            Reponse(
                200,
                listing(publication(name=f"t3_{i}")),
                headers={"X-Ratelimit-Remaining": "90", "X-Ratelimit-Reset": "300"},
            )
        )
    reponses.extend(puts if puts is not None else [Reponse(200), Reponse(200)])
    return Session(*reponses)


async def _collecter(hass, session, **donnees):
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        reponse = await hass.services.async_call(
            DOMAIN, SERVICE_COLLECTER, donnees, blocking=True, return_response=True
        )
        await hass.async_block_till_done()
    return reponse


async def test_un_passage_ecrit_le_fichier_local_et_depose_deux_objets(hass):
    session = _session_nominale(2)
    await _monter(hass, session, "programming\ndevops\n")

    bilan = await _collecter(hass, session)

    assert bilan["resultat"] == "succes"
    assert bilan["complet"] is True
    assert bilan["elements"] == 2
    assert bilan["sources_lues"] == 2
    assert bilan["erreur"] is None

    fichier = Path(bilan["fichier"])
    assert fichier.exists()
    contenu = json.loads(gzip.decompress(fichier.read_bytes()))
    assert contenu["media"] == "reddit"
    assert contenu["passage"]["complet"] is True
    assert len(contenu["elements"]) == 2

    puts = [a for a in session.appels if a["methode"] == "PUT"]
    assert len(puts) == 2
    assert puts[0]["url"].startswith(
        "https://s3.example.net/aliud-collecte/archives/reddit/"
    )
    assert puts[0]["url"].endswith(".json.gz")
    assert puts[1]["url"] == (
        "https://s3.example.net/aliud-collecte/archives/reddit/dernier.json.gz"
    )
    assert puts[0]["corps"] == puts[1]["corps"]
    assert bilan["cle_s3"].startswith("archives/reddit/")


async def test_le_disque_survit_a_un_depot_refuse(hass):
    session = _session_nominale(1, puts=[Reponse(403, corps="AccessDenied")])
    await _monter(hass, session, "programming\n")

    bilan = await _collecter(hass, session)

    assert Path(bilan["fichier"]).exists(), "la collecte ne doit pas être perdue"
    assert bilan["elements"] == 1
    assert "dépôt refusé" in bilan["erreur"]
    assert bilan["resultat"] == "partiel"


async def test_une_source_muette_se_lit_dans_le_capteur(hass):
    session = Session(
        Reponse(200, {"access_token": "j"}),
        Reponse(200, listing(publication())),
        Reponse(404),
        Reponse(200),
        Reponse(200),
    )
    await _monter(hass, session, "programming\nsupprime\n")
    await _collecter(hass, session)

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat is not None
    assert etat.state == "partiel"
    assert etat.attributes["sources_declarees"] == 2
    assert etat.attributes["sources_lues"] == 1
    assert [m["source"] for m in etat.attributes["sources_muettes"]] == ["supprime"]
    assert etat.attributes["complet"] is False


async def test_la_limite_borne_le_passage_sans_toucher_au_fichier_de_sources(hass):
    session = _session_nominale(1)
    await _monter(hass, session, "programming\ndevops\nkubernetes\n")

    bilan = await _collecter(hass, session, limite=1)

    assert bilan["sources_declarees"] == 1
    gets = [a for a in session.appels if a["methode"] == "GET"]
    assert len(gets) == 1
    assert gets[0]["url"].endswith("/r/programming/top")
    sources = Path(hass.config.path("aliud_collecteur/sources-reddit.txt"))
    assert sources.read_text(encoding="utf-8").splitlines() == [
        "programming", "devops", "kubernetes"
    ]


async def test_un_essai_peut_ne_rien_ecrire_a_distance(hass):
    session = _session_nominale(1, puts=[])
    await _monter(hass, session, "programming\n")

    bilan = await _collecter(hass, session, deposer=False)

    assert bilan["cle_s3"] == ""
    assert [a for a in session.appels if a["methode"] == "PUT"] == []
    assert Path(bilan["fichier"]).exists()


async def test_les_sources_non_lues_repassent_en_tete_au_passage_suivant(hass):
    # Premier passage : budget d'une seconde, des sources à 0,6 s pièce.
    lent = _session_nominale(3)
    entree = await _monter(hass, lent, "a\nb\nc\n")
    hass.config_entries.async_update_entry(
        entree, options={**OPTIONS, "budget_secondes": 1}
    )
    await hass.async_block_till_done()

    async def _lent(*args, **kwargs):
        import asyncio

        await asyncio.sleep(0.6)
        return await vrai(*args, **kwargs)

    from custom_components.aliud_collecteur.collecteurs.reddit import Reddit

    vrai = Reddit.moissonner
    with patch.object(Reddit, "moissonner", _lent):
        bilan = await _collecter(hass, lent)

    assert bilan["sources_non_lues"], "le budget devait couper"
    non_lues = list(bilan["sources_non_lues"])

    # Second passage : les non-lues sont demandées en premier.
    session = _session_nominale(3)
    await _collecter(hass, session)
    gets = [a["url"].rsplit("/r/", 1)[1].removesuffix("/top")
            for a in session.appels if a["methode"] == "GET"]
    assert gets[: len(non_lues)] == non_lues

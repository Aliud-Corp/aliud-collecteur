"""Ce que le fichier de diagnostic dit, et ce qu'il ne doit jamais dire.

UN DIAGNOSTIC SE PARTAGE
C'est un fichier qu'on télécharge pour l'envoyer à quelqu'un. Le premier test
est donc celui des secrets : quatre valeurs ne doivent en sortir sous aucune
forme, et le test les cherche dans le texte entier plutôt que clé par clé — une
clé recopiée ailleurs par mégarde ne se voit pas autrement.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from custom_components.aliud_collecteur.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.faux_reseau import Reponse, Session
from tests.test_integration import (
    DONNEES,
    _collecter,
    _monter,
    _session_nominale,
)

SECRETS = ("SECRET", "SK", "ID", "AK")


async def test_aucun_secret_ne_sort_du_diagnostic(hass):
    session = _session_nominale(1)
    entree = await _monter(hass, session, "programming\n")
    await _collecter(hass, session)

    diag = await async_get_config_entry_diagnostics(hass, entree)
    texte = json.dumps(diag, default=str)

    for champ in (
        "reddit_client_secret",
        "reddit_client_id",
        "s3_access_key",
        "s3_secret_key",
    ):
        assert diag["configuration"][champ] == "**REDACTED**", champ
    for valeur in SECRETS:
        assert f'"{valeur}"' not in texte, f"{valeur} a fuité dans le diagnostic"


async def test_ce_qui_reste_lisible_est_ce_qu_on_regarde_en_premier(hass):
    session = _session_nominale(1)
    entree = await _monter(hass, session, "programming\n")
    diag = await async_get_config_entry_diagnostics(hass, entree)

    assert diag["configuration"]["s3_endpoint"] == DONNEES["s3_endpoint"]
    assert diag["configuration"]["s3_bucket"] == DONNEES["s3_bucket"]
    assert diag["configuration"]["s3_region"] == DONNEES["s3_region"]
    assert diag["configuration"]["s3_prefixe"] == DONNEES["s3_prefixe"]
    assert diag["configuration"]["reddit_user_agent"] == DONNEES["reddit_user_agent"]
    assert diag["stockage_configure"] is True


async def test_le_diagnostic_porte_le_dernier_passage_et_le_journal(hass):
    session = _session_nominale(2)
    entree = await _monter(hass, session, "programming\ndevops\n")
    await _collecter(hass, session)

    diag = await async_get_config_entry_diagnostics(hass, entree)

    assert diag["dernier_passage"]["elements"] == 2
    assert diag["dernier_passage"]["depot"] == "envoye"
    assert len(diag["journal"]) == 1
    ligne = diag["journal"][0]
    assert ligne["resultat"] == "succes"
    assert ligne["sources_lues"] == 2
    assert ligne["muettes"] == []
    assert diag["passage_en_cours"] is False


async def test_le_journal_garde_une_ligne_par_passage(hass):
    entree = await _monter(hass, _session_nominale(1), "programming\n")
    for _ in range(3):
        await _collecter(hass, _session_nominale(1))

    diag = await async_get_config_entry_diagnostics(hass, entree)
    assert len(diag["journal"]) == 3
    assert [l["resultat"] for l in diag["journal"]] == ["succes"] * 3


async def test_le_journal_est_borne(hass):
    from custom_components.aliud_collecteur.const import JOURNAL_MAX

    entree = await _monter(hass, _session_nominale(1), "programming\n")
    with patch(
        "custom_components.aliud_collecteur.JOURNAL_MAX", 3
    ):
        for _ in range(5):
            await _collecter(hass, _session_nominale(1))
        diag = await async_get_config_entry_diagnostics(hass, entree)
        assert len(diag["journal"]) == 3

    assert JOURNAL_MAX == 20, "le plafond livré n'est pas celui du test"


async def test_une_source_muette_se_retrouve_dans_le_journal(hass):
    session = Session(
        Reponse(200, {"access_token": "j"}),
        Reponse(200, {"data": {"children": []}}),
        Reponse(404),
        Reponse(200),
        Reponse(200),
    )
    entree = await _monter(hass, session, "programming\nsupprime\n")
    await _collecter(hass, session)

    diag = await async_get_config_entry_diagnostics(hass, entree)
    assert diag["journal"][0]["muettes"] == ["supprime"]
    # Le motif reste sur le dernier passage, pas dans la série.
    assert diag["dernier_passage"]["sources_muettes"][0]["raison"]


async def test_le_diagnostic_compte_les_sources_et_les_releves(hass):
    session = _session_nominale(1)
    entree = await _monter(hass, session, "programming\ndevops\n# commenté\n")
    await _collecter(hass, session)

    diag = await async_get_config_entry_diagnostics(hass, entree)

    assert diag["sources"]["reddit"]["declarees"] == 2
    assert diag["sources"]["reddit"]["doublons"] == 0
    assert diag["releves_locaux"], "le relevé écrit doit se voir"
    assert diag["releves_locaux"][0]["nom"].startswith("reddit-")
    assert diag["releves_locaux"][0]["octets"] > 0
    assert Path(diag["sources"]["reddit"]["fichier"]).exists()

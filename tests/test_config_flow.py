"""Les deux écrans de saisie, et ce qu'ils refusent.

ON VÉRIFIE À LA SAISIE, PAS AU PREMIER PASSAGE
Une clé fausse rangée sans contrôle se découvre le lendemain matin, après une
nuit sans relevé. Les deux écrans font donc un appel réel, et ces tests fixent
la traduction de chaque refus en message.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aliud_collecteur.const import DOMAIN
from tests.faux_reseau import Reponse, Session

REDDIT = {
    "reddit_client_id": "ID",
    "reddit_client_secret": "SECRET",
    "reddit_user_agent": "aliud:collecteur:0.1.0 (by /u/board)",
}
STOCKAGE = {
    "s3_endpoint": "https://s3.example.net",
    "s3_region": "gra",
    "s3_bucket": "aliud-collecte",
    "s3_access_key": "AK",
    "s3_secret_key": "SK",
    "s3_prefixe": "",
}


def _session(*reponses):
    """Remplace la session partagée de Home Assistant dans le flux."""
    return patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        return_value=Session(*reponses),
    )


async def test_les_deux_ecrans_creent_l_entree(hass):
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert flux["step_id"] == "user"

    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    assert flux["step_id"] == "stockage"

    with _session(Reponse(200)), patch(
        "custom_components.aliud_collecteur.async_setup_entry", return_value=True
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], STOCKAGE)

    assert flux["type"] is FlowResultType.CREATE_ENTRY
    assert flux["data"] == {**REDDIT, **STOCKAGE}


@pytest.mark.parametrize(
    "reponse, attendu",
    [
        (Reponse(401, corps="Unauthorized"), "reddit_refuse"),
        (Reponse(200, {}), "reddit_refuse"),
        (Reponse(429, headers={"Retry-After": "5"}), "reddit_bride"),
    ],
)
async def test_reddit_refuse_reaffiche_l_ecran_avec_son_motif(hass, reponse, attendu):
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _session(reponse):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)

    assert flux["type"] is FlowResultType.FORM
    assert flux["step_id"] == "user"
    assert flux["errors"] == {"base": attendu}


async def test_reddit_injoignable_est_distingue_d_un_refus(hass):
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        side_effect=OSError("réseau coupé"),
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    assert flux["errors"] == {"base": "reddit_injoignable"}


@pytest.mark.parametrize("code", [403, 404, 500])
async def test_un_stockage_qui_refuse_reaffiche_le_second_ecran(hass, code):
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)

    with _session(Reponse(code)):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], STOCKAGE)

    assert flux["type"] is FlowResultType.FORM
    assert flux["step_id"] == "stockage"
    assert flux["errors"] == {"base": "stockage_refuse"}


async def test_une_seconde_entree_est_refusee(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert flux["type"] is FlowResultType.ABORT
    assert flux["reason"] == "already_configured"

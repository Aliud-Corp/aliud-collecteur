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

MEDIAS = {"medias": ["reddit"]}
REDDIT = {
    "reddit_client_id": "ID",
    "reddit_client_secret": "SECRET",
    "reddit_user_agent": "aliud:collecteur:0.1.0 (by /u/board)",
}

SANS_COOKIE = {"reddit_cookie": ""}
COOKIE_RANGE = {"reddit_cookie": "", "reddit_cookie_expire": ""}
STOCKAGE = {
    "s3_endpoint": "https://s3.example.net",
    "s3_region": "gra",
    "s3_bucket": "aliud-collecte",
    "s3_access_key": "AK",
    "s3_secret_key": "SK",
    "s3_prefixe": "",
}


async def _jusqu_a_reddit(hass):
    """Le flux jusqu'à l'écran des identifiants Reddit, médias cochés."""
    flux = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert flux["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(flux["flow_id"], MEDIAS)


async def _passer_les_cookies(hass, flux, saisie=None):
    """L'écran des cookies, franchi à vide sauf mention contraire."""
    assert flux["step_id"] in ("cookies", "reconfigure_cookies"), flux.get("step_id")
    return await hass.config_entries.flow.async_configure(
        flux["flow_id"], saisie if saisie is not None else SANS_COOKIE
    )


def _session(*reponses):
    """Remplace la session partagée de Home Assistant dans le flux."""
    return patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        return_value=Session(*reponses),
    )


async def test_les_deux_ecrans_creent_l_entree(hass):
    flux = await _jusqu_a_reddit(hass)
    assert flux["step_id"] == "reddit"

    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    flux = await _passer_les_cookies(hass, flux)
    assert flux["step_id"] == "stockage"

    with _session(Reponse(200)), patch(
        "custom_components.aliud_collecteur.async_setup_entry", return_value=True
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], STOCKAGE)

    assert flux["type"] is FlowResultType.CREATE_ENTRY
    assert flux["data"] == {**MEDIAS, **REDDIT, **COOKIE_RANGE, **STOCKAGE}


@pytest.mark.parametrize(
    "reponse, attendu",
    [
        (Reponse(401, corps="Unauthorized"), "reddit_refuse"),
        (Reponse(200, {}), "reddit_refuse"),
        (Reponse(429, headers={"Retry-After": "5"}), "reddit_bride"),
    ],
)
async def test_reddit_refuse_reaffiche_l_ecran_avec_son_motif(hass, reponse, attendu):
    flux = await _jusqu_a_reddit(hass)
    with _session(reponse):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)

    assert flux["type"] is FlowResultType.FORM
    assert flux["step_id"] == "reddit"
    assert flux["errors"] == {"base": attendu}


async def test_reddit_injoignable_est_distingue_d_un_refus(hass):
    flux = await _jusqu_a_reddit(hass)
    with patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        side_effect=OSError("réseau coupé"),
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    assert flux["errors"] == {"base": "reddit_injoignable"}


@pytest.mark.parametrize("code", [403, 404, 500])
async def test_un_stockage_qui_refuse_reaffiche_le_second_ecran(hass, code):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    flux = await _passer_les_cookies(hass, flux)

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


# ── Le stockage est facultatif ──────────────────────────────────────────────
#
# Un bucket se provisionne par une chaîne d'infrastructure qui a son propre
# rythme. Ces cas fixent ce qui doit rester possible en attendant.

async def test_un_ecran_de_stockage_vide_cree_quand_meme_l_entree(hass):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    flux = await _passer_les_cookies(hass, flux)

    vide = {c: "" for c in STOCKAGE}
    session = Session()  # aucune réponse posée : un appel lèverait
    with patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        return_value=session,
    ), patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], vide)

    assert flux["type"] is FlowResultType.CREATE_ENTRY
    assert session.appels == [], "un écran vide ne doit joindre personne"
    assert flux["data"]["reddit_client_id"] == "ID"
    assert flux["data"]["s3_bucket"] == ""


async def test_un_stockage_a_moitie_rempli_est_refuse_avant_tout_appel(hass):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)
    flux = await _passer_les_cookies(hass, flux)

    moitie = {**{c: "" for c in STOCKAGE}, "s3_endpoint": "https://s3.example.net",
              "s3_bucket": "aliud-collecte"}
    session = Session()
    with patch(
        "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
        return_value=session,
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], moitie)

    assert flux["type"] is FlowResultType.FORM
    assert flux["errors"] == {"base": "stockage_incomplet"}
    assert session.appels == []


# ── Reconfigurer : ajouter le stockage plus tard ────────────────────────────

async def _entree_sans_stockage(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={**MEDIAS, **REDDIT, **COOKIE_RANGE, **{c: "" for c in STOCKAGE}},
    )
    entree.add_to_hass(hass)
    return entree


async def _menu(hass, entree, branche):
    """Le menu de reconfiguration, puis la branche demandée."""
    flux = await entree.start_reconfigure_flow(hass)
    assert flux["type"] is FlowResultType.MENU, flux["type"]
    return await hass.config_entries.flow.async_configure(
        flux["flow_id"], {"next_step_id": branche}
    )


async def test_le_menu_n_offre_que_les_branches_qui_ont_un_sens(hass):
    entree = await _entree_sans_stockage(hass)
    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await entree.start_reconfigure_flow(hass)
    assert flux["type"] is FlowResultType.MENU
    # Reddit est coché : ses deux branches sont là. Le stockage l'est toujours.
    assert flux["menu_options"] == [
        "reconfigure_medias",
        "reconfigure_reddit",
        "reconfigure_cookies",
        "reconfigure_stockage",
    ]


async def test_sans_reddit_le_menu_n_offre_ni_reddit_ni_cookies(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={"medias": ["lobsters"], **{c: "" for c in STOCKAGE}},
    )
    entree.add_to_hass(hass)
    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await entree.start_reconfigure_flow(hass)
    assert flux["menu_options"] == ["reconfigure_medias", "reconfigure_stockage"]


async def test_le_stockage_se_regle_sans_retraverser_le_reste(hass):
    entree = await _entree_sans_stockage(hass)
    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await _menu(hass, entree, "reconfigure_stockage")
        assert flux["step_id"] == "reconfigure_stockage"
        with _session(Reponse(200)):
            flux = await hass.config_entries.flow.async_configure(
                flux["flow_id"], STOCKAGE
            )

    assert flux["reason"] == "reconfigure_successful"
    assert entree.data["s3_bucket"] == "aliud-collecte"
    # Ce que la branche ne doit surtout pas emporter. Un doublon de méthode
    # avait fait exactement ça : l'ancienne définition, héritée du couloir,
    # écrasait la neuve et repartait d'un dictionnaire vide.
    assert entree.data["reddit_user_agent"] == REDDIT["reddit_user_agent"]
    assert entree.data["medias"] == MEDIAS["medias"]


async def test_une_cle_secrete_laissee_vide_garde_celle_deja_rangee(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={**MEDIAS, **REDDIT, **COOKIE_RANGE, **STOCKAGE},
    )
    entree.add_to_hass(hass)

    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await _menu(hass, entree, "reconfigure_stockage")
        with _session(Reponse(200)):
            flux = await hass.config_entries.flow.async_configure(
                flux["flow_id"], {**STOCKAGE, "s3_secret_key": "", "s3_prefixe": "neuf"}
            )

    assert flux["reason"] == "reconfigure_successful"
    assert entree.data["s3_secret_key"] == "SK", "le secret ne devait pas être effacé"
    assert entree.data["s3_prefixe"] == "neuf"


async def test_vider_le_stockage_revient_au_disque_seul(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={**MEDIAS, **REDDIT, **COOKIE_RANGE, **STOCKAGE},
    )
    entree.add_to_hass(hass)

    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await _menu(hass, entree, "reconfigure_stockage")
        session = Session()
        with patch(
            "custom_components.aliud_collecteur.config_flow.async_get_clientsession",
            return_value=session,
        ):
            flux = await hass.config_entries.flow.async_configure(
                flux["flow_id"], {c: "" for c in STOCKAGE}
            )

    assert flux["reason"] == "reconfigure_successful"
    assert entree.data["s3_bucket"] == ""
    assert session.appels == []


async def test_les_medias_se_changent_sans_toucher_au_stockage(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={**MEDIAS, **REDDIT, **COOKIE_RANGE, **STOCKAGE},
    )
    entree.add_to_hass(hass)

    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await _menu(hass, entree, "reconfigure_medias")
        flux = await hass.config_entries.flow.async_configure(
            flux["flow_id"], {"medias": ["lobsters", "rss"]}
        )

    assert flux["reason"] == "reconfigure_successful"
    assert entree.data["medias"] == ["rss", "lobsters"], "remis dans l'ordre déclaré"
    assert entree.data["s3_bucket"] == "aliud-collecte"


# ── L'écran des cookies ─────────────────────────────────────────────────────
#
# Il existe parce qu'un cookie enterré dans l'écran des identifiants Reddit ne
# se trouve pas, et parce qu'un secret qui expire tout seul doit dire quand.

from datetime import datetime, timedelta, timezone  # noqa: E402

DANS_30_JOURS = (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
HIER = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()

SANS_CLIENT = {**REDDIT, "reddit_client_id": "", "reddit_client_secret": ""}


def _export(expire=DANS_30_JOURS):
    return (
        '[{"name":"reddit_session","value":"abc","domain":".reddit.com",'
        f'"expirationDate":{expire}}},'
        '{"name":"token_v2","value":"def","domain":".reddit.com",'
        f'"expirationDate":{expire}}}]'
    )


async def test_un_export_json_colle_devient_un_en_tete_et_sa_date(hass):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)

    flux = await _passer_les_cookies(hass, flux, {"reddit_cookie": _export()})
    with _session(Reponse(200)), patch(
        "custom_components.aliud_collecteur.async_setup_entry", return_value=True
    ):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], STOCKAGE)

    assert flux["data"]["reddit_cookie"] == "reddit_session=abc; token_v2=def"
    assert flux["data"]["reddit_cookie_expire"].startswith("20")


@pytest.mark.parametrize(
    "colle, attendu",
    [
        ("n'importe quoi", "cookie_illisible"),
        ('[{"name":"pref","value":"x","domain":".reddit.com"}]', "cookie_incomplet"),
    ],
)
async def test_un_cookie_de_travers_se_dit_a_la_saisie(hass, colle, attendu):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(flux["flow_id"], REDDIT)

    flux = await hass.config_entries.flow.async_configure(
        flux["flow_id"], {"reddit_cookie": colle}
    )
    assert flux["step_id"] == "cookies"
    assert flux["errors"] == {"base": attendu}


async def test_reddit_sans_client_ni_cookie_est_refuse_a_l_ecran(hass):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(
            flux["flow_id"], SANS_CLIENT
        )
    flux = await hass.config_entries.flow.async_configure(
        flux["flow_id"], {"reddit_cookie": ""}
    )
    assert flux["errors"] == {"base": "reddit_sans_porte"}


async def test_un_cookie_seul_suffit_a_ouvrir_reddit(hass):
    flux = await _jusqu_a_reddit(hass)
    with _session(Reponse(200, {"access_token": "j"})):
        flux = await hass.config_entries.flow.async_configure(
            flux["flow_id"], SANS_CLIENT
        )
    flux = await _passer_les_cookies(hass, flux, {"reddit_cookie": _export()})
    assert flux["step_id"] == "stockage"


# ── La réauthentification ───────────────────────────────────────────────────

async def _entree_avec_cookie(hass, expire_le=""):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entree = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            **MEDIAS,
            **SANS_CLIENT,
            "reddit_cookie": "reddit_session=abc; token_v2=def",
            "reddit_cookie_expire": expire_le,
            **{c: "" for c in STOCKAGE},
        },
    )
    entree.add_to_hass(hass)
    return entree


async def test_le_flux_de_reauthentification_remplace_le_cookie(hass):
    entree = await _entree_avec_cookie(hass)

    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await entree.start_reauth_flow(hass)
        assert flux["step_id"] == "reauth_confirm"
        flux = await hass.config_entries.flow.async_configure(
            flux["flow_id"], {"reddit_cookie": _export()}
        )

    assert flux["type"] is FlowResultType.ABORT
    assert flux["reason"] == "reauth_successful"
    assert entree.data["reddit_cookie"] == "reddit_session=abc; token_v2=def"
    assert entree.data["reddit_cookie_expire"].startswith("20")


async def test_la_reauthentification_dit_ou_en_est_la_session(hass):
    from custom_components.aliud_collecteur.cookies import lire

    entree = await _entree_avec_cookie(hass, lire(_export(HIER), "reddit").expire_le)
    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await entree.start_reauth_flow(hass)
    assert "expiré" in flux["description_placeholders"]["etat"]


async def test_la_reauthentification_refuse_de_laisser_reddit_sans_porte(hass):
    entree = await _entree_avec_cookie(hass)
    with patch("custom_components.aliud_collecteur.async_setup_entry", return_value=True):
        flux = await entree.start_reauth_flow(hass)
        flux = await hass.config_entries.flow.async_configure(
            flux["flow_id"], {"reddit_cookie": ""}
        )
    assert flux["errors"] == {"base": "reddit_sans_porte"}


def test_aucune_etape_du_flux_n_est_definie_deux_fois():
    """Une méthode redéfinie plus bas écrase la première, en silence.

    Écrit après coup : deux `async_step_reconfigure_stockage` cohabitaient, et
    Python gardait la seconde — l'ancienne, qui repartait d'un dictionnaire vide
    et effaçait tout le reste de l'entrée. Rien dans la syntaxe ne le signale.
    """
    import inspect
    import re

    from custom_components.aliud_collecteur import config_flow

    source = inspect.getsource(config_flow)
    noms = re.findall(r"\n    async def (async_step_\w+)", source)
    doublons = sorted({n for n in noms if noms.count(n) > 1})
    assert doublons == [], f"étapes définies deux fois : {doublons}"

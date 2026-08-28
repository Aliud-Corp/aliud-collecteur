"""La saisie des identifiants, et leur vérification avant d'être rangés.

POURQUOI PAS `secrets.yaml`
`secrets.yaml` est un fichier en clair qu'il faut éditer à la main à chaque
rotation de clé, et qui n'est lu qu'au démarrage. Le magasin de Home Assistant
est couvert par sa sauvegarde, se modifie depuis l'interface, et ne demande à
personne d'ouvrir un éditeur de texte pour changer une clé S3.

ON VÉRIFIE À LA SAISIE, PAS AU PREMIER PASSAGE
Les deux écrans font un appel réel : une poignée de main Reddit, un `HEAD` sur
le bucket. Une configuration fausse découverte au passage de 06:30 est une
configuration fausse découverte le lendemain matin, après une nuit sans relevé.

LE STOCKAGE EST FACULTATIF, ET CE N'EST PAS UNE COMMODITÉ
Un bucket se provisionne par une chaîne d'infrastructure qui a son propre
rythme, et le greffon doit pouvoir tourner avant. Le second écran se valide donc
à vide : la collecte a lieu, le relevé s'écrit sur le disque, et rien n'est
envoyé. Le stockage s'ajoute ensuite par « Reconfigurer », sans repasser par
Reddit ni perdre l'état de reprise.

Ce que ça n'est pas : un mode dégradé silencieux. Le capteur porte `depot` à
`non_configure`, parce qu'un capteur vert pendant quinze jours sans qu'un octet
soit parti est un échec qui a l'air d'un succès.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import depot_s3
from .collecteurs import PassageImpossible, TropDeRequetes
from .collecteurs.reddit import Reddit
from .const import (
    BUDGET_DEFAUT,
    CONF_REDDIT_CLIENT_ID,
    CONF_REDDIT_CLIENT_SECRET,
    CONF_REDDIT_USER_AGENT,
    CONF_S3_ACCESS_KEY,
    CONF_S3_BUCKET,
    CONF_S3_ENDPOINT,
    CONF_S3_PREFIXE,
    CONF_S3_REGION,
    CONF_S3_SECRET_KEY,
    DEBIT_DEFAUT,
    DOMAIN,
    FENETRE_DEFAUT,
    GARDER_BRUT_DEFAUT,
    GIGUE_MAX_DEFAUT,
    GIGUE_MIN_DEFAUT,
    HEURE_DEFAUT,
    MINUTE_DEFAUT,
    NOM,
    OPT_BUDGET,
    OPT_DEBIT,
    OPT_FENETRE,
    OPT_GARDER_BRUT,
    OPT_GIGUE_MAX,
    OPT_GIGUE_MIN,
    OPT_HEURE,
    OPT_MINUTE,
    OPT_PAR_SOURCE,
    OPT_RELEVES_GARDES,
    OPT_TENTATIVES,
    PAR_SOURCE_DEFAUT,
    RELEVES_GARDES_DEFAUT,
    TENTATIVES_DEFAUT,
)

_LOGGER = logging.getLogger(__name__)

_TEXTE = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

SCHEMA_REDDIT = vol.Schema(
    {
        vol.Required(CONF_REDDIT_CLIENT_ID): _TEXTE,
        vol.Required(CONF_REDDIT_CLIENT_SECRET): _SECRET,
        vol.Required(CONF_REDDIT_USER_AGENT): _TEXTE,
    }
)

# Tout est facultatif : l'écran se valide à vide tant qu'aucun bucket n'existe.
# Ce qui reste exigé est la cohérence — quatre champs sur cinq remplis est une
# saisie interrompue, pas une intention.
CHAMPS_STOCKAGE = (
    CONF_S3_ENDPOINT,
    CONF_S3_REGION,
    CONF_S3_BUCKET,
    CONF_S3_ACCESS_KEY,
    CONF_S3_SECRET_KEY,
    CONF_S3_PREFIXE,
)
INDISPENSABLES = (
    CONF_S3_ENDPOINT,
    CONF_S3_REGION,
    CONF_S3_BUCKET,
    CONF_S3_ACCESS_KEY,
    CONF_S3_SECRET_KEY,
)


def _schema_stockage(defauts: dict[str, Any] | None = None) -> vol.Schema:
    d = defauts or {}
    return vol.Schema(
        {
            vol.Optional(CONF_S3_ENDPOINT, default=d.get(CONF_S3_ENDPOINT, "")): _TEXTE,
            vol.Optional(CONF_S3_REGION, default=d.get(CONF_S3_REGION, "")): _TEXTE,
            vol.Optional(CONF_S3_BUCKET, default=d.get(CONF_S3_BUCKET, "")): _TEXTE,
            vol.Optional(CONF_S3_ACCESS_KEY, default=d.get(CONF_S3_ACCESS_KEY, "")): _TEXTE,
            vol.Optional(CONF_S3_SECRET_KEY, default=""): _SECRET,
            vol.Optional(CONF_S3_PREFIXE, default=d.get(CONF_S3_PREFIXE, "")): _TEXTE,
        }
    )


SCHEMA_STOCKAGE = _schema_stockage()


class FluxDeConfiguration(ConfigFlow, domain=DOMAIN):
    """Deux écrans : le média, puis le stockage."""

    VERSION = 1

    def __init__(self) -> None:
        self._donnees: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = await _essayer_reddit(self.hass, user_input)
            if erreur:
                erreurs["base"] = erreur
            else:
                self._donnees.update(user_input)
                return await self.async_step_stockage()

        return self.async_show_form(
            step_id="user", data_schema=SCHEMA_REDDIT, errors=erreurs
        )

    async def async_step_stockage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = await _valider_stockage(self.hass, user_input)
            if erreur:
                erreurs["base"] = erreur
            else:
                self._donnees.update(_nettoyer(user_input))
                return self.async_create_entry(title=NOM, data=self._donnees)

        return self.async_show_form(
            step_id="stockage",
            data_schema=SCHEMA_STOCKAGE,
            errors=erreurs,
            last_step=True,
        )

    # ── Reconfigurer : ajouter le stockage plus tard, ou tourner les clés ────
    #
    # Un flux de reconfiguration plutôt que des champs dans les options : ce
    # sont des identifiants, ils vivent dans `data` avec les autres, et ils se
    # vérifient par un appel réel avant d'être rangés. Les options portent des
    # réglages, pas des clés.

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = await _essayer_reddit(self.hass, user_input)
            if erreur:
                erreurs["base"] = erreur
            else:
                self._donnees = {**entree.data, **user_input}
                return await self.async_step_reconfigure_stockage()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                SCHEMA_REDDIT,
                {
                    CONF_REDDIT_CLIENT_ID: entree.data.get(CONF_REDDIT_CLIENT_ID, ""),
                    CONF_REDDIT_USER_AGENT: entree.data.get(CONF_REDDIT_USER_AGENT, ""),
                },
            ),
            errors=erreurs,
        )

    async def async_step_reconfigure_stockage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            # Le secret est le seul champ qui ne se réaffiche pas. Laissé vide
            # alors que le reste est rempli, il veut dire « garde celui d'avant »
            # — sinon rouvrir cet écran pour changer un préfixe obligerait à
            # ressaisir une clé qu'on n'a peut-être plus sous la main.
            #
            # Il ne se recopie que si un autre champ est rempli. Sinon vider
            # l'écran, qui est la façon de revenir au disque seul, remettrait
            # une clé toute seule et l'écran refuserait une saisie incomplète
            # que personne n'a faite.
            saisie = _nettoyer(user_input)
            autres = [
                c for c in INDISPENSABLES
                if c != CONF_S3_SECRET_KEY and saisie.get(c)
            ]
            if autres and not saisie.get(CONF_S3_SECRET_KEY):
                saisie[CONF_S3_SECRET_KEY] = entree.data.get(CONF_S3_SECRET_KEY, "")
            erreur = await _valider_stockage(self.hass, saisie)
            if erreur:
                erreurs["base"] = erreur
            else:
                return self.async_update_reload_and_abort(
                    entree, data={**self._donnees, **saisie}
                )

        return self.async_show_form(
            step_id="reconfigure_stockage",
            data_schema=_schema_stockage(entree.data),
            errors=erreurs,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return FluxDOptions()


class FluxDOptions(OptionsFlow):
    """L'heure, le débit, et ce qui borne un passage."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(OPT_HEURE, default=o.get(OPT_HEURE, HEURE_DEFAUT)): _nombre(0, 23),
                vol.Required(OPT_MINUTE, default=o.get(OPT_MINUTE, MINUTE_DEFAUT)): _nombre(0, 59),
                vol.Required(OPT_DEBIT, default=o.get(OPT_DEBIT, DEBIT_DEFAUT)): _nombre(1, 100),
                vol.Required(OPT_GIGUE_MIN, default=o.get(OPT_GIGUE_MIN, GIGUE_MIN_DEFAUT)): _nombre(0, 120, 0.1),
                vol.Required(OPT_GIGUE_MAX, default=o.get(OPT_GIGUE_MAX, GIGUE_MAX_DEFAUT)): _nombre(0, 120, 0.1),
                vol.Required(OPT_TENTATIVES, default=o.get(OPT_TENTATIVES, TENTATIVES_DEFAUT)): _nombre(1, 10),
                vol.Required(OPT_BUDGET, default=o.get(OPT_BUDGET, BUDGET_DEFAUT)): _nombre(60, 21600),
                vol.Required(OPT_PAR_SOURCE, default=o.get(OPT_PAR_SOURCE, PAR_SOURCE_DEFAUT)): _nombre(1, 100),
                vol.Required(OPT_FENETRE, default=o.get(OPT_FENETRE, FENETRE_DEFAUT)): SelectSelector(
                    SelectSelectorConfig(
                        options=["hour", "day", "week", "month"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(OPT_RELEVES_GARDES, default=o.get(OPT_RELEVES_GARDES, RELEVES_GARDES_DEFAUT)): _nombre(0, 90),
                vol.Required(OPT_GARDER_BRUT, default=o.get(OPT_GARDER_BRUT, GARDER_BRUT_DEFAUT)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


def _nombre(mini: float, maxi: float, pas: float = 1) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=mini, max=maxi, step=pas, mode=NumberSelectorMode.BOX)
    )


async def _essayer_reddit(hass: Any, saisie: dict[str, Any]) -> str | None:
    """Une poignée de main réelle. Rend la clé d'erreur, ou `None`."""
    collecteur = Reddit(
        client_id=saisie[CONF_REDDIT_CLIENT_ID].strip(),
        client_secret=saisie[CONF_REDDIT_CLIENT_SECRET].strip(),
        user_agent=saisie[CONF_REDDIT_USER_AGENT].strip(),
        noms=[],
    )
    try:
        await collecteur.ouvrir(async_get_clientsession(hass))
    except PassageImpossible as exc:
        _LOGGER.debug("aliud_collecteur : reddit refusé (%s)", exc)
        return "reddit_refuse"
    except TropDeRequetes:
        return "reddit_bride"
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("aliud_collecteur : reddit injoignable (%s)", exc)
        return "reddit_injoignable"
    return None


def _nettoyer(saisie: dict[str, Any]) -> dict[str, Any]:
    """Les champs de stockage, débarrassés de leurs espaces."""
    return {c: str(v).strip() for c, v in saisie.items()}


async def _valider_stockage(hass: Any, saisie: dict[str, Any]) -> str | None:
    """Vide, complet, ou refusé. Un écran vide est une réponse valable."""
    remplis = [c for c in INDISPENSABLES if str(saisie.get(c, "")).strip()]
    if not remplis:
        return None
    if len(remplis) < len(INDISPENSABLES):
        return "stockage_incomplet"
    return await _essayer_stockage(hass, saisie)


async def _essayer_stockage(hass: Any, saisie: dict[str, Any]) -> str | None:
    """Un `HEAD` sur le bucket. Rend la clé d'erreur, ou `None`."""
    stockage = depot_s3.Stockage(
        endpoint=str(saisie.get(CONF_S3_ENDPOINT, "")).strip(),
        region=str(saisie.get(CONF_S3_REGION, "")).strip(),
        bucket=str(saisie.get(CONF_S3_BUCKET, "")).strip(),
        access_key=str(saisie.get(CONF_S3_ACCESS_KEY, "")).strip(),
        secret_key=str(saisie.get(CONF_S3_SECRET_KEY, "")).strip(),
        prefixe=str(saisie.get(CONF_S3_PREFIXE, "")).strip(),
    )
    try:
        await depot_s3.verifier(async_get_clientsession(hass), stockage)
    except depot_s3.DepotRefuse as exc:
        _LOGGER.debug("aliud_collecteur : stockage refusé (%s)", exc)
        return "stockage_refuse"
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("aliud_collecteur : stockage injoignable (%s)", exc)
        return "stockage_injoignable"
    return None

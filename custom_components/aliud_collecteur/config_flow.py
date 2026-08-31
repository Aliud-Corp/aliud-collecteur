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

LE PREMIER ÉCRAN CHOISIT LES MÉDIAS, ET C'EST LUI QUI DÉCIDE DE LA SUITE
Trois des quatre médias n'ont besoin d'aucun identifiant. Reddit est le seul à
en exiger, et sa porte s'est refermée le 29/08/2026 : reddit.com refuse un client
anonyme au niveau réseau, `robots.txt` compris. L'écran des identifiants ne
s'affiche donc que si quelqu'un coche Reddit — sinon il demanderait une clé pour
un média qu'on ne lit pas.

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

from . import cookies, depot_s3
from .collecteurs import PassageImpossible, Source, SourceMuette, TropDeRequetes
from .collecteurs.reddit import Reddit
from .const import (
    BUDGET_DEFAUT,
    CONF_MEDIAS,
    CONF_REDDIT_CLIENT_ID,
    CONF_REDDIT_COOKIE,
    CONF_REDDIT_CLIENT_SECRET,
    CONF_REDDIT_USER_AGENT,
    CONF_S3_ACCESS_KEY,
    CONF_S3_BUCKET,
    CONF_S3_ENDPOINT,
    CONF_S3_PREFIXE,
    CONF_S3_REGION,
    CONF_S3_SECRET_KEY,
    DEBIT_DEFAUT,
    DISPOSITIONS,
    DISPOSITION_DEFAUT,
    DECALAGE_DEFAUT,
    DOMAIN,
    FENETRE_DEFAUT,
    FENETRE_JOURS_DEFAUT,
    GARDER_BRUT_DEFAUT,
    GIGUE_MAX_DEFAUT,
    GIGUE_MIN_DEFAUT,
    AGENT_PAR_DEFAUT,
    HEURE_DEFAUT,
    MEDIAS,
    MEDIAS_A_COOKIE,
    MEDIAS_SANS_IDENTIFIANTS,
    cle_cookie,
    cle_expiration,
    MINUTE_DEFAUT,
    NOM,
    OPT_AGENT,
    OPT_BUDGET,
    OPT_DEBIT,
    OPT_DISPOSITION,
    OPT_DECALAGE,
    OPT_FENETRE,
    OPT_FENETRE_JOURS,
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

SCHEMA_MEDIAS = vol.Schema(
    {
        vol.Required(CONF_MEDIAS, default=list(MEDIAS_SANS_IDENTIFIANTS)): SelectSelector(
            SelectSelectorConfig(
                options=list(MEDIAS),
                multiple=True,
                mode=SelectSelectorMode.LIST,
                translation_key="medias",
            )
        )
    }
)

# Deux portes, et l'agent est exigé dans les deux : Reddit bride un agent
# générique, et se déguiser en navigateur n'est pas ce que le board a autorisé.
SCHEMA_REDDIT = vol.Schema(
    {
        vol.Required(CONF_REDDIT_USER_AGENT): _TEXTE,
        vol.Optional(CONF_REDDIT_CLIENT_ID, default=""): _TEXTE,
        vol.Optional(CONF_REDDIT_CLIENT_SECRET, default=""): _SECRET,
    }
)

# Un champ multiligne : un export JSON fait quelques milliers de caractères, et
# une ligne unique en rendrait la relecture impossible.
_COLLE = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))


def _schema_cookies(medias: list[str]) -> vol.Schema:
    """Un champ par média qui sait lire un cookie, et rien pour les autres."""
    return vol.Schema(
        {
            vol.Optional(cle_cookie(m), default=""): _COLLE
            for m in MEDIAS_A_COOKIE
            if m in medias
        }
    )


def _valider_cookies(saisie: dict[str, Any], medias: list[str]) -> tuple[dict, str]:
    """Chaque saisie ramenée à son en-tête, avec sa date. Rend (valeurs, erreur).

    Un champ laissé vide efface le cookie rangé : c'est la façon de revenir au
    client enregistré, et elle ne doit pas demander un geste de plus.
    """
    valeurs: dict[str, Any] = {}
    for media in MEDIAS_A_COOKIE:
        if media not in medias:
            continue
        brut = str(saisie.get(cle_cookie(media), "")).strip()
        if not brut:
            valeurs[cle_cookie(media)] = ""
            valeurs[cle_expiration(media)] = ""
            continue
        try:
            cookie = cookies.lire(brut, media)
        except cookies.CookieIllisible:
            return {}, "cookie_illisible"
        if cookie.manquants:
            return {}, "cookie_incomplet"
        valeurs[cle_cookie(media)] = cookie.entete
        valeurs[cle_expiration(media)] = cookie.expire_le
    return valeurs, ""


def _sans_porte(donnees: dict[str, Any]) -> bool:
    """Reddit coché sans client enregistré ni cookie : rien ne pourra le lire."""
    if "reddit" not in (donnees.get(CONF_MEDIAS) or []):
        return False
    a_client = bool(
        str(donnees.get(CONF_REDDIT_CLIENT_ID, "")).strip()
        and str(donnees.get(CONF_REDDIT_CLIENT_SECRET, "")).strip()
    )
    return not a_client and not str(donnees.get(CONF_REDDIT_COOKIE, "")).strip()

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
    """Les médias, puis leurs identifiants s'il en faut, puis le stockage."""

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
            medias = _medias_retenus(user_input)
            if not medias:
                erreurs["base"] = "aucun_media"
            else:
                self._donnees[CONF_MEDIAS] = medias
                if "reddit" in medias:
                    return await self.async_step_reddit()
                return await self.async_step_stockage()

        return self.async_show_form(
            step_id="user", data_schema=SCHEMA_MEDIAS, errors=erreurs
        )

    async def async_step_reddit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = await _essayer_reddit(self.hass, user_input)
            if erreur:
                erreurs["base"] = erreur
            else:
                self._donnees.update(user_input)
                return await self.async_step_cookies()

        return self.async_show_form(
            step_id="reddit", data_schema=SCHEMA_REDDIT, errors=erreurs
        )

    async def async_step_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        medias = self._donnees.get(CONF_MEDIAS) or []
        if not any(m in medias for m in MEDIAS_A_COOKIE):
            return await self.async_step_stockage()

        erreurs: dict[str, str] = {}
        if user_input is not None:
            valeurs, erreur = _valider_cookies(user_input, medias)
            if erreur:
                erreurs["base"] = erreur
            else:
                candidat = {**self._donnees, **valeurs}
                if _sans_porte(candidat):
                    erreurs["base"] = "reddit_sans_porte"
                else:
                    self._donnees = candidat
                    return await self.async_step_stockage()

        return self.async_show_form(
            step_id="cookies", data_schema=_schema_cookies(medias), errors=erreurs
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

    # ── Reconfigurer : changer de médias, ajouter le stockage, tourner une clé ─
    #
    # Un flux de reconfiguration plutôt que des champs dans les options : ce
    # sont des identifiants et un choix de sources, ils vivent dans `data` avec
    # le reste, et les identifiants se vérifient par un appel réel avant d'être
    # rangés. Les options portent des réglages, pas des clés.

    # ── Reconfigurer : un menu, pas un couloir ──────────────────────────────
    #
    # Les quatre écrans étaient enchaînés : changer un bucket obligeait à
    # retraverser les médias, Reddit et les cookies. Un couloir se traverse une
    # fois à l'installation ; ensuite on vient pour un réglage précis, et
    # l'imposer tous les quatre est la meilleure façon de rendre le quatrième
    # introuvable.
    #
    # Chaque branche enregistre et rend la main. Rien n'est chaîné.

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        medias = list(entree.data.get(CONF_MEDIAS) or [])
        choix = ["reconfigure_medias"]
        if "reddit" in medias:
            choix.append("reconfigure_reddit")
        if any(m in medias for m in MEDIAS_A_COOKIE):
            choix.append("reconfigure_cookies")
        choix.append("reconfigure_stockage")
        return self.async_show_menu(step_id="reconfigure", menu_options=choix)

    async def async_step_reconfigure_medias(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            medias = _medias_retenus(user_input)
            if not medias:
                erreurs["base"] = "aucun_media"
            else:
                candidat = {**entree.data, CONF_MEDIAS: medias}
                if _sans_porte(candidat):
                    erreurs["base"] = "reddit_sans_porte"
                else:
                    return self.async_update_reload_and_abort(entree, data=candidat)

        return self.async_show_form(
            step_id="reconfigure_medias",
            data_schema=self.add_suggested_values_to_schema(
                SCHEMA_MEDIAS, {CONF_MEDIAS: entree.data.get(CONF_MEDIAS) or []}
            ),
            errors=erreurs,
        )

    async def async_step_reconfigure_reddit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = await _essayer_reddit(
                self.hass, {**entree.data, **user_input}
            )
            if erreur:
                erreurs["base"] = erreur
            else:
                candidat = {**entree.data, **user_input}
                if _sans_porte(candidat):
                    erreurs["base"] = "reddit_sans_porte"
                else:
                    return self.async_update_reload_and_abort(entree, data=candidat)

        return self.async_show_form(
            step_id="reconfigure_reddit",
            data_schema=self.add_suggested_values_to_schema(
                SCHEMA_REDDIT,
                {
                    CONF_REDDIT_CLIENT_ID: entree.data.get(CONF_REDDIT_CLIENT_ID, ""),
                    CONF_REDDIT_USER_AGENT: entree.data.get(CONF_REDDIT_USER_AGENT, ""),
                },
            ),
            errors=erreurs,
        )

    async def async_step_reconfigure_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        medias = list(entree.data.get(CONF_MEDIAS) or [])
        erreurs: dict[str, str] = {}
        if user_input is not None:
            valeurs, erreur = _valider_cookies(user_input, medias)
            if erreur:
                erreurs["base"] = erreur
            else:
                candidat = {**entree.data, **valeurs}
                if _sans_porte(candidat):
                    erreurs["base"] = "reddit_sans_porte"
                else:
                    return self.async_update_reload_and_abort(entree, data=candidat)

        return self.async_show_form(
            step_id="reconfigure_cookies",
            data_schema=_schema_cookies(medias),
            errors=erreurs,
            description_placeholders=_etat_des_cookies(entree.data, medias),
        )

    async def async_step_reconfigure_stockage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reconfigure_entry()
        erreurs: dict[str, str] = {}
        if user_input is not None:
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
                    entree, data={**entree.data, **saisie}
                )

        return self.async_show_form(
            step_id="reconfigure_stockage",
            data_schema=_schema_stockage(entree.data),
            errors=erreurs,
        )

    # ── Réauthentification : ce que Home Assistant déclenche tout seul ───────
    #
    # Quand une session tombe, l'intégration appelle `async_start_reauth`. Home
    # Assistant pose alors une carte « à reconfigurer » dans les paramètres et
    # amène ici. C'est le mécanisme natif, et c'est pour ça qu'on ne bricole pas
    # une notification à la main : celle-ci se ferme et s'oublie, la carte reste
    # tant que le cookie n'est pas remplacé.

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entree = self._get_reauth_entry()
        medias = list(entree.data.get(CONF_MEDIAS) or [])
        erreurs: dict[str, str] = {}
        if user_input is not None:
            valeurs, erreur = _valider_cookies(user_input, medias)
            if erreur:
                erreurs["base"] = erreur
            else:
                candidat = {**entree.data, **valeurs}
                if _sans_porte(candidat):
                    erreurs["base"] = "reddit_sans_porte"
                else:
                    return self.async_update_reload_and_abort(entree, data=candidat)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema_cookies(medias),
            errors=erreurs,
            description_placeholders=_etat_des_cookies(entree.data, medias),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return FluxDOptions()


def _etat_des_cookies(donnees: dict[str, Any], medias: list[str]) -> dict[str, str]:
    """Ce que l'écran affiche au-dessus des champs : où en est chaque session."""
    lignes = []
    for media in MEDIAS_A_COOKIE:
        if media not in medias:
            continue
        present = bool(str(donnees.get(cle_cookie(media), "")).strip())
        mot, jours = cookies.etat(str(donnees.get(cle_expiration(media), "")), present)
        detail = {
            "absent": "aucun cookie",
            "sans_date": "posé, sans date connue",
            "valide": f"valide encore {jours} jour(s)",
            "bientot": f"expire dans {jours} jour(s)",
            "expire": f"expiré depuis {abs(jours or 0)} jour(s)",
        }[mot]
        lignes.append(f"{media} : {detail}")
    return {"etat": " · ".join(lignes) or "—"}


def _medias_retenus(saisie: dict[str, Any]) -> list[str]:
    """Les médias cochés, remis dans l'ordre déclaré.

    L'ordre de la saisie est celui des clics ; celui de `MEDIAS` est celui dans
    lequel un passage les lit. Garder le premier ferait dépendre l'ordre de
    lecture de la façon dont quelqu'un a coché des cases.
    """
    choisis = set(saisie.get(CONF_MEDIAS) or [])
    return [m for m in MEDIAS if m in choisis]


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
                vol.Required(OPT_DECALAGE, default=o.get(OPT_DECALAGE, DECALAGE_DEFAUT)): _nombre(0, 30),
                vol.Required(OPT_FENETRE_JOURS, default=o.get(OPT_FENETRE_JOURS, FENETRE_JOURS_DEFAUT)): _nombre(1, 30),
                vol.Required(OPT_FENETRE, default=o.get(OPT_FENETRE, FENETRE_DEFAUT)): SelectSelector(
                    SelectSelectorConfig(
                        options=["hour", "day", "week", "month"],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(OPT_AGENT, default=o.get(OPT_AGENT) or AGENT_PAR_DEFAUT): _TEXTE,
                vol.Required(OPT_DISPOSITION, default=o.get(OPT_DISPOSITION, DISPOSITION_DEFAUT)): SelectSelector(
                    SelectSelectorConfig(
                        options=list(DISPOSITIONS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="disposition",
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
    """Un appel réel, par la porte que la saisie désigne. Rend la clé, ou `None`.

    En mode cookie, l'essai lit une source réelle : une poignée de main ne
    prouverait rien puisqu'il n'y en a pas, et un cookie périmé ne se distingue
    d'un cookie valide qu'en s'en servant.
    """
    ident = str(saisie.get(CONF_REDDIT_CLIENT_ID, "")).strip()
    secret = str(saisie.get(CONF_REDDIT_CLIENT_SECRET, "")).strip()
    cookie = str(saisie.get(CONF_REDDIT_COOKIE, "")).strip()
    if not cookie and not (ident and secret):
        # Rien à vérifier ici : l'écran suivant peut encore fournir un cookie.
        # C'est lui qui refuse un Reddit sans aucune porte, parce qu'il est le
        # seul à voir les deux.
        return None

    collecteur = Reddit(
        client_id=ident,
        client_secret=secret,
        user_agent=str(saisie.get(CONF_REDDIT_USER_AGENT, "")).strip(),
        noms=[],
        cookie=cookie,
        par_source=1,
    )
    try:
        session = async_get_clientsession(hass)
        contexte = await collecteur.ouvrir(session)
        if contexte.par_cookie:
            await collecteur.moissonner(
                session, contexte, Source(media="reddit", nom="programming")
            )
    except PassageImpossible as exc:
        _LOGGER.debug("aliud_collecteur : reddit refusé (%s)", exc)
        return "reddit_refuse"
    except SourceMuette as exc:
        _LOGGER.debug("aliud_collecteur : reddit muet à l'essai (%s)", exc)
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

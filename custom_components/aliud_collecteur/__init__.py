"""L'entrée de l'intégration : une horloge, un passage, un dépôt.

L'HORLOGE EST PORTÉE ICI, PAS PAR UNE AUTOMATION
Un passage garde un état entre deux exécutions — les sources qu'il n'a pas pu
lire repassent en tête la fois suivante. Une automation qui déclencherait ce
passage vivrait à côté de cet état, et le jour où quelqu'un la duplique ou la
déplace, la reprise se désynchronise sans rien dire. L'heure se règle dans les
options de l'intégration.

CE QUI SE PASSE QUAND LE DÉPÔT ÉCHOUE
Le relevé local est écrit avant l'envoi. Un `PUT` refusé laisse donc un fichier
complet sur le disque, et le passage suivant le reprend. Perdre une collecte de
cent sources parce qu'un point d'entrée S3 répond `503` serait payer trois
minutes de requêtes pour rien.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store

from . import depot_s3, releve
from .collecteurs import Source
from .collecteurs.reddit import SOURCES_PAR_DEFAUT, Reddit
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
    DOSSIER,
    FENETRE_DEFAUT,
    FICHIER_SOURCES,
    GARDER_BRUT_DEFAUT,
    GIGUE_MAX_DEFAUT,
    GIGUE_MIN_DEFAUT,
    HEURE_DEFAUT,
    MINUTE_DEFAUT,
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
    PLATEFORMES,
    RELEVES_GARDES_DEFAUT,
    RESULTAT_ECHEC,
    RESULTAT_PARTIEL,
    RESULTAT_SUCCES,
    SEUIL_DE_FREIN,
    SERVICE_COLLECTER,
    SIGNAL_PASSAGE,
    STOCKAGE_CLE,
    STOCKAGE_VERSION,
    TENTATIVES_DEFAUT,
)
from .ordonnanceur import Ordonnanceur, Reglages

_LOGGER = logging.getLogger(__name__)

SCHEMA_COLLECTER = vol.Schema(
    {
        vol.Optional("sources"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("limite"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("deposer", default=True): cv.boolean,
    }
)


@dataclass(slots=True)
class Bilan:
    """Ce qu'un passage laisse derrière lui, pour le capteur et pour le service."""

    media: str = "reddit"
    resultat: str = RESULTAT_ECHEC
    debut: str = ""
    fin: str = ""
    secondes: float = 0.0
    elements: int = 0
    sources_declarees: int = 0
    sources_lues: int = 0
    sources_muettes: list[dict[str, str]] = field(default_factory=list)
    sources_non_lues: list[str] = field(default_factory=list)
    complet: bool = False
    fichier: str = ""
    cle_s3: str = ""
    erreur: str | None = None


class Passeur:
    """Ce qui tient un passage de bout en bout, et son état entre deux."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.bilan = Bilan()
        self.en_cours = False
        self._store: Store = Store(hass, STOCKAGE_VERSION, f"{STOCKAGE_CLE}.{entry.entry_id}")
        self._etat: dict[str, Any] = {}
        self._defaire_horloge = None

    # ── Cycle de vie ────────────────────────────────────────────────────────

    async def demarrer(self) -> None:
        self._etat = await self._store.async_load() or {}
        bilan = self._etat.get("bilan")
        if bilan:
            self.bilan = Bilan(**{c: v for c, v in bilan.items() if c in Bilan.__slots__})
        self.armer()

    def armer(self) -> None:
        """(Re)pose l'horloge à l'heure des options."""
        if self._defaire_horloge is not None:
            self._defaire_horloge()
        options = self.entry.options
        self._defaire_horloge = async_track_time_change(
            self.hass,
            self._au_top,
            # Les sélecteurs numériques de Home Assistant rendent des
            # flottants ; `async_track_time_change` veut des entiers.
            hour=int(options.get(OPT_HEURE, HEURE_DEFAUT)),
            minute=int(options.get(OPT_MINUTE, MINUTE_DEFAUT)),
            second=0,
        )

    def arreter(self) -> None:
        if self._defaire_horloge is not None:
            self._defaire_horloge()
            self._defaire_horloge = None

    async def _au_top(self, _maintenant) -> None:
        await self.collecter()

    # ── Le passage ──────────────────────────────────────────────────────────

    async def collecter(
        self,
        sources: list[str] | None = None,
        limite: int | None = None,
        deposer: bool = True,
    ) -> Bilan:
        """Un passage complet. Ne lève jamais : le bilan porte l'échec."""
        if self.en_cours:
            _LOGGER.warning("aliud_collecteur : un passage est déjà en cours")
            return self.bilan
        self.en_cours = True
        try:
            return await self._collecter(sources, limite, deposer)
        finally:
            self.en_cours = False
            async_dispatcher_send(self.hass, SIGNAL_PASSAGE)

    async def _collecter(
        self, sources: list[str] | None, limite: int | None, deposer: bool
    ) -> Bilan:
        options = self.entry.options
        donnees = self.entry.data
        media = "reddit"

        noms = sources or await self.hass.async_add_executor_job(
            releve.lire_sources, self._chemin_sources(media), SOURCES_PAR_DEFAUT
        )
        if limite:
            noms = noms[:limite]

        collecteur = Reddit(
            client_id=donnees.get(CONF_REDDIT_CLIENT_ID, ""),
            client_secret=donnees.get(CONF_REDDIT_CLIENT_SECRET, ""),
            user_agent=donnees.get(CONF_REDDIT_USER_AGENT, ""),
            noms=noms,
            par_source=int(options.get(OPT_PAR_SOURCE, PAR_SOURCE_DEFAUT)),
            fenetre=options.get(OPT_FENETRE, FENETRE_DEFAUT),
        )

        ordonnanceur = Ordonnanceur(
            Reglages(
                debit_par_minute=options.get(OPT_DEBIT, DEBIT_DEFAUT),
                gigue_min=options.get(OPT_GIGUE_MIN, GIGUE_MIN_DEFAUT),
                gigue_max=options.get(OPT_GIGUE_MAX, GIGUE_MAX_DEFAUT),
                tentatives=options.get(OPT_TENTATIVES, TENTATIVES_DEFAUT),
                budget_secondes=options.get(OPT_BUDGET, BUDGET_DEFAUT),
                seuil_de_frein=SEUIL_DE_FREIN,
            )
        )

        session = async_get_clientsession(self.hass)
        resultat = await ordonnanceur.passage(
            collecteur, session, reprises=self._reprises(media)
        )

        contenu = releve.construire(
            resultat, media, garder_brut=options.get(OPT_GARDER_BRUT, GARDER_BRUT_DEFAUT)
        )
        octets = await self.hass.async_add_executor_job(releve.compresser, contenu)

        bilan = Bilan(
            media=media,
            debut=resultat.debut,
            fin=resultat.fin,
            secondes=resultat.secondes,
            elements=len(resultat.elements),
            sources_declarees=resultat.sources_declarees,
            sources_lues=len(resultat.sources_lues),
            sources_muettes=resultat.sources_muettes,
            sources_non_lues=resultat.sources_non_lues,
            complet=resultat.complet,
            erreur=resultat.erreur,
        )

        # Le disque d'abord : un dépôt refusé ne doit pas coûter la collecte.
        chemin = await self.hass.async_add_executor_job(
            releve.ecrire,
            self._dossier(),
            releve.nom_local(media, resultat.debut),
            octets,
        )
        bilan.fichier = str(chemin)
        await self.hass.async_add_executor_job(
            releve.elaguer,
            self._dossier(),
            media,
            int(options.get(OPT_RELEVES_GARDES, RELEVES_GARDES_DEFAUT)),
        )

        if deposer and resultat.erreur is None:
            try:
                bilan.cle_s3 = await self._deposer(session, media, resultat.debut, octets)
            except depot_s3.DepotRefuse as exc:
                bilan.erreur = f"dépôt refusé : {exc}"
                _LOGGER.error("aliud_collecteur : %s", bilan.erreur)
            except Exception as exc:  # noqa: BLE001
                bilan.erreur = f"dépôt impossible : {exc}"
                _LOGGER.error("aliud_collecteur : %s", bilan.erreur)

        bilan.resultat = _verdict(bilan)
        self.bilan = bilan
        await self._retenir(media, resultat.sources_non_lues, bilan)
        _LOGGER.info(
            "aliud_collecteur : %s — %d éléments, %d/%d sources, %.1f s, %s",
            media,
            bilan.elements,
            bilan.sources_lues,
            bilan.sources_declarees,
            bilan.secondes,
            bilan.resultat,
        )
        return bilan

    async def _deposer(
        self, session: Any, media: str, debut: str, octets: bytes
    ) -> str:
        stockage = self._stockage()
        cle = releve.cle_datee(media, debut)
        await depot_s3.deposer(
            session, stockage, cle, octets, "application/json", encodage="gzip"
        )
        await depot_s3.deposer(
            session,
            stockage,
            releve.cle_derniere(media),
            octets,
            "application/json",
            encodage="gzip",
        )
        return stockage.cle_complete(cle)

    # ── L'état gardé entre deux passages ────────────────────────────────────

    def _reprises(self, media: str) -> list[str]:
        return list((self._etat.get("reprises") or {}).get(media) or [])

    async def _retenir(self, media: str, non_lues: list[str], bilan: Bilan) -> None:
        reprises = dict(self._etat.get("reprises") or {})
        reprises[media] = [f"{media}:{nom}" for nom in non_lues]
        self._etat["reprises"] = reprises
        self._etat["bilan"] = {c: getattr(bilan, c) for c in Bilan.__slots__}
        await self._store.async_save(self._etat)

    # ── Chemins et configuration ────────────────────────────────────────────

    def _dossier(self) -> Path:
        return Path(self.hass.config.path(DOSSIER))

    def _chemin_sources(self, media: str) -> Path:
        return self._dossier() / FICHIER_SOURCES.format(media=media)

    def _stockage(self) -> depot_s3.Stockage:
        d = self.entry.data
        return depot_s3.Stockage(
            endpoint=d.get(CONF_S3_ENDPOINT, ""),
            region=d.get(CONF_S3_REGION, ""),
            bucket=d.get(CONF_S3_BUCKET, ""),
            access_key=d.get(CONF_S3_ACCESS_KEY, ""),
            secret_key=d.get(CONF_S3_SECRET_KEY, ""),
            prefixe=d.get(CONF_S3_PREFIXE, ""),
        )


def _verdict(bilan: Bilan) -> str:
    if bilan.erreur and bilan.elements == 0:
        return RESULTAT_ECHEC
    if bilan.complet and not bilan.erreur:
        return RESULTAT_SUCCES
    return RESULTAT_PARTIEL


# ── Ce que Home Assistant appelle ───────────────────────────────────────────

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    passeur = Passeur(hass, entry)
    await passeur.demarrer()
    entry.runtime_data = passeur

    await hass.config_entries.async_forward_entry_setups(entry, PLATEFORMES)
    entry.async_on_unload(entry.add_update_listener(_options_changees))
    _enregistrer_le_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    passeur: Passeur = entry.runtime_data
    passeur.arreter()
    decharge = await hass.config_entries.async_unload_platforms(entry, PLATEFORMES)
    if decharge and not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_COLLECTER)
    return decharge


async def _options_changees(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """L'heure a bougé, ou le débit. On repose l'horloge sans recharger l'entrée."""
    passeur: Passeur = entry.runtime_data
    passeur.armer()


def _enregistrer_le_service(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_COLLECTER):
        return

    async def _collecter(appel: ServiceCall) -> dict[str, Any]:
        entrees = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entrees:
            return {"erreur": "aucune entrée chargée"}
        passeur: Passeur = entrees[0].runtime_data
        bilan = await passeur.collecter(
            sources=appel.data.get("sources"),
            limite=appel.data.get("limite"),
            deposer=appel.data.get("deposer", True),
        )
        return {c: getattr(bilan, c) for c in Bilan.__slots__}

    hass.services.async_register(
        DOMAIN,
        SERVICE_COLLECTER,
        _collecter,
        schema=SCHEMA_COLLECTER,
        supports_response=SupportsResponse.OPTIONAL,
    )


# Importé pour que `Source` reste au contrat même si personne ne l'utilise ici.
_ = Source

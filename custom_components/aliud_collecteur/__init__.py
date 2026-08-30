"""L'entrée de l'intégration : une horloge, un passage, un dépôt.

L'HORLOGE EST PORTÉE ICI, PAS PAR UNE AUTOMATION
Un passage garde un état entre deux exécutions — les sources qu'il n'a pas pu
lire repassent en tête la fois suivante. Une automation qui déclencherait ce
passage vivrait à côté de cet état, et le jour où quelqu'un la duplique ou la
déplace, la reprise se désynchronise sans rien dire. L'heure se règle dans les
options de l'intégration.

LE DÉPÔT PEUT NE PAS EXISTER, ET LE PASSAGE A QUAND MÊME LIEU
Un bucket se provisionne par une chaîne d'infrastructure qui a son propre
rythme. Tant qu'il manque, la collecte tourne et le relevé s'écrit sur le
disque ; `depot` vaut `non_configure` et le capteur le porte. Ce n'est pas un
mode dégradé silencieux : quinze relevés qui n'ont jamais quitté la machine se
lisent sur l'écran, pas dans un journal.

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
from .collecteurs import REGISTRE, Source
from .collecteurs.arctic import SOURCES_PAR_DEFAUT as SOURCES_ARCTIC
from .collecteurs.arctic import ArcticShift
from .collecteurs.hackernews import SOURCES_PAR_DEFAUT as SOURCES_HN
from .collecteurs.hackernews import HackerNews
from .collecteurs.lobsters import SOURCES_PAR_DEFAUT as SOURCES_LOBSTERS
from .collecteurs.lobsters import Lobsters
from .collecteurs.reddit import SOURCES_PAR_DEFAUT as SOURCES_REDDIT
from .collecteurs.reddit import Reddit
from .collecteurs.rss import SOURCES_PAR_DEFAUT as SOURCES_RSS
from .collecteurs.rss import Rss
from .const import (
    AGENT_PAR_DEFAUT,
    BUDGET_DEFAUT,
    CONF_MEDIAS,
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
    DECALAGE_DEFAUT,
    DOMAIN,
    DOSSIER,
    FENETRE_DEFAUT,
    FENETRE_JOURS_DEFAUT,
    FICHIER_SOURCES,
    GARDER_BRUT_DEFAUT,
    GIGUE_MAX_DEFAUT,
    GIGUE_MIN_DEFAUT,
    HEURE_DEFAUT,
    MINUTE_DEFAUT,
    OPT_BUDGET,
    OPT_DEBIT,
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
    PLATEFORMES,
    RELEVES_GARDES_DEFAUT,
    DEPOT_DESACTIVE,
    DEPOT_ENVOYE,
    DEPOT_NON_CONFIGURE,
    DEPOT_REFUSE,
    RESULTAT_ECHEC,
    RESULTAT_PARTIEL,
    RESULTAT_SUCCES,
    SEUIL_DE_FREIN,
    SERVICE_COLLECTER,
    SIGNAL_PASSAGE,
    JOURNAL_MAX,
    MEDIAS,
    STOCKAGE_CLE,
    STOCKAGE_VERSION,
    TENTATIVES_DEFAUT,
)
from .ordonnanceur import Ordonnanceur, Reglages

_LOGGER = logging.getLogger(__name__)

SCHEMA_COLLECTER = vol.Schema(
    {
        vol.Optional("medias"): vol.All(cv.ensure_list, [vol.In(MEDIAS)]),
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
    depot: str = DEPOT_NON_CONFIGURE
    cle_s3: str = ""
    erreur: str | None = None

    def en_json(self) -> dict[str, Any]:
        return {c: getattr(self, c) for c in Bilan.__slots__}


class Passeur:
    """Ce qui tient un passage de bout en bout, et son état entre deux."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.bilans: dict[str, Bilan] = {}
        self.journal: list[dict[str, Any]] = []
        self.en_cours = False
        self._reprises_par_media: dict[str, list[str]] = {}
        self._store: Store = Store(hass, STOCKAGE_VERSION, f"{STOCKAGE_CLE}.{entry.entry_id}")
        self._etat: dict[str, Any] = {}
        self._defaire_horloge = None

    # ── Cycle de vie ────────────────────────────────────────────────────────

    async def demarrer(self) -> None:
        self._etat = await self._store.async_load() or {}
        for media, brut in (self._etat.get("bilans") or {}).items():
            self.bilans[media] = Bilan(
                **{c: v for c, v in brut.items() if c in Bilan.__slots__}
            )
        self._reprises_par_media = dict(self._etat.get("reprises") or {})
        self.journal = list(self._etat.get("journal") or [])
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
        medias: list[str] | None = None,
        sources: list[str] | None = None,
        limite: int | None = None,
        deposer: bool = True,
    ) -> dict[str, Any]:
        """Un passage sur chaque média configuré. Ne lève jamais.

        Les médias sont lus l'un après l'autre, jamais en parallèle : ils
        partagent le budget de temps et la session HTTP, et deux passages
        simultanés rendraient le rythme de chacun illisible dans le journal.
        """
        if self.en_cours:
            _LOGGER.warning("aliud_collecteur : un passage est déjà en cours")
            return self.resume()
        self.en_cours = True
        try:
            retenus = [m for m in (medias or self.medias) if m in REGISTRE]
            inconnus = [m for m in (medias or []) if m not in REGISTRE]
            for m in inconnus:
                _LOGGER.warning("aliud_collecteur : média inconnu, ignoré : %s", m)
            if not retenus:
                _LOGGER.warning("aliud_collecteur : aucun média configuré")
            for media in retenus:
                self.bilans[media] = await self._un_media(
                    media, sources, limite, deposer
                )
            await self._retenir()
            return self.resume()
        finally:
            self.en_cours = False
            async_dispatcher_send(self.hass, SIGNAL_PASSAGE)

    async def _un_media(
        self,
        media: str,
        sources: list[str] | None,
        limite: int | None,
        deposer: bool,
    ) -> Bilan:
        options = self.entry.options

        noms = sources or await self.hass.async_add_executor_job(
            releve.lire_sources,
            self._chemin_sources(media),
            _sources_par_defaut(media),
        )
        if limite:
            noms = noms[:limite]

        collecteur = self._collecteur(media, noms)
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

        bilan.depot = self._etat_du_depot(deposer, resultat.erreur)
        if bilan.depot == DEPOT_ENVOYE:
            try:
                bilan.cle_s3 = await self._deposer(session, media, resultat.debut, octets)
            except depot_s3.DepotRefuse as exc:
                bilan.depot = DEPOT_REFUSE
                bilan.erreur = f"dépôt refusé : {exc}"
                _LOGGER.error("aliud_collecteur : %s", bilan.erreur)
            except Exception as exc:  # noqa: BLE001
                bilan.depot = DEPOT_REFUSE
                bilan.erreur = f"dépôt impossible : {exc}"
                _LOGGER.error("aliud_collecteur : %s", bilan.erreur)

        bilan.resultat = _verdict(bilan)
        self._reprises_par_media[media] = [
            f"{media}:{nom}" for nom in resultat.sources_non_lues
        ]
        _LOGGER.info(
            "aliud_collecteur : %s — %d éléments, %d/%d sources, %.1f s, %s, dépôt %s",
            media,
            bilan.elements,
            bilan.sources_lues,
            bilan.sources_declarees,
            bilan.secondes,
            bilan.resultat,
            bilan.depot,
        )
        return bilan

    def _collecteur(self, media: str, noms: list[str]) -> Any:
        """Le collecteur du média, monté avec ce que sa classe demande.

        Chaque média prend ce qui le concerne et rien d'autre : Reddit ses
        identifiants, Arctic Shift sa fenêtre décalée, les deux autres leur seul
        agent. Un constructeur commun obligerait chacun à ignorer les champs des
        autres, ce qui est la façon la plus sûre d'en oublier un.
        """
        d = self.entry.data
        o = self.entry.options
        agent = d.get(CONF_REDDIT_USER_AGENT) or AGENT_PAR_DEFAUT
        par_source = int(o.get(OPT_PAR_SOURCE, PAR_SOURCE_DEFAUT))

        if media == "reddit":
            return Reddit(
                client_id=d.get(CONF_REDDIT_CLIENT_ID, ""),
                client_secret=d.get(CONF_REDDIT_CLIENT_SECRET, ""),
                user_agent=d.get(CONF_REDDIT_USER_AGENT, ""),
                noms=noms,
                par_source=par_source,
                fenetre=o.get(OPT_FENETRE, FENETRE_DEFAUT),
            )
        if media == "arctic":
            return ArcticShift(
                agent=agent,
                noms=noms,
                par_source=par_source,
                decalage_jours=int(o.get(OPT_DECALAGE, DECALAGE_DEFAUT)),
                fenetre_jours=int(o.get(OPT_FENETRE_JOURS, FENETRE_JOURS_DEFAUT)),
            )
        if media == "hackernews":
            return HackerNews(
                agent=agent,
                noms=noms,
                par_source=par_source,
                fenetre_jours=int(o.get(OPT_FENETRE_JOURS, FENETRE_JOURS_DEFAUT)),
            )
        if media == "lobsters":
            return Lobsters(agent=agent, noms=noms, par_source=par_source)
        if media == "rss":
            return Rss(agent=agent, noms=noms, par_source=par_source)
        raise ValueError(f"média sans constructeur : {media}")

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

    def _etat_du_depot(self, demande: bool, erreur: str | None) -> str:
        """Ce qui décide qu'un envoi est tenté, et le mot qui dit pourquoi non."""
        if not self._stockage_configure():
            return DEPOT_NON_CONFIGURE
        if not demande:
            return DEPOT_DESACTIVE
        if erreur is not None:
            # Le passage n'a rien pu ouvrir : déposer un relevé vide écraserait
            # `dernier.json.gz` avec un fichier qui ne dit rien.
            return DEPOT_DESACTIVE
        return DEPOT_ENVOYE

    def _stockage_configure(self) -> bool:
        d = self.entry.data
        return all(
            str(d.get(c, "")).strip()
            for c in (
                CONF_S3_ENDPOINT,
                CONF_S3_REGION,
                CONF_S3_BUCKET,
                CONF_S3_ACCESS_KEY,
                CONF_S3_SECRET_KEY,
            )
        )

    # ── L'état gardé entre deux passages ────────────────────────────────────

    @property
    def medias(self) -> list[str]:
        """Les médias configurés, dans l'ordre déclaré, jamais dans celui saisi."""
        choisis = set(self.entry.data.get(CONF_MEDIAS) or [])
        return [m for m in MEDIAS if m in choisis]

    @property
    def bilan(self) -> Bilan:
        """L'agrégat du dernier passage, ce que le capteur montre.

        Un capteur par média multiplierait les entités pour une question qu'on
        se pose une fois : est-ce que le passage de cette nuit a abouti. Le
        détail par média voyage en attribut.
        """
        return _agreger(list(self.bilans.values()))

    def resume(self) -> dict[str, Any]:
        """Ce que le service rend : l'agrégat, et le détail par média."""
        agrege = self.bilan
        return {
            **{c: getattr(agrege, c) for c in Bilan.__slots__},
            "medias": {m: b.en_json() for m, b in self.bilans.items()},
        }

    def _reprises(self, media: str) -> list[str]:
        return list(self._reprises_par_media.get(media) or [])

    async def _retenir(self) -> None:
        self._etat["reprises"] = dict(self._reprises_par_media)
        self._etat["bilans"] = {m: b.en_json() for m, b in self.bilans.items()}

        # Une ligne par média et par passage, bornée. Ce qu'on veut savoir d'un
        # collecteur n'est pas ce qui vient d'arriver mais la série : combien de
        # passages ont abouti cette semaine, quelle source se tait toujours. Le
        # dernier bilan seul ne le dit pas.
        lignes = [_ligne_de_journal(b) for b in self.bilans.values()]
        self.journal = [*self.journal, *lignes][-JOURNAL_MAX:]
        self._etat["journal"] = self.journal
        await self._store.async_save(self._etat)

    # ── Ce que les diagnostics lisent ───────────────────────────────────────

    @property
    def stockage_configure(self) -> bool:
        return self._stockage_configure()

    @property
    def reprises_en_attente(self) -> dict[str, list[str]]:
        return dict(self._reprises_par_media)

    def bilan_en_json(self) -> dict[str, Any]:
        return self.resume()

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


SEVERITE_RESULTAT = {RESULTAT_SUCCES: 0, RESULTAT_PARTIEL: 1, RESULTAT_ECHEC: 2}
SEVERITE_DEPOT = {
    DEPOT_ENVOYE: 0,
    DEPOT_DESACTIVE: 1,
    DEPOT_NON_CONFIGURE: 2,
    DEPOT_REFUSE: 3,
}


def _sources_par_defaut(media: str) -> str:
    """La liste livrée d'un média, écrite au premier passage puis jamais relue.

    Arctic Shift hérite de celle de Reddit : ce qui a changé est la porte, pas
    les sujets.
    """
    return {
        "reddit": SOURCES_REDDIT,
        "arctic": SOURCES_ARCTIC,
        "hackernews": SOURCES_HN,
        "lobsters": SOURCES_LOBSTERS,
        "rss": SOURCES_RSS,
    }.get(media, "")


def _agreger(bilans: list[Bilan]) -> Bilan:
    """Le passage vu d'un seul coup d'œil, et il prend toujours le pire.

    Un média qui a échoué ne doit pas être noyé par deux qui ont abouti : ce
    qu'on lit sur un capteur vert est « tout va bien », et ce serait faux. Les
    sources muettes et non lues gardent le nom de leur média, sans quoi
    « supprime » ne dirait pas chez qui.
    """
    if not bilans:
        return Bilan(media="")
    pire_resultat = max(bilans, key=lambda b: SEVERITE_RESULTAT.get(b.resultat, 2))
    pire_depot = max(bilans, key=lambda b: SEVERITE_DEPOT.get(b.depot, 2))
    erreurs = [f"{b.media} : {b.erreur}" for b in bilans if b.erreur]
    return Bilan(
        media=", ".join(b.media for b in bilans),
        resultat=pire_resultat.resultat,
        depot=pire_depot.depot,
        debut=min((b.debut for b in bilans if b.debut), default=""),
        fin=max((b.fin for b in bilans if b.fin), default=""),
        secondes=round(sum(b.secondes for b in bilans), 2),
        elements=sum(b.elements for b in bilans),
        sources_declarees=sum(b.sources_declarees for b in bilans),
        sources_lues=sum(b.sources_lues for b in bilans),
        sources_muettes=[
            {**m, "media": b.media} for b in bilans for m in b.sources_muettes
        ],
        sources_non_lues=[
            f"{b.media}:{nom}" for b in bilans for nom in b.sources_non_lues
        ],
        complet=all(b.complet for b in bilans),
        erreur=" · ".join(erreurs) or None,
    )


def _ligne_de_journal(bilan: Bilan) -> dict[str, Any]:
    """Le passage réduit à ce qui se compare d'une ligne à l'autre.

    Les sources muettes gardent leur nom mais perdent leur motif : sur vingt
    lignes, ce qu'on cherche est laquelle se tait toujours, pas ce qu'elle a
    répondu ce jour-là. Le motif du dernier passage reste dans `dernier_passage`.
    """
    return {
        "debut": bilan.debut,
        "secondes": bilan.secondes,
        "resultat": bilan.resultat,
        "depot": bilan.depot,
        "elements": bilan.elements,
        "sources_lues": bilan.sources_lues,
        "sources_declarees": bilan.sources_declarees,
        "muettes": [m["source"] for m in bilan.sources_muettes],
        "non_lues": list(bilan.sources_non_lues),
        "erreur": bilan.erreur,
    }


def _verdict(bilan: Bilan) -> str:
    """`succes` veut dire que le passage a fait tout ce qu'on lui demandait.

    Un stockage absent n'est pas un échec du passage : personne ne lui a demandé
    d'envoyer quoi que ce soit. Ce que ça vaut se lit dans `depot`, et le capteur
    le porte — c'est là que quinze relevés restés sur la machine se voient.
    """
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
        return await passeur.collecter(
            medias=appel.data.get("medias"),
            sources=appel.data.get("sources"),
            limite=appel.data.get("limite"),
            deposer=appel.data.get("deposer", True),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_COLLECTER,
        _collecter,
        schema=SCHEMA_COLLECTER,
        supports_response=SupportsResponse.OPTIONAL,
    )


# Importé pour que `Source` reste au contrat même si personne ne l'utilise ici.
_ = Source

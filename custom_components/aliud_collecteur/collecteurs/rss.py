"""N'importe quel flux Atom ou RSS, sans dépendance.

POURQUOI CE COLLECTEUR VAUT PLUS QUE LES AUTRES RÉUNIS
Aucune porte ne se ferme sur un flux : le publier est le geste par lequel un
site dit « lisez-moi en automatique ». C'est la seule source de ce greffon dont
personne ne peut décider un matin qu'elle exige un client enregistré.

POURQUOI PAS `feedparser`
Il ferait le travail en une ligne et il pèse une dépendance dans un
`manifest.json` qui n'en a aucune. Ce qu'on lui demande — deux dialectes, cinq
champs — tient dans `xml.etree` de la bibliothèque standard.

CE QUE COÛTE `xml.etree`, ET COMMENT C'EST BORNÉ
Il n'est pas durci contre un document hostile : un flux qui déclare des entités
imbriquées peut faire exploser la mémoire de l'analyseur, et un flux vient d'un
tiers. La charge est donc lue par morceaux et refusée au-delà de `OCTETS_MAX`,
avant d'atteindre l'analyseur. Ça ne couvre pas tout ce que `defusedxml`
couvrirait ; ça couvre ce qui coûte cher ici, et c'est écrit plutôt que supposé.

UN FLUX N'A NI SCORE NI COMMENTAIRES
Les deux valent zéro, et c'est exact : un billet n'est pas classé par ses
lecteurs. Un plancher de score posé sur une source RSS la rendrait muette — le
collecteur refuse donc ce réglage plutôt que de vider silencieusement la source.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from . import Element, Moisson, Source, SourceMuette, TropDeRequetes, enregistrer

_LOGGER = logging.getLogger(__name__)

# Deux mégaoctets : le plus gros flux sérieux qu'on ait relevé en fait deux cents
# fois moins. Ce plafond n'est pas un réglage de confort, c'est la borne qui
# protège l'analyseur.
OCTETS_MAX = 2 * 1024 * 1024

# Les racines qu'un flux peut porter. Le contrôle n'est pas du zèle : une page
# d'erreur HTML est du XML parfaitement valide, et sans lui elle rendrait zéro
# élément en silence — un trou qu'une archive ne saurait pas relire.
RACINES = ("feed", "rss", "RDF")

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
SLASH = "{http://purl.org/rss/1.0/modules/slash/}"

SOURCES_PAR_DEFAUT = """\
# Une ligne par flux. Deux formes :
#   <url>
#   <nom> <url>      le nom sert d'étiquette dans le relevé
#
# Un flux n'a pas de score : un plancher posé sur une de ces lignes rendrait la
# source muette, et le collecteur le refuse au lieu de la vider en silence.
simonwillison https://simonwillison.net/atom/everything/
lwn https://lwn.net/headlines/newrss
phoronix https://www.phoronix.com/rss.php
ovh-status https://public-cloud.status-ovhcloud.com/history.rss
"""


@dataclass(slots=True)
class Contexte:
    agent: str


@enregistrer
class Rss:
    """Un flux, une requête. Atom et RSS 2.0, rien d'autre."""

    media = "rss"

    def __init__(self, agent: str, noms: list[str], par_source: int = 25) -> None:
        self._agent = agent or "aliud-collecteur"
        self._noms = noms
        self._par_source = max(1, int(par_source))

    def sources(self) -> list[Source]:
        sorties = []
        for ligne in self._noms:
            nom, url = _decouper(ligne)
            if url:
                sorties.append(Source(media=self.media, nom=nom, options={"url": url}))
        return sorties

    async def ouvrir(self, session: Any) -> Contexte:
        return Contexte(agent=self._agent)

    async def moissonner(
        self, session: Any, contexte: Contexte, source: Source
    ) -> Moisson:
        url = source.options.get("url", "")
        if not url:
            raise SourceMuette(f"rss : {source.nom} n'a pas d'adresse")

        async with session.get(
            url,
            headers={
                "User-Agent": contexte.agent,
                "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9",
            },
        ) as reponse:
            if reponse.status == 429:
                raise TropDeRequetes(
                    f"rss : {source.nom} bridé",
                    attente=_flottant(reponse.headers.get("Retry-After")),
                )
            if reponse.status >= 500:
                raise TropDeRequetes(f"rss : {source.nom} a rendu {reponse.status}")
            if reponse.status != 200:
                raise SourceMuette(f"rss : {source.nom} a rendu {reponse.status}")
            brut = await _lire_borne(reponse, source.nom)

        collecte_le = datetime.now(timezone.utc).isoformat(timespec="seconds")
        elements = _analyser(brut, source.nom, url, collecte_le)
        return Moisson(elements=elements[: self._par_source])


async def _lire_borne(reponse: Any, nom: str) -> bytes:
    """La charge, refusée avant l'analyseur si elle dépasse le plafond."""
    morceaux: list[bytes] = []
    total = 0
    async for morceau in reponse.content.iter_chunked(65536):
        total += len(morceau)
        if total > OCTETS_MAX:
            raise SourceMuette(
                f"rss : {nom} dépasse {OCTETS_MAX} octets, refusé avant analyse"
            )
        morceaux.append(morceau)
    return b"".join(morceaux)


def _decouper(ligne: str) -> tuple[str, str]:
    """`nom url` ou `url` seule. Le nom déduit de l'hôte quand il manque."""
    parties = ligne.split()
    if len(parties) >= 2 and not parties[0].lower().startswith(("http://", "https://")):
        return parties[0], parties[1]
    url = parties[0] if parties else ""
    hote = urlparse(url).netloc.removeprefix("www.")
    return (hote or url), url


def _analyser(brut: bytes, source: str, url_flux: str, collecte_le: str) -> list[Element]:
    try:
        racine = ET.fromstring(brut)
    except ET.ParseError as exc:
        raise SourceMuette(f"rss : {source} n'est pas du XML lisible ({exc})") from exc

    balise = racine.tag.rpartition("}")[2]
    if balise not in RACINES:
        raise SourceMuette(
            f"rss : {source} n'est pas un flux — racine <{balise}>, "
            f"attendu {' ou '.join(RACINES)}"
        )

    entrees = racine.findall(f".//{ATOM}entry") or racine.findall(".//item")
    elements = []
    for entree in entrees:
        element = _element(entree, source, url_flux, collecte_le)
        if element is not None:
            elements.append(element)
    if not elements and entrees:
        raise SourceMuette(f"rss : {source} n'a rendu aucune entrée exploitable")
    return elements


def _element(
    entree: ET.Element, source: str, url_flux: str, collecte_le: str
) -> Element | None:
    titre = _texte(entree, f"{ATOM}title", "title")
    lien = _lien(entree)
    identifiant = _texte(entree, f"{ATOM}id", "guid") or lien
    if not identifiant or not (titre or lien):
        return None
    return Element(
        media="rss",
        source=source,
        identifiant=identifiant,
        titre=titre,
        # Une entrée sans lien renvoie au flux : mieux qu'une chaîne vide dans
        # une archive qu'on relira sans le contexte d'aujourd'hui.
        url=lien or url_flux,
        permalien=lien or url_flux,
        auteur=_auteur(entree),
        # Un flux ne classe pas. Zéro est la valeur exacte, pas un défaut.
        points=0,
        commentaires=_entier(_texte(entree, f"{SLASH}comments", f"{SLASH}comments")),
        cree_le=_date(entree),
        collecte_le=collecte_le,
        brut={"xml": ET.tostring(entree, encoding="unicode")[:4000]},
    )


def _lien(entree: ET.Element) -> str:
    for lien in entree.findall(f"{ATOM}link"):
        if lien.get("rel", "alternate") == "alternate" and lien.get("href"):
            return lien.get("href", "")
    direct = entree.find("link")
    if direct is not None and (direct.text or "").strip():
        return (direct.text or "").strip()
    if direct is not None and direct.get("href"):
        return direct.get("href", "")
    return ""


def _auteur(entree: ET.Element) -> str:
    auteur = entree.find(f"{ATOM}author")
    if auteur is not None:
        nom = auteur.find(f"{ATOM}name")
        if nom is not None and (nom.text or "").strip():
            return (nom.text or "").strip()
    return _texte(entree, f"{DC}creator", "author")


def _date(entree: ET.Element) -> str:
    """Atom d'abord, RSS ensuite. Les deux ramenés en UTC."""
    for balise in (f"{ATOM}published", f"{ATOM}updated"):
        brut = _texte(entree, balise, balise)
        if brut:
            try:
                return datetime.fromisoformat(brut.replace("Z", "+00:00")).astimezone(
                    timezone.utc
                ).isoformat(timespec="seconds")
            except ValueError:
                continue
    for balise in ("pubDate", f"{DC}date"):
        brut = _texte(entree, balise, balise)
        if brut:
            try:
                return parsedate_to_datetime(brut).astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                )
            except (TypeError, ValueError):
                try:
                    return datetime.fromisoformat(
                        brut.replace("Z", "+00:00")
                    ).astimezone(timezone.utc).isoformat(timespec="seconds")
                except ValueError:
                    continue
    return ""


def _texte(entree: ET.Element, *balises: str) -> str:
    for balise in balises:
        noeud = entree.find(balise)
        if noeud is not None and (noeud.text or "").strip():
            return (noeud.text or "").strip()
    return ""


def _entier(valeur: str) -> int:
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return 0


def _flottant(valeur: Any) -> float | None:
    try:
        return float(valeur)
    except (TypeError, ValueError):
        return None

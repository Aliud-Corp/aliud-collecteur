"""Le relevé d'un passage : ce qu'il contient, et où il se pose.

DATÉ, JAMAIS ÉCRASÉ
Un objet par passage, sous une clé qui porte l'instant du départ. C'est ce que
« archive brute » veut dire : un passage partiel et sa complétion du lendemain
sont deux fichiers honnêtes, pas un fichier réécrit dont personne ne sait plus
ce qu'il contenait la veille.

L'EN-TÊTE DIT CE QUE LE CORPS NE PEUT PAS DIRE
Un tableau d'éléments ne dit pas si trente sources se sont tues. Le bloc
`passage` porte le compte des sources déclarées, lues, muettes et non lues, avec
leurs noms. Sans lui, un fichier de quarante sources se lit comme un fichier
complet de quarante sources.

`dernier.json.gz` EST UNE COMMODITÉ, PAS LA SOURCE
Une copie sous une clé fixe, pour qu'un lecteur en aval n'ait pas à lister un
préfixe pour trouver le passage le plus récent. Elle s'écrase à chaque passage ;
l'archive, elle, est la série des clés datées.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime
from pathlib import Path

from .const import SCHEMA_RELEVE
from .ordonnanceur import Resultat

_LOGGER = logging.getLogger(__name__)


def construire(resultat: Resultat, media: str, garder_brut: bool = True) -> dict:
    """Le relevé complet, prêt à être sérialisé."""
    return {
        "schema": SCHEMA_RELEVE,
        "media": media,
        "passage": {
            "debut": resultat.debut,
            "fin": resultat.fin,
            "secondes": resultat.secondes,
            "complet": resultat.complet,
            "erreur": resultat.erreur,
            "sources_declarees": resultat.sources_declarees,
            "sources_lues": len(resultat.sources_lues),
            "sources_muettes": resultat.sources_muettes,
            "sources_non_lues": resultat.sources_non_lues,
            "reprises_du_passage_precedent": resultat.reprises,
        },
        "elements": [e.en_json(avec_brut=garder_brut) for e in resultat.elements],
    }


def compresser(releve: dict) -> bytes:
    """JSON compact puis gzip. `mtime=0` pour que deux relevés identiques le soient."""
    brut = json.dumps(releve, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(brut, compresslevel=9, mtime=0)


def horodatage(debut: str) -> datetime:
    """L'instant de départ, relu depuis le relevé plutôt que recalculé."""
    return datetime.fromisoformat(debut)


def cle_datee(media: str, debut: str) -> str:
    """`reddit/2026/08/28/reddit-20260828T063012Z.json.gz`"""
    quand = horodatage(debut)
    return (
        f"{media}/{quand:%Y/%m/%d}/{media}-{quand:%Y%m%dT%H%M%SZ}.json.gz"
    )


def cle_derniere(media: str) -> str:
    return f"{media}/dernier.json.gz"


def nom_local(media: str, debut: str) -> str:
    return f"{media}-{horodatage(debut):%Y%m%dT%H%M%SZ}.json.gz"


# ── Ce qui touche le disque. Appelé depuis un exécuteur, jamais la boucle. ───

def ecrire(dossier: Path, nom: str, contenu: bytes) -> Path:
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / nom
    provisoire = chemin.with_suffix(chemin.suffix + ".partiel")
    provisoire.write_bytes(contenu)
    provisoire.replace(chemin)
    return chemin


def elaguer(dossier: Path, media: str, garder: int) -> list[str]:
    """Ne garde que les `garder` relevés les plus récents d'un média.

    Le tri est sur le nom, qui porte l'horodatage en ISO compact : il trie
    comme la date. Trier sur `mtime` mentirait après une copie de sauvegarde.
    """
    if garder <= 0 or not dossier.is_dir():
        return []
    fichiers = sorted(dossier.glob(f"{media}-*.json.gz"), key=lambda p: p.name)
    supprimes = []
    for chemin in fichiers[:-garder] if len(fichiers) > garder else []:
        try:
            chemin.unlink()
            supprimes.append(chemin.name)
        except OSError as exc:  # noqa: PERF203 — un fichier verrouillé n'arrête rien
            _LOGGER.warning("aliud_collecteur : %s non supprimé (%s)", chemin, exc)
    return supprimes


def lire_sources(chemin: Path, defaut: str) -> list[str]:
    """La liste des sources, écrite au premier passage puis jamais réécrite.

    La liste appartient au board : elle s'édite dans un fichier texte, une
    source par ligne, `#` pour commenter. Cent entrées dans un champ de
    configuration ne s'éditent pas.
    """
    if not chemin.exists():
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(defaut, encoding="utf-8")
    lignes = chemin.read_text(encoding="utf-8", errors="replace").splitlines()
    vus: set[str] = set()
    sources: list[str] = []
    for ligne in lignes:
        nom = ligne.split("#", 1)[0].strip().lstrip("/").removeprefix("r/")
        if nom and nom not in vus:
            vus.add(nom)
            sources.append(nom)
    return sources

"""Ce qu'un navigateur exporte, ramené à un en-tête `Cookie`.

POURQUOI TROIS FORMATS ET PAS UN
Ce que quelqu'un a sous la main dépend de l'extension qu'il a installée.
Cookie-Editor rend un tableau JSON, « Get cookies.txt » rend le format Netscape,
et l'inspecteur du navigateur rend la ligne d'en-tête brute. Exiger un format
précis, c'est demander une conversion à la main avant de coller — donc une faute
de frappe dans un secret qu'on ne peut pas relire pour la trouver.

CE QUI SORT D'ICI EST TOUJOURS LA MÊME CHOSE
Une chaîne `nom=valeur; nom=valeur`, celle que porte l'en-tête. Le reste — les
domaines, les drapeaux, les dates — sert à deux choses et pas une de plus :
écarter les cookies d'un autre site collés par mégarde, et dire quand la session
expire.

L'EXPIRATION EST LA SEULE CHOSE QUI RENDE CE SECRET GÉRABLE
Un jeton d'API meurt quand on le révoque. Une session meurt toute seule, à une
date que l'export connaît et que l'en-tête brut ignore. Quand elle est là, on la
garde : c'est ce qui permet de prévenir avant la panne plutôt qu'après.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# Les cookies que Reddit exige pour reconnaître une session. Le reste de
# l'export — préférences d'affichage, mesure d'audience — voyage pour rien et
# grossit un secret qu'on range.
ESSENTIELS = {
    "reddit": ("reddit_session", "token_v2"),
    "x": ("auth_token", "ct0"),
}

DOMAINES = {
    "reddit": ("reddit.com",),
    "x": ("x.com", "twitter.com"),
}


class CookieIllisible(Exception):
    """Ce qui a été collé n'est aucun des trois formats connus."""


@dataclass(slots=True)
class Cookie:
    """Un en-tête prêt à envoyer, et ce qu'on sait de sa durée de vie."""

    entete: str
    noms: list[str] = field(default_factory=list)
    expire_le: str = ""
    manquants: list[str] = field(default_factory=list)

    @property
    def vide(self) -> bool:
        return not self.entete

    @property
    def jours_restants(self) -> int | None:
        if not self.expire_le:
            return None
        try:
            fin = datetime.fromisoformat(self.expire_le)
        except ValueError:
            return None
        return (fin - datetime.now(timezone.utc)).days


def lire(brut: str, media: str = "") -> Cookie:
    """Un des trois formats, ramené à l'en-tête. Lève `CookieIllisible`.

    Le média sert à écarter ce qui vient d'ailleurs et à dire ce qui manque. Sans
    lui, tout ce qui est lisible est gardé.
    """
    brut = (brut or "").strip()
    if not brut:
        return Cookie(entete="")

    if brut.startswith(("[", "{")):
        paires = _depuis_json(brut, media)
    elif "\t" in brut or brut.lstrip().startswith("# Netscape"):
        paires = _depuis_netscape(brut, media)
    else:
        paires = _depuis_entete(brut)

    if not paires:
        raise CookieIllisible(
            "aucun cookie lisible. Attendu : la ligne d'en-tête "
            "« nom=valeur; nom=valeur », un export JSON d'extension, ou un "
            "fichier cookies.txt"
        )

    noms = [nom for nom, _, _ in paires]
    essentiels = ESSENTIELS.get(media, ())
    return Cookie(
        entete="; ".join(f"{nom}={valeur}" for nom, valeur, _ in paires),
        noms=noms,
        expire_le=_plus_proche(paires, essentiels),
        manquants=[nom for nom in essentiels if nom not in noms],
    )


def _depuis_entete(brut: str) -> list[tuple[str, str, float | None]]:
    # Une ligne collée depuis l'inspecteur commence parfois par « Cookie: ».
    brut = brut.split(":", 1)[1] if brut.lower().startswith("cookie:") else brut
    paires = []
    for morceau in brut.split(";"):
        nom, separe, valeur = morceau.strip().partition("=")
        if separe and nom.strip():
            paires.append((nom.strip(), valeur.strip(), None))
    return paires


def _depuis_json(brut: str, media: str) -> list[tuple[str, str, float | None]]:
    try:
        charge = json.loads(brut)
    except json.JSONDecodeError as exc:
        raise CookieIllisible(f"JSON illisible ({exc})") from exc
    if isinstance(charge, dict):
        charge = charge.get("cookies") or [charge]
    if not isinstance(charge, list):
        raise CookieIllisible("JSON lisible mais ce n'est pas une liste de cookies")

    paires = []
    for entree in charge:
        if not isinstance(entree, dict):
            continue
        nom, valeur = entree.get("name"), entree.get("value")
        if not nom or valeur is None:
            continue
        if not _du_bon_domaine(str(entree.get("domain", "")), media):
            continue
        paires.append((str(nom), str(valeur), _flottant(entree.get("expirationDate"))))
    return paires


def _depuis_netscape(brut: str, media: str) -> list[tuple[str, str, float | None]]:
    paires = []
    for ligne in brut.splitlines():
        if not ligne.strip() or ligne.lstrip().startswith("#"):
            continue
        champs = ligne.split("\t")
        if len(champs) < 7:
            continue
        domaine, _, _, _, expire, nom, valeur = champs[:7]
        if not nom.strip() or not _du_bon_domaine(domaine, media):
            continue
        paires.append((nom.strip(), valeur.strip(), _flottant(expire)))
    return paires


def _du_bon_domaine(domaine: str, media: str) -> bool:
    """Sans média déclaré, tout passe. Avec, un cookie d'ailleurs est écarté.

    Coller l'export d'un autre onglet est l'erreur la plus facile à faire et la
    plus pénible à diagnostiquer : le secret a l'air rempli et la session n'est
    reconnue nulle part.
    """
    attendus = DOMAINES.get(media)
    if not attendus:
        return True
    domaine = domaine.lstrip(".").lower()
    return any(domaine == a or domaine.endswith(f".{a}") for a in attendus)


def _plus_proche(
    paires: list[tuple[str, str, float | None]], essentiels: tuple[str, ...]
) -> str:
    """La première expiration parmi les cookies qui comptent.

    Parmi les essentiels quand on sait lesquels ils sont : un cookie de
    préférence d'affichage qui meurt demain ne dit rien de la session.
    """
    retenus = [
        e for nom, _, e in paires
        if e and (not essentiels or nom in essentiels)
    ]
    if not retenus:
        return ""
    return datetime.fromtimestamp(min(retenus), tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _flottant(valeur: object) -> float | None:
    try:
        nombre = float(valeur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Le format Netscape écrit 0 pour « à la fermeture du navigateur ».
    return nombre if nombre > 0 else None


def etat(expire_le: str, present: bool) -> tuple[str, int | None]:
    """Le mot que l'écran montre, et les jours qui restent.

    Séparé de `Cookie` parce qu'il se calcule à la lecture de la configuration,
    des mois après la saisie : ce qui est rangé est la date, pas le verdict.
    """
    from .const import (
        COOKIE_ABSENT,
        COOKIE_ALERTE_JOURS,
        COOKIE_BIENTOT,
        COOKIE_EXPIRE,
        COOKIE_SANS_DATE,
        COOKIE_VALIDE,
    )

    if not present:
        return COOKIE_ABSENT, None
    if not expire_le:
        # Une session collée en en-tête brut ne porte pas sa date. On ne la
        # déclare pas valide pour autant : on dit qu'on ne sait pas.
        return COOKIE_SANS_DATE, None
    try:
        fin = datetime.fromisoformat(expire_le)
    except ValueError:
        return COOKIE_SANS_DATE, None
    jours = (fin - datetime.now(timezone.utc)).days
    if jours < 0:
        return COOKIE_EXPIRE, jours
    if jours <= COOKIE_ALERTE_JOURS:
        return COOKIE_BIENTOT, jours
    return COOKIE_VALIDE, jours

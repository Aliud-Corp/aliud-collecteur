"""Le contrat qu'un collecteur remplit, et la seule chose que l'ordonnanceur sait.

POURQUOI CE FICHIER EXISTE AVANT LE PREMIER COLLECTEUR
Reddit est le premier média, il ne sera pas le seul. Écrire l'ordonnanceur
autour de Reddit puis « l'ouvrir » plus tard, c'est se garantir que le second
média arrivera par une série de `if media == ...`. Le contrat est donc posé
d'abord, et `reddit.py` est déjà un cas particulier de quelque chose.

CE QUE L'ORDONNANCEUR CONNAÎT, ET RIEN DE PLUS
Une liste de sources, une façon d'ouvrir un passage, une façon de moissonner une
source. Il ne sait pas ce qu'est un jeton OAuth, ni qu'un sous-reddit se lit par
`/top`. Tout ce qui est propre à un média vit dans son fichier.

CE QU'UNE MOISSON REND, ET POURQUOI CE N'EST PAS JUSTE UNE LISTE
Une source qui sait combien de requêtes il lui reste avant sa fenêtre de remise
à zéro le dit dans ses en-têtes. Cette information est la seule qui permette de
ralentir *avant* d'être refusé, donc elle voyage avec les éléments plutôt que
d'être lue par l'ordonnanceur dans une réponse HTTP qu'il n'a pas à connaître.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Source:
    """Une cible déclarée : ce que l'ordonnanceur met dans sa file."""

    media: str
    nom: str
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def cle(self) -> str:
        """L'identifiant stable d'une source, celui qui sert à reprendre."""
        return f"{self.media}:{self.nom}"

    @property
    def plancher(self) -> int:
        """Le score en dessous duquel une publication n'entre pas.

        Zéro par défaut, et zéro veut dire « tout entre » : l'archive est brute,
        et un filtre par défaut contredirait ce qu'elle promet. C'est un réglage
        qu'on pose source par source, pas un comportement.
        """
        return int(self.options.get("plancher") or 0)


@dataclass(slots=True)
class Element:
    """Une publication, réduite à ce qui vaut pour tous les médias.

    `brut` garde la charge d'origine. L'archive est brute au sens propre : le
    jour où une question demande un champ que ce dataclass n'a pas, il est
    encore dans le fichier. Il est écarté du relevé par une option, jamais par
    défaut, parce qu'une archive amputée ne se répare pas rétroactivement.
    """

    media: str
    source: str
    identifiant: str
    titre: str
    url: str
    permalien: str
    auteur: str
    points: int
    commentaires: int
    cree_le: str
    collecte_le: str
    brut: dict[str, Any] = field(default_factory=dict)

    def en_json(self, avec_brut: bool = True) -> dict[str, Any]:
        sortie: dict[str, Any] = {
            "media": self.media,
            "source": self.source,
            "id": self.identifiant,
            "titre": self.titre,
            "url": self.url,
            "permalien": self.permalien,
            "auteur": self.auteur,
            "points": self.points,
            "commentaires": self.commentaires,
            "cree_le": self.cree_le,
            "collecte_le": self.collecte_le,
        }
        if avec_brut and self.brut:
            sortie["brut"] = self.brut
        return sortie


@dataclass(slots=True)
class Moisson:
    """Ce qu'une source rend : ses éléments, et ce qu'elle a dit de son débit.

    `restant` et `remise_a_zero` sont facultatifs. Un média qui ne publie pas
    ces chiffres laisse l'ordonnanceur sur son intervalle de base, ce qui est le
    comportement prudent.
    """

    elements: list[Element]
    restant: int | None = None
    remise_a_zero: float | None = None


class TropDeRequetes(Exception):
    """La source a refusé pour cause de débit. Se réessaie.

    `attente` porte les secondes que la source a demandées quand elle l'a dit
    (`Retry-After`). Absente, l'ordonnanceur applique son propre repli.
    """

    def __init__(self, message: str = "", attente: float | None = None) -> None:
        super().__init__(message or "trop de requêtes")
        self.attente = attente


class SourceMuette(Exception):
    """La source a échoué d'une façon qui ne se réessaie pas dans ce passage.

    Un sous-reddit privé, supprimé, ou mal orthographié. Réessayer coûterait
    trois requêtes du budget pour le même refus.
    """


class PassageImpossible(Exception):
    """Rien de ce passage ne peut aboutir : identifiants absents ou refusés.

    Distinct de `SourceMuette` : ici aucune source ne répondra, donc le passage
    s'arrête au lieu de brûler cent requêtes pour cent échecs identiques.
    """


@runtime_checkable
class Collecteur(Protocol):
    """Ce qu'un média doit savoir faire pour entrer dans l'ordonnanceur."""

    media: str

    def sources(self) -> list[Source]:
        """Les cibles déclarées, dans l'ordre où elles seront lues."""
        ...

    async def ouvrir(self, session: Any) -> Any:
        """Ce qui dure un passage : un jeton, des en-têtes, une session signée.

        Rendu tel quel à chaque `moissonner`. Lève `PassageImpossible` quand la
        configuration manque, avant la première requête de collecte.
        """
        ...

    async def moissonner(self, session: Any, contexte: Any, source: Source) -> Moisson:
        """Une source, une requête. Lève `TropDeRequetes` ou `SourceMuette`."""
        ...


# Le séparateur du plancher, et ce n'est pas `:` — il est déjà pris par les
# formes `q:<termes>` de Hacker News et `t:<etiquette>` de Lobsters. Une
# grammaire qui se marche dessus produit une source muette que personne ne
# rattache à une faute de frappe.
SEPARATEUR_PLANCHER = "@"


def decouper_plancher(ligne: str) -> tuple[str, int]:
    """`programming@200` rend `("programming", 200)`, `programming` rend `(…, 0)`.

    Un plancher illisible est ignoré plutôt que refusé : une source vaut mieux
    lue sans son filtre que pas lue du tout.
    """
    nom, separe, brut = ligne.rpartition(SEPARATEUR_PLANCHER)
    if not separe:
        return ligne.strip(), 0
    try:
        return nom.strip(), max(0, int(brut.strip()))
    except ValueError:
        return ligne.strip(), 0


REGISTRE: dict[str, type] = {}


def enregistrer(classe: type) -> type:
    """Range un collecteur sous le nom de son média.

    Décorateur plutôt qu'une table écrite à la main : la table et le fichier
    divergeraient le jour où l'un des deux serait renommé.
    """
    REGISTRE[classe.media] = classe
    return classe

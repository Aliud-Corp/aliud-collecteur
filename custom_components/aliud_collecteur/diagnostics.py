"""Ce que « Télécharger les diagnostics » rend, et ce qu'il masque.

POURQUOI CE FICHIER PLUTÔT QUE LE JOURNAL DE HOME ASSISTANT
Le journal dit ce qui vient de se passer, à condition d'y être au bon moment et
de connaître le mot à filtrer. Ce qu'on veut savoir d'un collecteur est autre :
combien de passages ont réussi cette semaine, quelles sources se taisent
toujours, et si quelque chose est parti vers le stockage. Ça ne se lit pas dans
une ligne de journal, ça se lit dans une série.

CE QUI EST MASQUÉ, ET CE QUI NE L'EST PAS
Les deux secrets et les deux identifiants partent en `**masqué**`. Le point
d'entrée, la région, le bucket et le préfixe restent lisibles : c'est ce qu'on
regarde en premier quand un dépôt échoue, et un fichier de diagnostic qui masque
la moitié du problème oblige à en demander un second.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_REDDIT_CLIENT_ID,
    CONF_REDDIT_CLIENT_SECRET,
    CONF_REDDIT_COOKIE,
    CONF_S3_ACCESS_KEY,
    CONF_S3_SECRET_KEY,
    DOSSIER,
)

A_MASQUER = {
    CONF_REDDIT_CLIENT_ID,
    CONF_REDDIT_CLIENT_SECRET,
    CONF_REDDIT_COOKIE,
    CONF_REDDIT_COOKIE,
    CONF_S3_ACCESS_KEY,
    CONF_S3_SECRET_KEY,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    passeur = entry.runtime_data
    dossier = Path(hass.config.path(DOSSIER))

    releves = await hass.async_add_executor_job(_releves, dossier)
    sources = await hass.async_add_executor_job(_sources, dossier)

    return {
        "configuration": async_redact_data(dict(entry.data), A_MASQUER),
        "options": dict(entry.options),
        "stockage_configure": passeur.stockage_configure,
        "cookies": passeur.cookies,
        "passage_en_cours": passeur.en_cours,
        "dernier_passage": passeur.bilan_en_json(),
        "journal": passeur.journal,
        "reprises_en_attente": passeur.reprises_en_attente,
        "sources": sources,
        "releves_locaux": releves,
    }


def _releves(dossier: Path) -> list[dict[str, Any]]:
    """Ce qui est sur le disque, du plus récent au plus ancien."""
    if not dossier.is_dir():
        return []
    fichiers = sorted(dossier.glob("*.json.gz"), key=lambda p: p.name, reverse=True)
    return [
        {"nom": f.name, "octets": f.stat().st_size}
        for f in fichiers[:20]
    ]


def _sources(dossier: Path) -> dict[str, Any]:
    """Le compte des sources déclarées, pas la liste : cent lignes n'aident pas."""
    sortie: dict[str, Any] = {}
    for fichier in sorted(dossier.glob("sources-*.txt")) if dossier.is_dir() else []:
        lignes = [
            l.split("#", 1)[0].strip()
            for l in fichier.read_text(encoding="utf-8", errors="replace").splitlines()
        ]
        retenues = [l for l in lignes if l]
        sortie[fichier.stem.removeprefix("sources-")] = {
            "fichier": str(fichier),
            "declarees": len(retenues),
            "doublons": len(retenues) - len(set(retenues)),
        }
    return sortie

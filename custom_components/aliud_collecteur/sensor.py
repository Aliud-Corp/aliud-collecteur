"""L'état du dernier passage, visible sans ouvrir un journal.

CE QUE CE CAPTEUR DIT, ET CE QU'IL NE DIT PAS
Il dit si le dernier passage a été complet, partiel ou raté, et il porte en
attributs ce qui explique un « partiel » : les sources muettes avec leur raison,
et les sources que le budget n'a pas laissé lire. Il ne dit pas si l'archive
servira à quelque chose — c'est en aval que ça se juge.

UN CAPTEUR VERT PENDANT QUINZE JOURS AVEC QUINZE RELEVÉS VIDES EST UN ÉCHEC,
pas un succès de l'horloge. D'où `elements` en attribut : un relevé complet de
zéro élément se voit.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NOM, RESULTAT_ECHEC, SIGNAL_PASSAGE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, ajouter: AddEntitiesCallback
) -> None:
    passeur = entry.runtime_data
    ajouter([CapteurDePassage(passeur, entry), CapteurDeDate(passeur, entry)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, passeur, entry: ConfigEntry) -> None:
        self._passeur = passeur
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NOM,
            manufacturer="Aliud",
            entry_type="service",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PASSAGE, self._rafraichir)
        )

    @callback
    def _rafraichir(self) -> None:
        self.async_write_ha_state()


class CapteurDePassage(_Base):
    """`succes`, `partiel` ou `echec`, et le détail en attributs."""

    _attr_translation_key = "passage"
    _attr_icon = "mdi:download-network"

    def __init__(self, passeur, entry: ConfigEntry) -> None:
        super().__init__(passeur, entry)
        self._attr_unique_id = f"{entry.entry_id}_passage"

    @property
    def native_value(self) -> str:
        return self._passeur.bilan.resultat or RESULTAT_ECHEC

    @property
    def extra_state_attributes(self) -> dict:
        b = self._passeur.bilan
        return {
            "media": b.media,
            "elements": b.elements,
            "sources_declarees": b.sources_declarees,
            "sources_lues": b.sources_lues,
            "sources_muettes": b.sources_muettes,
            "sources_non_lues": b.sources_non_lues,
            "complet": b.complet,
            "secondes": b.secondes,
            "fichier": b.fichier,
            "depot": b.depot,
            "cle_s3": b.cle_s3,
            "erreur": b.erreur,
            "en_cours": self._passeur.en_cours,
        }


class CapteurDeDate(_Base):
    """L'instant de fin du dernier passage."""

    _attr_translation_key = "dernier_passage"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, passeur, entry: ConfigEntry) -> None:
        super().__init__(passeur, entry)
        self._attr_unique_id = f"{entry.entry_id}_dernier_passage"

    @property
    def native_value(self):
        fin = self._passeur.bilan.fin
        return dt_util.parse_datetime(fin) if fin else None

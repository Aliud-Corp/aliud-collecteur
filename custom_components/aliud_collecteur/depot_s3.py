"""Un `PUT` signé, et rien de plus.

POURQUOI PAS BOTO3
`manifest.json` le déclarerait en une ligne, et Home Assistant l'installerait.
Trois raisons de ne pas le faire : il pèse plusieurs dizaines de mégaoctets sur
une installation qui tourne souvent sur carte SD, son import coûte une seconde
au démarrage de l'intégration, et il est synchrone — il faudrait le pousser dans
un exécuteur pour ne pas bloquer la boucle de Home Assistant. Ce qu'on lui
demande ici tient en un `PUT` : quatre-vingts lignes de `hmac` et `hashlib`, sur
l'`aiohttp` que Home Assistant embarque déjà.

CHEMIN PLUTÔT QUE SOUS-DOMAINE
L'URL est `{endpoint}/{bucket}/{clé}`. Le style « hôte virtuel »
(`{bucket}.{endpoint}/{clé}`) demande un certificat générique et une résolution
DNS par bucket ; le style chemin marche partout, y compris devant un point
d'entrée que personne n'a encore vérifié.

CE QUI N'EST PAS VÉRIFIÉ TANT QU'UN 200 N'EST PAS REVENU
Le point d'entrée exact de l'Object Storage OVH est une valeur de configuration,
pas une constante de ce fichier. Il se lit dans la console OVH, sur le bucket.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

_LOGGER = logging.getLogger(__name__)

ALGORITHME = "AWS4-HMAC-SHA256"
SERVICE = "s3"
CHARGE_VIDE = hashlib.sha256(b"").hexdigest()


class DepotRefuse(Exception):
    """Le stockage a refusé. Porte le code et le début du corps."""


@dataclass(slots=True)
class Stockage:
    """Où le relevé va, et avec quelles clés."""

    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefixe: str = ""

    def __post_init__(self) -> None:
        self.endpoint = _normaliser(self.endpoint)
        self.prefixe = self.prefixe.strip("/")

    def cle_complete(self, cle: str) -> str:
        cle = cle.lstrip("/")
        return f"{self.prefixe}/{cle}" if self.prefixe else cle

    def url(self, cle: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{_encoder(self.cle_complete(cle))}"


def _normaliser(endpoint: str) -> str:
    endpoint = (endpoint or "").strip().rstrip("/")
    if endpoint and "://" not in endpoint:
        endpoint = f"https://{endpoint}"
    return endpoint


def _encoder(chemin: str) -> str:
    """Encodage d'URI qui garde les séparateurs de la clé.

    `~` est dans `safe` parce que SigV4 exige qu'il ne soit pas encodé, et
    `quote` l'encode par défaut sur les versions anciennes de Python.
    """
    return quote(chemin, safe="/~")


async def deposer(
    session: Any,
    stockage: Stockage,
    cle: str,
    corps: bytes,
    type_contenu: str = "application/json",
    encodage: str | None = None,
    horodatage: datetime | None = None,
) -> str:
    """Écrit un objet et rend son URL. Lève `DepotRefuse` sur tout autre code."""
    url = stockage.url(cle)
    entetes = _entetes(
        stockage,
        methode="PUT",
        url=url,
        corps=corps,
        supplementaires=_sans_vide(
            {"content-type": type_contenu, "content-encoding": encodage}
        ),
        horodatage=horodatage,
    )
    async with session.put(url, data=corps, headers=entetes) as reponse:
        if reponse.status not in (200, 201):
            detail = (await reponse.text())[:300]
            raise DepotRefuse(
                f"{reponse.status} sur {stockage.bucket}/{stockage.cle_complete(cle)} : {detail}"
            )
    return url


async def verifier(session: Any, stockage: Stockage) -> None:
    """Un `HEAD` sur le bucket, pour que le config flow échoue à la saisie.

    Une configuration fausse découverte au premier passage nocturne est une
    configuration fausse découverte le lendemain matin.
    """
    if not stockage.endpoint or not stockage.bucket:
        raise DepotRefuse("point d'entrée ou bucket absent")
    url = f"{stockage.endpoint}/{stockage.bucket}"
    entetes = _entetes(stockage, methode="HEAD", url=url, corps=b"")
    async with session.head(url, headers=entetes) as reponse:
        if reponse.status == 404:
            raise DepotRefuse(f"le bucket {stockage.bucket} n'existe pas")
        if reponse.status in (401, 403):
            raise DepotRefuse("clés refusées par le stockage")
        if reponse.status >= 400:
            raise DepotRefuse(f"le stockage a rendu {reponse.status}")


# ── La signature ────────────────────────────────────────────────────────────

def _entetes(
    stockage: Stockage,
    methode: str,
    url: str,
    corps: bytes,
    supplementaires: dict[str, str] | None = None,
    horodatage: datetime | None = None,
) -> dict[str, str]:
    """Les en-têtes signés d'une requête SigV4, `Authorization` comprise."""
    morceaux = urlsplit(url)
    hote = morceaux.netloc
    chemin = morceaux.path or "/"
    requete = morceaux.query or ""

    quand = horodatage or datetime.now(timezone.utc)
    date_longue = quand.strftime("%Y%m%dT%H%M%SZ")
    date_courte = quand.strftime("%Y%m%d")
    empreinte = hashlib.sha256(corps).hexdigest() if corps else CHARGE_VIDE

    a_signer = {
        "host": hote,
        "x-amz-content-sha256": empreinte,
        "x-amz-date": date_longue,
    }
    a_signer.update({c.lower(): v for c, v in (supplementaires or {}).items()})

    noms = sorted(a_signer)
    canoniques = "".join(f"{n}:{a_signer[n].strip()}\n" for n in noms)
    signes = ";".join(noms)

    requete_canonique = "\n".join(
        [methode, chemin, requete, canoniques, signes, empreinte]
    )
    portee = f"{date_courte}/{stockage.region}/{SERVICE}/aws4_request"
    a_signer_texte = "\n".join(
        [
            ALGORITHME,
            date_longue,
            portee,
            hashlib.sha256(requete_canonique.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _cle_de_signature(stockage.secret_key, date_courte, stockage.region),
        a_signer_texte.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    entetes = dict(a_signer)
    entetes.pop("host", None)  # aiohttp le pose lui-même
    entetes["Authorization"] = (
        f"{ALGORITHME} Credential={stockage.access_key}/{portee}, "
        f"SignedHeaders={signes}, Signature={signature}"
    )
    return entetes


def _cle_de_signature(secret: str, date_courte: str, region: str) -> bytes:
    cle = _hmac(f"AWS4{secret}".encode("utf-8"), date_courte)
    cle = _hmac(cle, region)
    cle = _hmac(cle, SERVICE)
    return _hmac(cle, "aws4_request")


def _hmac(cle: bytes, message: str) -> bytes:
    return hmac.new(cle, message.encode("utf-8"), hashlib.sha256).digest()


def _sans_vide(valeurs: dict[str, str | None]) -> dict[str, str]:
    return {c: v for c, v in valeurs.items() if v}

"""La signature, l'URL, et ce que le stockage refuse.

LE VECTEUR AWS EST LE SEUL TEST QUI PROUVE LA SIGNATURE
Une signature SigV4 écrite à la main est juste ou fausse, sans milieu, et un
`403` du fournisseur ne dit jamais lequel des huit endroits est faux. Le vecteur
publié par AWS (« Example: PUT Object », doc S3, signature
98ad7217…08bd) fixe les huit d'un coup.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.aliud_collecteur.depot_s3 import (
    DepotRefuse,
    Stockage,
    _entetes,
    deposer,
    verifier,
)

VECTEUR_SIGNATURE = "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


def test_la_signature_reproduit_le_vecteur_publie_par_aws():
    stockage = Stockage(
        endpoint="https://examplebucket.s3.amazonaws.com",
        region="us-east-1",
        bucket="examplebucket",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    entetes = _entetes(
        stockage,
        methode="PUT",
        url="https://examplebucket.s3.amazonaws.com/test%24file.text",
        corps=b"Welcome to Amazon S3.",
        supplementaires={
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        horodatage=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert entetes["Authorization"].endswith(f"Signature={VECTEUR_SIGNATURE}")
    assert "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class" in entetes["Authorization"]
    assert "host" not in entetes  # aiohttp le pose lui-même


def test_l_hote_n_est_pas_envoye_deux_fois():
    stockage = _stockage()
    entetes = _entetes(stockage, "PUT", stockage.url("a/b.json.gz"), b"x")
    assert set(entetes) == {"x-amz-content-sha256", "x-amz-date", "Authorization"}


def test_l_url_est_en_style_chemin_et_porte_le_prefixe():
    stockage = _stockage(prefixe="archives")
    assert stockage.cle_complete("reddit/dernier.json.gz") == "archives/reddit/dernier.json.gz"
    assert stockage.url("reddit/dernier.json.gz") == (
        "https://s3.example.net/seau/archives/reddit/dernier.json.gz"
    )


def test_sans_prefixe_la_cle_reste_nue():
    assert _stockage().cle_complete("/reddit/x.gz") == "reddit/x.gz"


@pytest.mark.parametrize(
    "saisi, attendu",
    [
        ("s3.example.net", "https://s3.example.net"),
        ("https://s3.example.net/", "https://s3.example.net"),
        ("  https://s3.example.net  ", "https://s3.example.net"),
    ],
)
def test_le_point_d_entree_se_normalise(saisi, attendu):
    assert _stockage(endpoint=saisi).endpoint == attendu


async def test_un_put_accepte_rend_l_url_et_annonce_le_gzip():
    session = SessionFactice(reponses=[Reponse(200)])
    url = await deposer(
        session, _stockage(), "reddit/x.json.gz", b"\x1f\x8b", encodage="gzip"
    )
    assert url == "https://s3.example.net/seau/reddit/x.json.gz"
    appel = session.appels[0]
    assert appel["methode"] == "PUT"
    assert appel["entetes"]["content-encoding"] == "gzip"
    assert appel["entetes"]["content-type"] == "application/json"
    assert "content-encoding" in appel["entetes"]["Authorization"]  # il est signé


async def test_un_put_refuse_leve_avec_le_code_et_le_corps():
    session = SessionFactice(reponses=[Reponse(403, "AccessDenied")])
    with pytest.raises(DepotRefuse) as capture:
        await deposer(session, _stockage(), "reddit/x.json.gz", b"x")
    assert "403" in str(capture.value)
    assert "AccessDenied" in str(capture.value)


@pytest.mark.parametrize(
    "code, morceau",
    [(404, "n'existe pas"), (403, "clés refusées"), (500, "500")],
)
async def test_la_verification_traduit_ce_que_le_stockage_repond(code, morceau):
    session = SessionFactice(reponses=[Reponse(code)])
    with pytest.raises(DepotRefuse) as capture:
        await verifier(session, _stockage())
    assert morceau in str(capture.value)


async def test_la_verification_passe_sur_un_200():
    await verifier(SessionFactice(reponses=[Reponse(200)]), _stockage())


async def test_un_stockage_sans_bucket_est_refuse_avant_tout_appel():
    session = SessionFactice(reponses=[])
    with pytest.raises(DepotRefuse):
        await verifier(session, _stockage(bucket=""))
    assert session.appels == []


# ── Le faux réseau ──────────────────────────────────────────────────────────

def _stockage(**remplacements):
    defauts = dict(
        endpoint="https://s3.example.net",
        region="gra",
        bucket="seau",
        access_key="AK",
        secret_key="SK",
        prefixe="",
    )
    return Stockage(**{**defauts, **remplacements})


class Reponse:
    def __init__(self, status: int, corps: str = "") -> None:
        self.status = status
        self._corps = corps
        self.headers = {}

    async def text(self) -> str:
        return self._corps

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class SessionFactice:
    def __init__(self, reponses: list[Reponse]) -> None:
        self._reponses = list(reponses)
        self.appels: list[dict] = []

    def _noter(self, methode, url, headers, **reste):
        self.appels.append({"methode": methode, "url": url, "entetes": headers, **reste})
        return self._reponses.pop(0)

    def put(self, url, data=None, headers=None):
        return self._noter("PUT", url, headers or {}, corps=data)

    def head(self, url, headers=None):
        return self._noter("HEAD", url, headers or {})

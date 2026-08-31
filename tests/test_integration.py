"""Un passage de bout en bout : collecte, fichier local, dépôt, capteur.

CE QUE CES TESTS TIENNENT, ET QUE LES AUTRES NE TIENNENT PAS
Les modules ont chacun leurs tests. Ici on vérifie l'ordre dans lequel ils
s'appellent, et il porte une décision : **le disque avant le réseau.** Un `PUT`
refusé ne doit pas coûter trois minutes de requêtes déjà dépensées.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import pytest
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aliud_collecteur.const import (
    DOMAIN,
    OPT_DEBIT,
    OPT_GIGUE_MAX,
    OPT_GIGUE_MIN,
    SERVICE_COLLECTER,
)
from tests.faux_reseau import Reponse, Session, listing, publication

DONNEES = {
    "medias": ["reddit"],
    "reddit_client_id": "ID",
    "reddit_client_secret": "SECRET",
    "reddit_user_agent": "aliud:collecteur:0.1.0 (by /u/board)",
    "s3_endpoint": "https://s3.example.net",
    "s3_region": "gra",
    "s3_bucket": "aliud-collecte",
    "s3_access_key": "AK",
    "s3_secret_key": "SK",
    "s3_prefixe": "archives",
}
# Pas de gigue, débit maximal : ces tests mesurent l'enchaînement, pas le rythme.
OPTIONS = {OPT_DEBIT: 6000, OPT_GIGUE_MIN: 0, OPT_GIGUE_MAX: 0}


async def _monter(hass: HomeAssistant, session: Session, sources: str) -> MockConfigEntry:
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text(sources, encoding="utf-8")

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data=DONNEES, options=OPTIONS
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    return entree


def _session_nominale(nombre_de_sources: int, puts: list[Reponse] | None = None):
    reponses = [Reponse(200, {"access_token": "j"})]
    for i in range(nombre_de_sources):
        reponses.append(
            Reponse(
                200,
                listing(publication(name=f"t3_{i}")),
                headers={"X-Ratelimit-Remaining": "90", "X-Ratelimit-Reset": "300"},
            )
        )
    reponses.extend(puts if puts is not None else [Reponse(200), Reponse(200)])
    return Session(*reponses)


async def _collecter(hass, session, **donnees):
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        reponse = await hass.services.async_call(
            DOMAIN, SERVICE_COLLECTER, donnees, blocking=True, return_response=True
        )
        await hass.async_block_till_done()
    return reponse


async def test_un_passage_ecrit_le_fichier_local_et_depose_deux_objets(hass):
    session = _session_nominale(2)
    await _monter(hass, session, "programming\ndevops\n")

    bilan = await _collecter(hass, session)

    assert bilan["resultat"] == "succes"
    assert bilan["complet"] is True
    assert bilan["elements"] == 2
    assert bilan["sources_lues"] == 2
    assert bilan["erreur"] is None

    fichier = Path(bilan["medias"]["reddit"]["fichier"])
    assert fichier.exists()
    contenu = json.loads(gzip.decompress(fichier.read_bytes()))
    assert contenu["media"] == "reddit"
    assert contenu["passage"]["complet"] is True
    assert len(contenu["elements"]) == 2

    puts = [a for a in session.appels if a["methode"] == "PUT"]
    assert len(puts) == 2
    assert puts[0]["url"].startswith(
        "https://s3.example.net/aliud-collecte/archives/reddit/"
    )
    assert puts[0]["url"].endswith(".json.gz")
    assert puts[1]["url"] == (
        "https://s3.example.net/aliud-collecte/archives/reddit/dernier.json.gz"
    )
    assert puts[0]["corps"] == puts[1]["corps"]
    assert bilan["medias"]["reddit"]["cle_s3"].startswith("archives/reddit/")


async def test_le_disque_survit_a_un_depot_refuse(hass):
    session = _session_nominale(1, puts=[Reponse(403, corps="AccessDenied")])
    await _monter(hass, session, "programming\n")

    bilan = await _collecter(hass, session)

    assert Path(bilan["medias"]["reddit"]["fichier"]).exists(), (
        "la collecte ne doit pas être perdue"
    )
    assert bilan["elements"] == 1
    assert "dépôt refusé" in bilan["erreur"]
    assert bilan["resultat"] == "partiel"


async def test_une_source_muette_se_lit_dans_le_capteur(hass):
    session = Session(
        Reponse(200, {"access_token": "j"}),
        Reponse(200, listing(publication())),
        Reponse(404),
        Reponse(200),
        Reponse(200),
    )
    await _monter(hass, session, "programming\nsupprime\n")
    await _collecter(hass, session)

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat is not None
    assert etat.state == "partiel"
    assert etat.attributes["sources_declarees"] == 2
    assert etat.attributes["sources_lues"] == 1
    assert [m["source"] for m in etat.attributes["sources_muettes"]] == ["supprime"]
    assert etat.attributes["complet"] is False


async def test_la_limite_borne_le_passage_sans_toucher_au_fichier_de_sources(hass):
    session = _session_nominale(1)
    await _monter(hass, session, "programming\ndevops\nkubernetes\n")

    bilan = await _collecter(hass, session, limite=1)

    assert bilan["sources_declarees"] == 1
    gets = [a for a in session.appels if a["methode"] == "GET"]
    assert len(gets) == 1
    assert gets[0]["url"].endswith("/r/programming/top")
    sources = Path(hass.config.path("aliud_collecteur/sources-reddit.txt"))
    assert sources.read_text(encoding="utf-8").splitlines() == [
        "programming", "devops", "kubernetes"
    ]


async def test_un_essai_peut_ne_rien_ecrire_a_distance(hass):
    session = _session_nominale(1, puts=[])
    await _monter(hass, session, "programming\n")

    bilan = await _collecter(hass, session, deposer=False)

    assert bilan["medias"]["reddit"]["cle_s3"] == ""
    assert [a for a in session.appels if a["methode"] == "PUT"] == []
    assert Path(bilan["medias"]["reddit"]["fichier"]).exists()


async def test_les_sources_non_lues_repassent_en_tete_au_passage_suivant(hass):
    # Premier passage : budget d'une seconde, des sources à 0,6 s pièce.
    lent = _session_nominale(3)
    entree = await _monter(hass, lent, "a\nb\nc\n")
    hass.config_entries.async_update_entry(
        entree, options={**OPTIONS, "budget_secondes": 1}
    )
    await hass.async_block_till_done()

    async def _lent(*args, **kwargs):
        import asyncio

        await asyncio.sleep(0.6)
        return await vrai(*args, **kwargs)

    from custom_components.aliud_collecteur.collecteurs.reddit import Reddit

    vrai = Reddit.moissonner
    with patch.object(Reddit, "moissonner", _lent):
        bilan = await _collecter(hass, lent)

    assert bilan["sources_non_lues"], "le budget devait couper"
    # L'agrégat préfixe par le média ; le nom nu est dans le détail.
    assert all(n.startswith("reddit:") for n in bilan["sources_non_lues"])
    non_lues = list(bilan["medias"]["reddit"]["sources_non_lues"])

    # Second passage : les non-lues sont demandées en premier.
    session = _session_nominale(3)
    await _collecter(hass, session)
    gets = [a["url"].rsplit("/r/", 1)[1].removesuffix("/top")
            for a in session.appels if a["methode"] == "GET"]
    assert gets[: len(non_lues)] == non_lues


# ── Sans stockage, la collecte a lieu quand même ────────────────────────────
#
# C'est ce qui permet d'installer le greffon avant que le bucket existe. Ce que
# ces cas tiennent, c'est que le silence soit visible : `depot` porte le mot,
# le capteur le montre, et personne ne prend quinze relevés restés sur la
# machine pour quinze relevés archivés.

SANS_STOCKAGE = {
    **{c: v for c, v in DONNEES.items() if not c.startswith("s3_")},
    **{c: "" for c in DONNEES if c.startswith("s3_")},
}


async def _monter_sans_stockage(hass, session, sources):
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text(sources, encoding="utf-8")

    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data=SANS_STOCKAGE, options=OPTIONS
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    return entree


async def test_sans_stockage_le_releve_s_ecrit_et_rien_ne_part(hass):
    session = _session_nominale(2, puts=[])  # aucun PUT posé : un envoi lèverait
    await _monter_sans_stockage(hass, session, "programming\ndevops\n")

    bilan = await _collecter(hass, session)

    assert bilan["depot"] == "non_configure"
    assert bilan["cle_s3"] == ""
    assert bilan["erreur"] is None
    assert bilan["resultat"] == "succes", "une collecte complète reste complète"
    assert bilan["elements"] == 2
    assert [a for a in session.appels if a["methode"] == "PUT"] == []

    contenu = json.loads(
        gzip.decompress(Path(bilan["medias"]["reddit"]["fichier"]).read_bytes())
    )
    assert len(contenu["elements"]) == 2


async def test_le_capteur_dit_que_rien_n_a_ete_envoye(hass):
    session = _session_nominale(1, puts=[])
    await _monter_sans_stockage(hass, session, "programming\n")
    await _collecter(hass, session)

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat.state == "succes"
    assert etat.attributes["depot"] == "non_configure"


async def test_avec_stockage_le_depot_se_dit_envoye(hass):
    session = _session_nominale(1)
    await _monter(hass, session, "programming\n")
    bilan = await _collecter(hass, session)
    assert bilan["depot"] == "envoye"
    assert bilan["medias"]["reddit"]["cle_s3"].startswith("archives/reddit/")


async def test_un_essai_sans_depot_se_distingue_d_un_stockage_absent(hass):
    session = _session_nominale(1, puts=[])
    await _monter(hass, session, "programming\n")
    bilan = await _collecter(hass, session, deposer=False)
    assert bilan["depot"] == "desactive"


async def test_un_depot_refuse_porte_son_propre_mot(hass):
    session = _session_nominale(1, puts=[Reponse(403, corps="AccessDenied")])
    await _monter(hass, session, "programming\n")
    bilan = await _collecter(hass, session)
    assert bilan["depot"] == "refuse"
    assert Path(bilan["medias"]["reddit"]["fichier"]).exists()


async def test_un_passage_qui_n_a_rien_pu_ouvrir_n_ecrase_pas_le_dernier_releve(hass):
    # Reddit refuse la poignée de main : le relevé serait vide, et le déposer
    # remplacerait `dernier.json.gz` par un fichier qui ne dit rien.
    session = Session(Reponse(401, corps="Unauthorized"))
    await _monter(hass, session, "programming\n")

    bilan = await _collecter(hass, session)

    assert bilan["depot"] == "desactive"
    assert bilan["resultat"] == "echec"
    assert [a for a in session.appels if a["methode"] == "PUT"] == []


# ── Plusieurs médias dans un passage ────────────────────────────────────────
#
# L'agrégat prend toujours le pire, et c'est le point : un média en échec ne
# doit pas être noyé par deux qui ont abouti, parce qu'un capteur vert se lit
# « tout va bien ».

def _hn(points=775):
    return {"hits": [{"objectID": "49479837", "title": "un titre",
                      "url": "https://exemple.net/x", "author": "a",
                      "points": points, "num_comments": 12,
                      "created_at": "2026-08-28T15:17:09Z"}]}


def _lob():
    return [{"short_id": "xr1eor", "title": "un titre",
             "url": "https://exemple.net/y",
             "comments_url": "https://lobste.rs/s/xr1eor/y",
             "submitter_user": "b", "score": 56, "comment_count": 3,
             "created_at": "2026-08-28T11:35:15.935-05:00"}]


async def _monter_medias(hass, session, medias, sources_par_media):
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    for media, contenu in sources_par_media.items():
        (dossier / f"sources-{media}.txt").write_text(contenu, encoding="utf-8")

    entree = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={**DONNEES, "medias": medias},
        options=OPTIONS,
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    return entree


async def test_deux_medias_font_deux_releves_et_deux_depots(hass):
    session = Session(
        Reponse(200, _hn()), Reponse(200), Reponse(200),      # hackernews + 2 PUT
        Reponse(200, _lob()), Reponse(200), Reponse(200),     # lobsters + 2 PUT
    )
    await _monter_medias(
        hass, session, ["hackernews", "lobsters"],
        {"hackernews": "front_page\n", "lobsters": "hottest\n"},
    )

    bilan = await _collecter(hass, session)

    assert bilan["resultat"] == "succes"
    assert bilan["elements"] == 2
    assert set(bilan["medias"]) == {"hackernews", "lobsters"}
    for media in ("hackernews", "lobsters"):
        assert Path(bilan["medias"][media]["fichier"]).exists()
        assert bilan["medias"][media]["cle_s3"].startswith(f"archives/{media}/")

    puts = [a["url"] for a in session.appels if a["methode"] == "PUT"]
    assert len(puts) == 4
    assert sum("/hackernews/" in u for u in puts) == 2
    assert sum("/lobsters/" in u for u in puts) == 2


async def test_l_agregat_prend_le_pire_des_deux_medias(hass):
    session = Session(
        Reponse(200, _hn()), Reponse(200), Reponse(200),   # hackernews : complet
        Reponse(404),                                       # lobsters : muette
        Reponse(200), Reponse(200),
    )
    await _monter_medias(
        hass, session, ["hackernews", "lobsters"],
        {"hackernews": "front_page\n", "lobsters": "hottest\n"},
    )

    bilan = await _collecter(hass, session)

    assert bilan["medias"]["hackernews"]["resultat"] == "succes"
    assert bilan["medias"]["lobsters"]["resultat"] == "partiel"
    assert bilan["resultat"] == "partiel", "un média en défaut décide de l'agrégat"
    # Le nom du média voyage avec la source, sinon « hottest » ne dit pas chez qui.
    assert bilan["sources_muettes"] == [
        {"source": "hottest", "raison": bilan["sources_muettes"][0]["raison"],
         "media": "lobsters"}
    ]


async def test_le_capteur_montre_le_detail_par_media(hass):
    session = Session(
        Reponse(200, _hn()), Reponse(200), Reponse(200),
        Reponse(200, _lob()), Reponse(200), Reponse(200),
    )
    await _monter_medias(
        hass, session, ["hackernews", "lobsters"],
        {"hackernews": "front_page\n", "lobsters": "hottest\n"},
    )
    await _collecter(hass, session)

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat.state == "succes"
    assert set(etat.attributes["medias"]) == {"hackernews", "lobsters"}
    assert etat.attributes["medias"]["hackernews"]["elements"] == 1


async def test_le_service_peut_ne_reveiller_qu_un_media(hass):
    session = Session(Reponse(200, _hn()), Reponse(200), Reponse(200))
    await _monter_medias(
        hass, session, ["hackernews", "lobsters"],
        {"hackernews": "front_page\n", "lobsters": "hottest\n"},
    )

    bilan = await _collecter(hass, session, medias=["hackernews"])

    assert set(bilan["medias"]) == {"hackernews"}
    assert all("lobste.rs" not in a["url"] for a in session.appels)


async def test_avant_le_premier_passage_l_etat_est_inconnu(hass):
    session = Session(Reponse(200, _hn()))
    await _monter_medias(hass, session, ["hackernews"], {"hackernews": "front_page\n"})

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat.state == "unknown", "une intégration neuve n'a rien raté"


# ── Chaque média doit pouvoir se monter ─────────────────────────────────────
#
# Écrit après coup, contre un défaut réel : `DECALAGE_DEFAUT` n'était pas
# importé, et seul le média `arctic` le lit. Aucun test ne faisait passer ce
# média, donc la suite était verte et l'intégration levait un `NameError` au
# premier passage sur la machine. Un constructeur par média est une branche par
# média, et une branche non parcourue est une branche non testée.

@pytest.mark.parametrize("media", ["arctic", "hackernews", "lobsters", "reddit"])
async def test_chaque_media_se_monte_avec_ce_que_sa_classe_demande(hass, media):
    entree = await _monter_medias(
        hass, Session(), [media], {media: "une-source\n"}
    )
    passeur = entree.runtime_data
    collecteur = passeur._collecteur(media, ["une-source"])
    assert collecteur.media == media
    assert [s.nom for s in collecteur.sources()] == ["une-source"]


async def test_un_passage_arctic_va_au_bout(hass):
    publication = {
        "name": "t3_abc", "title": "un titre", "url": "https://exemple.net/a",
        "permalink": "/r/programming/comments/abc/x/", "author": "q",
        "score": 700, "num_comments": 273, "created_utc": 1787700000.0,
    }
    session = Session(
        Reponse(200, {"data": [publication]}), Reponse(200), Reponse(200)
    )
    await _monter_medias(hass, session, ["arctic"], {"arctic": "programming\n"})

    bilan = await _collecter(hass, session)

    assert bilan["resultat"] == "succes"
    assert bilan["medias"]["arctic"]["elements"] == 1
    contenu = json.loads(
        gzip.decompress(Path(bilan["medias"]["arctic"]["fichier"]).read_bytes())
    )
    assert contenu["media"] == "arctic"
    assert contenu["elements"][0]["points"] == 700
    # La fenêtre décalée voyage dans la requête, pas seulement dans le code.
    params = [a for a in session.appels if a["methode"] == "GET"][0]["params"]
    assert int(params["before"]) < int(__import__("time").time())
    assert int(params["before"]) - int(params["after"]) == 2 * 86400


# ── Le cookie, vu depuis Home Assistant ─────────────────────────────────────
#
# Un secret qui expire tout seul se gère à l'écran ou ne se gère pas : ces cas
# tiennent ce que l'utilisateur voit et ce que Home Assistant déclenche.

from datetime import datetime, timedelta, timezone  # noqa: E402

DEMAIN = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
HIER_ISO = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
DANS_UN_MOIS = (
    datetime.now(timezone.utc) + timedelta(days=30)
).isoformat(timespec="seconds")

AVEC_COOKIE = {
    **{c: v for c, v in DONNEES.items() if not c.startswith("s3_")},
    **{c: "" for c in DONNEES if c.startswith("s3_")},
    "reddit_client_id": "",
    "reddit_client_secret": "",
    "reddit_cookie": "reddit_session=abc; token_v2=def",
}


async def _monter_cookie(hass, session, expire_le):
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text("programming\n", encoding="utf-8")
    entree = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={**AVEC_COOKIE, "reddit_cookie_expire": expire_le},
        options=OPTIONS,
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        charge = await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    return entree, charge


@pytest.mark.parametrize(
    "expire_le, attendu",
    [(DANS_UN_MOIS, "valide"), (DEMAIN, "bientot"), ("", "sans_date")],
)
async def test_le_capteur_dit_ou_en_est_la_session(hass, expire_le, attendu):
    session = _session_nominale(1, puts=[])
    await _monter_cookie(hass, session, expire_le)
    await _collecter(hass, session)

    etat = hass.states.get("sensor.aliud_collecteur_de_medias_last_run")
    assert etat.attributes["cookies"]["reddit"]["etat"] == attendu


async def test_un_cookie_expire_empeche_le_chargement_et_demande_le_formulaire(hass):
    _, charge = await _monter_cookie(hass, Session(), HIER_ISO)
    assert charge is False, "un cookie périmé ne sert à rien, autant le dire au départ"
    flux = [
        f for f in hass.config_entries.flow.async_progress()
        if f["handler"] == DOMAIN
    ]
    assert flux and flux[0]["context"]["source"] == "reauth"


async def test_une_session_tombee_en_vol_ouvre_le_formulaire(hass):
    # 403 sur la première source : la session est tombée pendant le passage.
    session = Session(Reponse(403, corps="Forbidden"))
    entree, _ = await _monter_cookie(hass, session, DANS_UN_MOIS)

    bilan = await _collecter(hass, session)

    assert bilan["medias"]["reddit"]["session_tombee"] is True
    assert bilan["resultat"] == "echec"
    flux = [
        f for f in hass.config_entries.flow.async_progress()
        if f["handler"] == DOMAIN and f["context"]["source"] == "reauth"
    ]
    assert flux, "Home Assistant doit poser la carte « à reconfigurer »"


async def test_une_session_tombee_n_insiste_pas_sur_les_autres_sources(hass):
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text("a\nb\nc\n", encoding="utf-8")
    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={**AVEC_COOKIE, "reddit_cookie_expire": DANS_UN_MOIS}, options=OPTIONS,
    )
    entree.add_to_hass(hass)
    session = Session(Reponse(403))
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()

    bilan = await _collecter(hass, session)

    assert len([a for a in session.appels if a["methode"] == "GET"]) == 1
    assert sorted(bilan["medias"]["reddit"]["sources_non_lues"]) == ["a", "b", "c"]


async def test_la_disposition_choisie_atteint_la_cle_deposee(hass):
    session = _session_nominale(1)
    entree = await _monter(hass, session, "programming\n")
    hass.config_entries.async_update_entry(
        entree, options={**OPTIONS, "disposition": "date_puis_media"}
    )
    await hass.async_block_till_done()

    bilan = await _collecter(hass, session)

    cle = bilan["medias"]["reddit"]["cle_s3"]
    assert cle.startswith("archives/20"), cle
    assert "/reddit/reddit-" in cle


# ── L'agent déclaré ─────────────────────────────────────────────────────────
#
# Réglable dans les options depuis la v0.8.0. Avant, les quatre médias ouverts
# héritaient du champ « agent Reddit » : un réglage nommé pour un média en
# pilotait quatre autres, ce que personne n'aurait deviné en le lisant.

async def _agent_envoye(hass, media, reponse, options=None):
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / f"sources-{media}.txt").write_text("une-source\n", encoding="utf-8")
    session = Session(reponse, Reponse(200), Reponse(200))
    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN,
        data={**DONNEES, "medias": [media]},
        options={**OPTIONS, **(options or {})},
    )
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    await _collecter(hass, session)
    return [a for a in session.appels if a["methode"] == "GET"][0]["entetes"]["User-Agent"]


async def test_l_agent_des_options_atteint_les_medias_ouverts(hass):
    envoye = await _agent_envoye(
        hass, "lobsters", Reponse(200, []), {"agent": "le-mien/1.0 (+https://exemple.net)"}
    )
    assert envoye == "le-mien/1.0 (+https://exemple.net)"


async def test_un_agent_vide_retombe_sur_celui_du_greffon(hass):
    envoye = await _agent_envoye(hass, "lobsters", Reponse(200, []), {"agent": "   "})
    assert envoye.startswith("aliud-collecteur/")


async def test_sans_reglage_l_agent_reste_celui_du_greffon(hass):
    envoye = await _agent_envoye(hass, "lobsters", Reponse(200, []))
    assert envoye.startswith("aliud-collecteur/")


async def test_l_agent_des_options_ne_pilote_pas_reddit(hass):
    # Reddit garde le sien : son API exige une forme précise et refuse un agent
    # générique. Deux champs, deux contrats.
    session = _session_nominale(1, puts=[])
    entree = MockConfigEntry(
        domain=DOMAIN, unique_id=DOMAIN, data=DONNEES,
        options={**OPTIONS, "agent": "le-mien/1.0"},
    )
    dossier = Path(hass.config.path("aliud_collecteur"))
    dossier.mkdir(parents=True, exist_ok=True)
    (dossier / "sources-reddit.txt").write_text("programming\n", encoding="utf-8")
    entree.add_to_hass(hass)
    with patch(
        "custom_components.aliud_collecteur.async_get_clientsession",
        return_value=session,
    ):
        assert await hass.config_entries.async_setup(entree.entry_id)
        await hass.async_block_till_done()
    await _collecter(hass, session)

    gets = [a for a in session.appels if a["methode"] == "GET"]
    assert gets[0]["entetes"]["User-Agent"] == DONNEES["reddit_user_agent"]

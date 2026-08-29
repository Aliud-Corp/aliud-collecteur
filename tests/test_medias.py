"""Les trois médias ouverts, et ce que chacun fait de sa charge.

POURQUOI CES TROIS-LÀ
reddit.com refuse un client anonyme au niveau réseau depuis le 29/08/2026,
`robots.txt` compris. Ces trois sources répondent `200` à un agent nommé, et
celui d'Arctic Shift déclare `Disallow:` vide — tout permis. Le premier test de
chaque classe fixe donc la même chose : aucune poignée de main, aucun secret.
"""

from __future__ import annotations

import time

import pytest

from custom_components.aliud_collecteur.collecteurs import (
    REGISTRE,
    Source,
    SourceMuette,
    TropDeRequetes,
)
from custom_components.aliud_collecteur.collecteurs.arctic import ArcticShift
from custom_components.aliud_collecteur.collecteurs.hackernews import HackerNews
from custom_components.aliud_collecteur.collecteurs.lobsters import Lobsters
from tests.faux_reseau import Reponse, Session

AGENT = "aliud-collecteur/0.3 (+https://exemple)"


def _src(media, nom):
    return Source(media=media, nom=nom)


# ── Le registre ─────────────────────────────────────────────────────────────

def test_les_quatre_medias_sont_enregistres():
    assert set(REGISTRE) == {"arctic", "hackernews", "lobsters", "reddit"}
    for nom, classe in REGISTRE.items():
        assert classe.media == nom, "un collecteur rangé sous un autre nom que le sien"


# ── Arctic Shift ────────────────────────────────────────────────────────────

def _post(**r):
    d = {
        "name": "t3_abc", "id": "abc", "title": "un titre",
        "url": "https://exemple.net/a",
        "permalink": "/r/programming/comments/abc/un_titre/",
        "author": "quelqu-un", "score": 10, "num_comments": 2,
        "created_utc": 1787900000.0,
    }
    return {**d, **r}


async def test_arctic_n_a_besoin_d_aucun_identifiant():
    session = Session()  # aucune réponse posée : un appel lèverait
    ctx = await ArcticShift(agent=AGENT, noms=["programming"]).ouvrir(session)
    assert session.appels == []
    assert ctx.agent == AGENT


async def test_arctic_lit_une_fenetre_decalee_fixee_une_fois_par_passage():
    session = Session(Reponse(200, {"data": []}), Reponse(200, {"data": []}))
    collecteur = ArcticShift(
        agent=AGENT, noms=["a", "b"], decalage_jours=2, fenetre_jours=3
    )
    ctx = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, ctx, _src("arctic", "a"))
    await collecteur.moissonner(session, ctx, _src("arctic", "b"))

    maintenant = time.time()
    assert maintenant - ctx.avant == pytest.approx(2 * 86400, abs=30)
    assert ctx.avant - ctx.apres == 3 * 86400
    # Les deux sources lisent la même fenêtre, sinon les bornes du relevé ne
    # voudraient rien dire.
    a, b = session.appels
    assert a["params"]["after"] == b["params"]["after"]
    assert a["params"]["before"] == b["params"]["before"]
    assert a["params"]["subreddit"] == "a"


async def test_arctic_trie_par_score_puisque_l_api_ne_sait_pas():
    session = Session(Reponse(200, {"data": [
        _post(name="t3_1", score=5, num_comments=1),
        _post(name="t3_2", score=700, num_comments=273),
        _post(name="t3_3", score=128, num_comments=41),
    ]}))
    collecteur = ArcticShift(agent=AGENT, noms=["a"], par_source=2)
    ctx = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, ctx, _src("arctic", "a"))

    assert [e.points for e in moisson.elements] == [700, 128]
    assert moisson.elements[0].permalien.startswith("https://www.reddit.com/r/")
    assert moisson.elements[0].media == "arctic"
    assert moisson.elements[0].cree_le.startswith("2026-")


async def test_arctic_ecarte_une_publication_sans_identifiant():
    boiteuse = _post(); boiteuse.pop("name"); boiteuse.pop("id")
    session = Session(Reponse(200, {"data": [boiteuse, _post(name="t3_ok")]}))
    collecteur = ArcticShift(agent=AGENT, noms=["a"])
    ctx = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, ctx, _src("arctic", "a"))
    assert [e.identifiant for e in moisson.elements] == ["t3_ok"]


async def test_arctic_traduit_une_erreur_de_charge_en_source_muette():
    session = Session(Reponse(200, {"data": None, "error": "'sort_type' must be one of"}))
    collecteur = ArcticShift(agent=AGENT, noms=["a"])
    ctx = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, ctx, _src("arctic", "a"))


@pytest.mark.parametrize(
    "code, attendu", [(429, TropDeRequetes), (503, TropDeRequetes), (400, SourceMuette)]
)
async def test_arctic_distingue_ce_qui_se_reessaie(code, attendu):
    session = Session(Reponse(code, corps="oups"))
    collecteur = ArcticShift(agent=AGENT, noms=["a"])
    ctx = await collecteur.ouvrir(session)
    with pytest.raises(attendu):
        await collecteur.moissonner(session, ctx, _src("arctic", "a"))


# ── Hacker News ─────────────────────────────────────────────────────────────

def _hit(**r):
    d = {
        "objectID": "49479837", "title": "GUIs should be keyboard-driven",
        "url": "https://exemple.net/x", "author": "ckardaris",
        "points": 775, "num_comments": 393, "created_at": "2026-08-28T15:17:09Z",
        "_tags": ["story"],
    }
    return {**d, **r}


async def test_hn_ne_borne_pas_la_page_d_accueil_dans_le_temps():
    session = Session(Reponse(200, {"hits": [_hit()]}))
    collecteur = HackerNews(agent=AGENT, noms=["front_page"], fenetre_jours=1)
    ctx = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, ctx, _src("hackernews", "front_page"))

    params = session.appels[0]["params"]
    assert params["tags"] == "front_page"
    assert "numericFilters" not in params, (
        "la page d'accueil porte des publications remontées le lendemain"
    )


async def test_hn_construit_une_recherche_bornee_dans_le_temps():
    session = Session(Reponse(200, {"hits": []}))
    collecteur = HackerNews(agent=AGENT, noms=["q:kubernetes"], fenetre_jours=2)
    ctx = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, ctx, _src("hackernews", "q:kubernetes"))

    params = session.appels[0]["params"]
    assert params["query"] == "kubernetes"
    assert params["tags"] == "story"
    assert params["numericFilters"].startswith("created_at_i>")


async def test_hn_retombe_sur_la_page_d_accueil_pour_une_etiquette_inconnue():
    session = Session(Reponse(200, {"hits": []}))
    collecteur = HackerNews(agent=AGENT, noms=["farfelu"])
    ctx = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, ctx, _src("hackernews", "farfelu"))
    assert session.appels[0]["params"]["tags"] == "front_page"


async def test_hn_donne_son_fil_pour_adresse_a_une_publication_sans_url():
    sans_url = _hit(); sans_url.pop("url")
    session = Session(Reponse(200, {"hits": [sans_url]}))
    collecteur = HackerNews(agent=AGENT, noms=["ask_hn"])
    ctx = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, ctx, _src("hackernews", "ask_hn"))

    e = moisson.elements[0]
    assert e.url == e.permalien == "https://news.ycombinator.com/item?id=49479837"
    assert e.points == 775 and e.commentaires == 393
    assert "_tags" not in e.brut, "les champs internes d'Algolia ne sont pas l'archive"


# ── Lobsters ────────────────────────────────────────────────────────────────

def _story(**r):
    d = {
        "short_id": "xr1eor", "title": "Yap: a particular kind of slop",
        "url": "https://mckayla.blog/posts/yap.html",
        "comments_url": "https://lobste.rs/s/xr1eor/yap",
        "submitter_user": "lilac", "score": 56, "comment_count": 16,
        "created_at": "2026-08-28T11:35:15.935-05:00",
        "tags": ["practices"],
    }
    return {**d, **r}


@pytest.mark.parametrize(
    "nom, url",
    [
        ("hottest", "https://lobste.rs/hottest.json"),
        ("newest", "https://lobste.rs/newest.json"),
        ("t:devops", "https://lobste.rs/t/devops.json"),
        ("farfelu", "https://lobste.rs/hottest.json"),
    ],
)
async def test_lobsters_compose_l_adresse_de_chaque_forme(nom, url):
    session = Session(Reponse(200, []))
    collecteur = Lobsters(agent=AGENT, noms=[nom])
    ctx = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, ctx, _src("lobsters", nom))
    assert session.appels[0]["url"] == url


async def test_lobsters_ramene_la_date_locale_en_utc():
    session = Session(Reponse(200, [_story()]))
    collecteur = Lobsters(agent=AGENT, noms=["hottest"])
    ctx = await collecteur.ouvrir(session)
    e = (await collecteur.moissonner(session, ctx, _src("lobsters", "hottest"))).elements[0]

    assert e.cree_le == "2026-08-28T16:35:15+00:00"
    assert e.points == 56 and e.commentaires == 16
    assert e.auteur == "lilac"


async def test_lobsters_donne_le_fil_pour_adresse_a_une_publication_sans_url():
    sans_url = _story(); sans_url.pop("url")
    session = Session(Reponse(200, [sans_url]))
    collecteur = Lobsters(agent=AGENT, noms=["hottest"])
    ctx = await collecteur.ouvrir(session)
    e = (await collecteur.moissonner(session, ctx, _src("lobsters", "hottest"))).elements[0]
    assert e.url == e.permalien == "https://lobste.rs/s/xr1eor/yap"


async def test_lobsters_refuse_une_charge_qui_n_est_pas_une_liste():
    session = Session(Reponse(200, {"erreur": "maintenance"}))
    collecteur = Lobsters(agent=AGENT, noms=["hottest"])
    ctx = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, ctx, _src("lobsters", "hottest"))


async def test_lobsters_borne_ce_qu_il_rend():
    session = Session(Reponse(200, [_story(short_id=f"s{i}") for i in range(10)]))
    collecteur = Lobsters(agent=AGENT, noms=["hottest"], par_source=3)
    ctx = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, ctx, _src("lobsters", "hottest"))
    assert len(moisson.elements) == 3

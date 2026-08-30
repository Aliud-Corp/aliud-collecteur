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
    decouper_plancher,
    Source,
    SourceMuette,
    TropDeRequetes,
)
from custom_components.aliud_collecteur.collecteurs.arctic import ArcticShift
from custom_components.aliud_collecteur.collecteurs.hackernews import HackerNews
from custom_components.aliud_collecteur.collecteurs.lobsters import Lobsters
from custom_components.aliud_collecteur.collecteurs.rss import OCTETS_MAX, Rss
from tests.faux_reseau import Reponse, Session

AGENT = "aliud-collecteur/0.3 (+https://exemple)"


def _src(media, nom):
    return Source(media=media, nom=nom)


# ── Le registre ─────────────────────────────────────────────────────────────

def test_les_cinq_medias_sont_enregistres():
    assert set(REGISTRE) == {"rss", "arctic", "hackernews", "lobsters", "reddit"}
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


# ── Le plancher de score ────────────────────────────────────────────────────
#
# Repris de Horizon, qui pose un `min_score` par sous-reddit. Ici il est **à
# zéro par défaut** : l'archive est brute, et un filtre par défaut contredirait
# ce que l'ADR 0034 promet. Le séparateur est `@` et non `:`, déjà pris par
# `q:<termes>` et `t:<etiquette>`.

@pytest.mark.parametrize(
    "ligne, nom, plancher",
    [
        ("programming", "programming", 0),
        ("programming@200", "programming", 200),
        ("  programming @ 200 ", "programming", 200),
        ("q:kubernetes", "q:kubernetes", 0),
        ("q:kubernetes@50", "q:kubernetes", 50),
        ("t:devops@20", "t:devops", 20),
        ("programming@abc", "programming@abc", 0),
        ("programming@-5", "programming", 0),
    ],
)
def test_le_plancher_se_lit_sans_marcher_sur_les_autres_grammaires(ligne, nom, plancher):
    assert decouper_plancher(ligne) == (nom, plancher)


def test_une_source_sans_plancher_laisse_tout_entrer():
    assert Lobsters(AGENT, ["hottest"]).sources()[0].plancher == 0


def test_le_plancher_declare_voyage_jusqu_a_la_source():
    sources = ArcticShift(AGENT, ["programming@200", "devops"]).sources()
    assert [(s.nom, s.plancher) for s in sources] == [("programming", 200), ("devops", 0)]


# ── RSS ─────────────────────────────────────────────────────────────────────

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Un blogue</title>
  <entry>
    <title>Un billet</title>
    <link rel="alternate" href="https://exemple.net/billet"/>
    <id>tag:exemple.net,2026:1</id>
    <published>2026-08-28T15:17:09Z</published>
    <author><name>quelqu-un</name></author>
  </entry>
</feed>"""

RSS2 = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Un site</title>
    <item>
      <title>Une nouvelle</title>
      <link>https://exemple.net/nouvelle</link>
      <guid>https://exemple.net/nouvelle</guid>
      <pubDate>Thu, 28 Aug 2026 11:35:15 -0500</pubDate>
      <dc:creator>quelqu-une</dc:creator>
    </item>
  </channel>
</rss>"""


class ReponseFlux:
    """Une réponse dont le corps se lit par morceaux, comme aiohttp le fait."""

    def __init__(self, status=200, corps=b"", morceau=65536):
        self.status = status
        self.headers = {}
        self._corps = corps
        self._morceau = morceau
        self.content = self

    async def iter_chunked(self, taille):
        for i in range(0, len(self._corps), self._morceau):
            yield self._corps[i : i + self._morceau]

    async def text(self):
        return self._corps.decode("utf-8", "replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class SessionFlux(Session):
    def get(self, url, params=None, headers=None):
        return self._servir("GET", url, params=params, entetes=headers or {})


async def _flux(corps, nom="essai https://exemple.net/flux.xml", **kw):
    session = SessionFlux(ReponseFlux(200, corps))
    collecteur = Rss(AGENT, [nom], **kw)
    source = collecteur.sources()[0]
    ctx = await collecteur.ouvrir(session)
    return await collecteur.moissonner(session, ctx, source), session, source


async def test_rss_lit_un_flux_atom():
    moisson, session, _ = await _flux(ATOM.encode())
    e = moisson.elements[0]
    assert e.media == "rss"
    assert e.titre == "Un billet"
    assert e.url == e.permalien == "https://exemple.net/billet"
    assert e.identifiant == "tag:exemple.net,2026:1"
    assert e.auteur == "quelqu-un"
    assert e.cree_le == "2026-08-28T15:17:09+00:00"
    assert e.points == 0, "un flux ne classe pas, et zéro est la valeur exacte"


async def test_rss_lit_un_flux_rss2_et_ramene_la_date_en_utc():
    moisson, _, _ = await _flux(RSS2.encode())
    e = moisson.elements[0]
    assert e.titre == "Une nouvelle"
    assert e.url == "https://exemple.net/nouvelle"
    assert e.auteur == "quelqu-une"
    assert e.cree_le == "2026-08-28T16:35:15+00:00"


async def test_rss_deduit_le_nom_de_l_hote_quand_il_manque():
    collecteur = Rss(AGENT, ["https://www.exemple.net/flux.xml"])
    source = collecteur.sources()[0]
    assert source.nom == "exemple.net"
    assert source.options["url"] == "https://www.exemple.net/flux.xml"


async def test_rss_refuse_une_charge_trop_grosse_avant_de_l_analyser():
    enorme = b"<feed>" + b"x" * (OCTETS_MAX + 1)
    with pytest.raises(SourceMuette) as capture:
        await _flux(enorme)
    assert "avant analyse" in str(capture.value)


async def test_rss_refuse_ce_qui_n_est_pas_du_xml():
    with pytest.raises(SourceMuette):
        await _flux(b"<html><body>page d'erreur</body></html>")


async def test_rss_ecarte_une_entree_sans_rien_d_exploitable():
    creux = ATOM.replace("<title>Un billet</title>", "").replace(
        '<link rel="alternate" href="https://exemple.net/billet"/>', ""
    ).replace("<id>tag:exemple.net,2026:1</id>", "")
    with pytest.raises(SourceMuette):
        await _flux(creux.encode())


async def test_rss_borne_ce_qu_il_rend():
    plusieurs = ATOM.replace("</feed>", ("<entry><title>x</title>"
        "<link rel=\"alternate\" href=\"https://exemple.net/x\"/>"
        "<id>x</id></entry>" * 9) + "</feed>")
    moisson, _, _ = await _flux(plusieurs.encode(), par_source=3)
    assert len(moisson.elements) == 3


async def test_rss_annonce_ce_qu_il_accepte():
    _, session, _ = await _flux(ATOM.encode())
    assert "atom" in session.appels[0]["entetes"]["Accept"]
    assert session.appels[0]["entetes"]["User-Agent"] == AGENT


async def test_rss_accepte_un_flux_vide_sans_le_confondre_avec_une_panne():
    vide = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title></feed>'
    moisson, _, _ = await _flux(vide.encode())
    assert moisson.elements == [], "un flux sans entrée est un flux, pas une panne"

"""Le collecteur Reddit : la porte, la moisson, et ce qu'il fait des codes.

LA PORTE EST L'ENREGISTREMENT
Les trois premiers tests fixent le comportement le plus important du fichier :
sans client enregistré, aucune requête de collecte n'est tentée. Un jour où
quelqu'un voudra « juste essayer sans les clés », ces tests diront non.
"""

from __future__ import annotations

import pytest

from custom_components.aliud_collecteur.collecteurs import (
    PassageImpossible,
    Source,
    SourceMuette,
    TropDeRequetes,
)
from custom_components.aliud_collecteur.collecteurs.reddit import (
    SOURCES_PAR_DEFAUT,
    Reddit,
)
from tests.faux_reseau import Reponse, Session, listing, publication

AGENT = "aliud:collecteur:0.1.0 (by /u/board)"


def _reddit(**remplacements):
    defauts = dict(
        client_id="ID", client_secret="SECRET", user_agent=AGENT,
        noms=["programming"], par_source=25, cookie="",
    )
    return Reddit(**{**defauts, **remplacements})


# ── La porte ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("manquant", ["client_id", "client_secret", "user_agent"])
async def test_sans_client_enregistre_aucune_requete_n_est_tentee(manquant):
    session = Session()  # aucune réponse posée : tout appel lèverait
    with pytest.raises(PassageImpossible):
        await _reddit(**{manquant: ""}).ouvrir(session)
    assert session.appels == []


async def test_la_poignee_de_main_rend_le_jeton():
    session = Session(Reponse(200, {"access_token": "jeton-du-jour"}))
    contexte = await _reddit().ouvrir(session)
    assert contexte.jeton == "jeton-du-jour"
    assert contexte.agent == AGENT
    assert session.appels[0]["entetes"]["Authorization"].startswith("Basic ")


async def test_une_poignee_de_main_vide_arrete_le_passage():
    session = Session(Reponse(200, {}))
    with pytest.raises(PassageImpossible):
        await _reddit().ouvrir(session)


async def test_un_401_a_la_poignee_de_main_arrete_le_passage():
    session = Session(Reponse(401, corps="Unauthorized"))
    with pytest.raises(PassageImpossible):
        await _reddit().ouvrir(session)


async def test_un_429_a_la_poignee_de_main_se_reessaie():
    session = Session(Reponse(429, headers={"Retry-After": "12"}))
    with pytest.raises(TropDeRequetes) as capture:
        await _reddit().ouvrir(session)
    assert capture.value.attente == 12.0


# ── La moisson ──────────────────────────────────────────────────────────────

async def test_une_publication_devient_un_element_normalise():
    session = Session(
        Reponse(200, {"access_token": "j"}),
        Reponse(200, listing(publication()), headers={"X-Ratelimit-Remaining": "97.0",
                                                      "X-Ratelimit-Reset": "540"}),
    )
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(
        session, contexte, Source("reddit", "programming")
    )

    assert moisson.restant == 97
    assert moisson.remise_a_zero == 540.0
    element = moisson.elements[0]
    assert element.media == "reddit"
    assert element.source == "programming"
    assert element.identifiant == "t3_abc"
    assert element.points == 1234
    assert element.commentaires == 89
    assert element.permalien == (
        "https://www.reddit.com/r/programming/comments/abc/un_titre/"
    )
    assert element.cree_le.startswith("2026-")
    assert element.brut["title"] == "un titre"


async def test_la_requete_porte_la_fenetre_la_limite_et_le_jeton():
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(200, listing()))
    collecteur = _reddit(par_source=40, fenetre="week")
    contexte = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, contexte, Source("reddit", "devops"))

    appel = session.appels[1]
    assert appel["url"] == "https://oauth.reddit.com/r/devops/top"
    assert appel["params"] == {"t": "week", "limit": "40", "raw_json": "1"}
    assert appel["entetes"]["Authorization"] == "bearer j"
    assert appel["entetes"]["User-Agent"] == AGENT


async def test_une_limite_hors_bornes_est_ramenee_dans_les_bornes():
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(200, listing()))
    collecteur = _reddit(par_source=500)
    contexte = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert session.appels[1]["params"]["limit"] == "100"


async def test_une_fenetre_inconnue_retombe_sur_le_jour():
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(200, listing()))
    collecteur = _reddit(fenetre="siecle")
    contexte = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert session.appels[1]["params"]["t"] == "day"


@pytest.mark.parametrize("code", [403, 404, 451])
async def test_une_source_inaccessible_est_muette_et_ne_se_reessaie_pas(code):
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(code))
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, contexte, Source("reddit", "prive"))


@pytest.mark.parametrize("code", [429, 500, 503])
async def test_un_bridage_ou_une_panne_se_reessaie(code):
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(code))
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(TropDeRequetes):
        await collecteur.moissonner(session, contexte, Source("reddit", "a"))


async def test_une_publication_sans_identifiant_est_ecartee_sans_casser_la_moisson():
    boiteuse = publication()
    boiteuse.pop("name")
    boiteuse.pop("created_utc")
    session = Session(
        Reponse(200, {"access_token": "j"}),
        Reponse(200, listing(boiteuse, publication(name="t3_ok"))),
    )
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert [e.identifiant for e in moisson.elements] == ["t3_ok"]


async def test_un_media_sans_en_tetes_de_debit_ne_ment_pas():
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(200, listing()))
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert moisson.restant is None
    assert moisson.remise_a_zero is None


# ── La liste livrée ─────────────────────────────────────────────────────────

def test_la_liste_par_defaut_porte_cent_sources_sans_doublon():
    lignes = [l for l in SOURCES_PAR_DEFAUT.splitlines() if l.strip()]
    assert len(lignes) == 100
    assert len(set(lignes)) == 100


def test_les_sources_declarees_portent_leur_media():
    sources = _reddit(noms=["a", "b"]).sources()
    assert [s.cle for s in sources] == ["reddit:a", "reddit:b"]


# ── Le cookie de session ────────────────────────────────────────────────────
#
# Autorisé par la clause 4 de l'ADR 0034 le 31/08/2026. Ces cas tiennent les
# trois conditions qu'elle pose, et la seule que le board a écartée : le cookie
# oui, l'usurpation d'agent non.

COOKIE = "reddit_session=abc; token_v2=def"


async def test_un_cookie_ouvre_le_passage_sans_poignee_de_main():
    session = Session()  # aucune réponse posée : une poignée de main lèverait
    contexte = await _reddit(client_id="", client_secret="", cookie=COOKIE).ouvrir(
        session
    )
    assert session.appels == []
    assert contexte.par_cookie is True
    assert contexte.cookie == COOKIE


async def test_le_client_enregistre_reste_prefere_au_cookie():
    session = Session(Reponse(200, {"access_token": "j"}))
    contexte = await _reddit(cookie=COOKIE).ouvrir(session)
    assert contexte.par_cookie is False, "un jeton ne fait que lire, un cookie publie"
    assert contexte.jeton == "j"


async def test_sans_client_ni_cookie_le_passage_est_impossible():
    session = Session()
    with pytest.raises(PassageImpossible) as capture:
        await _reddit(client_id="", client_secret="").ouvrir(session)
    assert "ni client enregistré, ni cookie" in str(capture.value)
    assert session.appels == []


async def test_l_agent_reste_le_notre_en_mode_cookie():
    session = Session(Reponse(200, listing(publication())))
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, contexte, Source("reddit", "programming"))

    entetes = session.appels[0]["entetes"]
    assert entetes["User-Agent"] == AGENT
    assert entetes["Cookie"] == COOKIE
    assert "Authorization" not in entetes
    assert "Mozilla" not in entetes["User-Agent"], (
        "le board a choisi le cookie contre l'usurpation d'agent, pas en plus"
    )


async def test_le_mode_cookie_passe_par_le_site_et_non_par_oauth():
    session = Session(Reponse(200, listing()))
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    await collecteur.moissonner(session, contexte, Source("reddit", "devops"))
    assert session.appels[0]["url"] == "https://www.reddit.com/r/devops/top.json"


async def test_un_401_arrete_le_passage_tout_de_suite():
    """Un `401` dit « je ne sais pas qui tu es » : c'est la session, pas la porte."""
    session = Session(Reponse(401, corps="Unauthorized"))
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(PassageImpossible) as capture:
        await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert "s'arrête au lieu d'insister" in str(capture.value)


async def test_un_403_isole_ne_rend_qu_une_source_muette():
    """MESURÉ LE 01/09/2026, ET C'EST CE QUI A COÛTÉ UN RELEVÉ ENTIER

    `r/api` a rendu `403` au milieu d'un passage, quatre-vingt-une sources après
    le début, avec un cookie valide cent quatre-vingts jours de plus. Reddit rend
    `403` à un compte connecté sur un sous-reddit privé, restreint ou en
    quarantaine : la porte de ce sous-reddit est fermée, pas la session. Le
    passage entier était jeté, six cent un éléments avec lui.
    """
    session = Session(Reponse(403, corps="Forbidden"))
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette) as capture:
        await collecteur.moissonner(session, contexte, Source("reddit", "api"))
    assert "quarantaine" in str(capture.value)
    assert contexte.refus_consecutifs == 1


async def test_trois_403_d_affilee_arretent_quand_meme_le_passage():
    """La clause 4 tient : on ne pilonne pas une porte fermée.

    Trois sous-reddits fermés à la suite dans une liste de cent n'arrive presque
    jamais par hasard ; une session tombée, elle, les ferme tous d'un coup.
    """
    session = Session(*[Reponse(403, corps="Forbidden")] * 3)
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    for nom in ("a", "b"):
        with pytest.raises(SourceMuette):
            await collecteur.moissonner(session, contexte, Source("reddit", nom))
    with pytest.raises(PassageImpossible) as capture:
        await collecteur.moissonner(session, contexte, Source("reddit", "c"))
    assert "3 refus d'affilée" in str(capture.value)


async def test_une_source_lue_remet_le_compteur_a_zero():
    """Deux portes fermées séparées par une source qui répond ne font pas trois.

    Sans cette remise à zéro, une liste de cent sous-reddits dont trois sont
    privés finirait par s'arrêter à la centième, sur un compteur qui n'aurait
    jamais rien mesuré d'autre que la longueur de la liste.
    """
    session = Session(
        Reponse(403, corps="Forbidden"),
        Reponse(200, listing(publication(score=700))),
        Reponse(403, corps="Forbidden"),
    )
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    await collecteur.moissonner(session, contexte, Source("reddit", "b"))
    assert contexte.refus_consecutifs == 0
    assert contexte.lues == 1
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, contexte, Source("reddit", "c"))
    assert contexte.refus_consecutifs == 1


@pytest.mark.parametrize("code", [401, 403])
async def test_en_mode_jeton_le_meme_code_ne_rend_qu_une_source_muette(code):
    session = Session(Reponse(200, {"access_token": "j"}), Reponse(code))
    collecteur = _reddit()
    contexte = await collecteur.ouvrir(session)
    with pytest.raises(SourceMuette):
        await collecteur.moissonner(session, contexte, Source("reddit", "a"))


async def test_un_cookie_lit_les_memes_publications_qu_un_jeton():
    session = Session(Reponse(200, listing(publication(score=700))))
    collecteur = _reddit(client_id="", client_secret="", cookie=COOKIE)
    contexte = await collecteur.ouvrir(session)
    moisson = await collecteur.moissonner(session, contexte, Source("reddit", "a"))
    assert moisson.elements[0].points == 700
    assert moisson.elements[0].media == "reddit"

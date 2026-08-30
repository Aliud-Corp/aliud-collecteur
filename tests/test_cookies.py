"""Les trois formats qu'un navigateur exporte, et ce qu'on en tire.

CE QUE CES CAS PROTÈGENT
Un cookie est un secret qu'on ne peut pas relire pour y chercher une faute de
frappe. Tout ce qui peut être accepté sans conversion à la main doit l'être, et
tout ce qui est collé de travers doit se dire à la saisie — pas au passage de
06:30.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.aliud_collecteur.cookies import CookieIllisible, lire

DANS_30_JOURS = (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
HIER = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()


# ── L'en-tête brut ──────────────────────────────────────────────────────────

def test_une_ligne_d_en_tete_se_lit_telle_quelle():
    c = lire("reddit_session=abc; token_v2=def", "reddit")
    assert c.entete == "reddit_session=abc; token_v2=def"
    assert c.noms == ["reddit_session", "token_v2"]
    assert c.manquants == []
    assert c.expire_le == "", "un en-tête brut ne porte pas de date, et ne l'invente pas"


def test_le_prefixe_colle_depuis_l_inspecteur_est_toleree():
    assert lire("Cookie: a=1; b=2").entete == "a=1; b=2"


def test_une_saisie_vide_n_est_pas_une_erreur():
    c = lire("   ")
    assert c.vide is True
    assert c.entete == ""


def test_ce_qui_n_est_aucun_des_trois_formats_est_refuse():
    with pytest.raises(CookieIllisible):
        lire("j'ai collé n'importe quoi")


# ── L'export JSON d'une extension ───────────────────────────────────────────

def _json_reddit(expire=DANS_30_JOURS):
    return (
        '[{"name":"reddit_session","value":"abc","domain":".reddit.com",'
        f'"expirationDate":{expire}}},'
        '{"name":"token_v2","value":"def","domain":".reddit.com",'
        f'"expirationDate":{expire + 86400}}},'
        '{"name":"pref_gated_sr_optin","value":"x","domain":".reddit.com",'
        f'"expirationDate":{expire - 86400 * 25}}}]'
    )


def test_un_export_json_devient_un_en_tete():
    c = lire(_json_reddit(), "reddit")
    assert c.entete.startswith("reddit_session=abc; token_v2=def")
    assert "reddit_session" in c.noms and "token_v2" in c.noms


def test_l_expiration_retenue_est_celle_des_cookies_qui_comptent():
    c = lire(_json_reddit(), "reddit")
    # Le cookie de préférence expire bien plus tôt et ne dit rien de la session.
    assert 29 <= (c.jours_restants or 0) <= 30


def test_un_cookie_d_un_autre_site_est_ecarte():
    charge = (
        '[{"name":"reddit_session","value":"abc","domain":".reddit.com"},'
        '{"name":"sid","value":"zzz","domain":".exemple.net"}]'
    )
    c = lire(charge, "reddit")
    assert c.noms == ["reddit_session"]
    assert "zzz" not in c.entete


def test_sans_media_declare_rien_n_est_ecarte():
    charge = '[{"name":"a","value":"1","domain":".exemple.net"}]'
    assert lire(charge).noms == ["a"]


def test_un_export_qui_ne_porte_pas_la_session_le_dit():
    charge = '[{"name":"pref_gated_sr_optin","value":"x","domain":".reddit.com"}]'
    c = lire(charge, "reddit")
    assert c.manquants == ["reddit_session", "token_v2"]


def test_un_json_qui_n_est_pas_une_liste_de_cookies_est_refuse():
    with pytest.raises(CookieIllisible):
        lire('{"erreur": "rien ici"}', "reddit")


# ── Le format Netscape ──────────────────────────────────────────────────────

def test_un_fichier_cookies_txt_se_lit():
    contenu = (
        "# Netscape HTTP Cookie File\n"
        f".reddit.com\tTRUE\t/\tTRUE\t{int(DANS_30_JOURS)}\treddit_session\tabc\n"
        f".reddit.com\tTRUE\t/\tTRUE\t{int(DANS_30_JOURS)}\ttoken_v2\tdef\n"
        ".exemple.net\tTRUE\t/\tFALSE\t0\tsid\tzzz\n"
    )
    c = lire(contenu, "reddit")
    assert c.entete == "reddit_session=abc; token_v2=def"
    assert 29 <= (c.jours_restants or 0) <= 30


def test_une_expiration_a_zero_veut_dire_a_la_fermeture_du_navigateur():
    contenu = ".reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\tabc\n"
    c = lire(contenu, "reddit")
    assert c.entete == "reddit_session=abc"
    assert c.expire_le == "", "zéro n'est pas une date, c'est l'absence de date"


# ── L'expiration, qui est tout l'enjeu ──────────────────────────────────────

def test_un_cookie_expire_rend_un_nombre_de_jours_negatif():
    charge = (
        '[{"name":"reddit_session","value":"abc","domain":".reddit.com",'
        f'"expirationDate":{HIER}}}]'
    )
    assert (lire(charge, "reddit").jours_restants or 0) < 0


def test_sans_date_connue_on_ne_prétend_pas_savoir():
    assert lire("reddit_session=abc", "reddit").jours_restants is None

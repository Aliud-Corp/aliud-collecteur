"""Ce que le relevé porte, où il se pose, et ce qu'il jette.

UN RELEVÉ QUI SE TAIT SUR SES TROUS EST LE SEUL DÉFAUT QUI NE SE RATTRAPE PAS.
Six mois plus tard, un fichier de quarante sources se lit comme un fichier
complet de quarante sources. D'où le premier test.
"""

from __future__ import annotations

import gzip
import json

from custom_components.aliud_collecteur import releve
from custom_components.aliud_collecteur.collecteurs import Element
from custom_components.aliud_collecteur.ordonnanceur import Resultat

DEBUT = "2026-08-28T06:30:12+00:00"


def _element(nom="programming"):
    return Element(
        media="reddit", source=nom, identifiant="t3_abc", titre="un titre",
        url="https://exemple.net", permalien="https://www.reddit.com/r/x/y",
        auteur="quelqu-un", points=1234, commentaires=89,
        cree_le="2026-08-28T04:00:00+00:00", collecte_le=DEBUT,
        brut={"selftext": "le corps d'origine"},
    )


def test_l_en_tete_nomme_les_sources_muettes_et_les_non_lues():
    resultat = Resultat(
        elements=[_element()], debut=DEBUT, fin=DEBUT, secondes=3.5,
        sources_declarees=4, sources_lues=["a"],
        sources_muettes=[{"source": "b", "raison": "r/b n'existe pas"}],
        sources_non_lues=["c", "d"], reprises=["a"],
    )
    contenu = releve.construire(resultat, "reddit")
    passage = contenu["passage"]
    assert passage["complet"] is False
    assert passage["sources_declarees"] == 4
    assert passage["sources_lues"] == 1
    assert passage["sources_muettes"] == [{"source": "b", "raison": "r/b n'existe pas"}]
    assert passage["sources_non_lues"] == ["c", "d"]
    assert passage["reprises_du_passage_precedent"] == ["a"]
    assert contenu["schema"] == 1


def test_un_passage_sans_trou_est_complet():
    resultat = Resultat(
        elements=[], debut=DEBUT, fin=DEBUT, sources_declarees=2,
        sources_lues=["a", "b"],
    )
    assert releve.construire(resultat, "reddit")["passage"]["complet"] is True


def test_la_charge_d_origine_est_gardee_par_defaut_et_se_coupe_a_la_demande():
    resultat = Resultat(elements=[_element()], debut=DEBUT, fin=DEBUT,
                        sources_declarees=1, sources_lues=["programming"])
    assert "brut" in releve.construire(resultat, "reddit")["elements"][0]
    sans = releve.construire(resultat, "reddit", garder_brut=False)
    assert "brut" not in sans["elements"][0]


def test_le_relevé_compresse_se_relit_a_l_identique():
    contenu = releve.construire(
        Resultat(elements=[_element()], debut=DEBUT, fin=DEBUT,
                 sources_declarees=1, sources_lues=["programming"]),
        "reddit",
    )
    octets = releve.compresser(contenu)
    assert octets[:2] == b"\x1f\x8b"
    assert json.loads(gzip.decompress(octets)) == contenu


def test_la_cle_porte_la_date_du_depart_et_ne_s_ecrase_pas():
    assert releve.cle_datee("reddit", DEBUT) == (
        "reddit/2026/08/28/reddit-20260828T063012Z.json.gz"
    )
    assert releve.cle_derniere("reddit") == "reddit/dernier.json.gz"


def test_l_ecriture_est_atomique_et_ne_laisse_pas_de_fichier_partiel(tmp_path):
    chemin = releve.ecrire(tmp_path / "d", "reddit-20260828T063012Z.json.gz", b"\x1f\x8b")
    assert chemin.read_bytes() == b"\x1f\x8b"
    assert list((tmp_path / "d").glob("*.partiel")) == []


def test_l_elagage_garde_les_plus_recents_par_le_nom(tmp_path):
    for jour in range(1, 6):
        (tmp_path / f"reddit-2026080{jour}T060000Z.json.gz").write_bytes(b"x")
    (tmp_path / "x-20260801T060000Z.json.gz").write_bytes(b"x")  # autre média

    supprimes = releve.elaguer(tmp_path, "reddit", garder=2)

    restants = sorted(p.name for p in tmp_path.glob("reddit-*"))
    assert restants == [
        "reddit-20260804T060000Z.json.gz",
        "reddit-20260805T060000Z.json.gz",
    ]
    assert len(supprimes) == 3
    assert (tmp_path / "x-20260801T060000Z.json.gz").exists()


def test_l_elagage_a_zero_ne_supprime_rien(tmp_path):
    (tmp_path / "reddit-20260801T060000Z.json.gz").write_bytes(b"x")
    assert releve.elaguer(tmp_path, "reddit", garder=0) == []
    assert list(tmp_path.glob("reddit-*"))


def test_la_liste_de_sources_s_ecrit_au_premier_passage(tmp_path):
    chemin = tmp_path / "sources-reddit.txt"
    assert releve.lire_sources(chemin, "programming\ndevops\n") == ["programming", "devops"]
    assert chemin.exists()


def test_la_liste_editee_a_la_main_est_nettoyee_sans_etre_reecrite(tmp_path):
    chemin = tmp_path / "sources-reddit.txt"
    chemin.write_text(
        "# un commentaire\n"
        "r/programming\n"
        "  devops  # en bout de ligne\n"
        "\n"
        "programming\n"      # doublon
        "/kubernetes\n",
        encoding="utf-8",
    )
    avant = chemin.read_text(encoding="utf-8")

    assert releve.lire_sources(chemin, "ignoré") == [
        "programming",
        "devops",
        "kubernetes",
    ]
    assert chemin.read_text(encoding="utf-8") == avant


# ── La disposition dans le bucket ───────────────────────────────────────────
#
# Le choix décide de ce qu'un `ls` de préfixe rend : tout un média, ou toute
# une journée. Le nom du fichier ne bouge pas — c'est lui qui distingue deux
# passages du même jour.

def test_la_disposition_par_defaut_range_par_media():
    assert releve.cle_datee("reddit", DEBUT) == (
        "reddit/2026/08/28/reddit-20260828T063012Z.json.gz"
    )


def test_la_disposition_par_date_range_une_journee_ensemble():
    assert releve.cle_datee("reddit", DEBUT, "date_puis_media") == (
        "2026-08-28/reddit/reddit-20260828T063012Z.json.gz"
    )
    assert releve.cle_datee("lobsters", DEBUT, "date_puis_media") == (
        "2026-08-28/lobsters/lobsters-20260828T063012Z.json.gz"
    )


def test_une_disposition_inconnue_retombe_sur_celle_par_defaut():
    assert releve.cle_datee("reddit", DEBUT, "farfelue") == releve.cle_datee(
        "reddit", DEBUT
    )


def test_le_nom_du_fichier_ne_depend_pas_de_la_disposition():
    par_media = releve.cle_datee("reddit", DEBUT).rsplit("/", 1)[1]
    par_date = releve.cle_datee("reddit", DEBUT, "date_puis_media").rsplit("/", 1)[1]
    assert par_media == par_date

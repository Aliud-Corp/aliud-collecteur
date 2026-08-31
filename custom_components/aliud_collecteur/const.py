"""Les noms et les valeurs par défaut, écrits une fois.

LES DÉFAUTS DE DÉBIT NE SONT PAS DE LA PRUDENCE DÉCORATIVE
Reddit accorde 100 requêtes par minute à un client enregistré. Cent sources
tiennent donc dans une minute de budget. `DEBIT_DEFAUT` est à 30, ce qui étale
un passage sur trois minutes et demie et laisse 70 % du budget intact — la
marge sert aux réessais, pas à passer inaperçu. Se cacher n'est pas la stratégie :
`reddit.com/robots.txt` refuse tout agent anonyme quel que soit son rythme, et
la porte est l'enregistrement.

Toutes ces valeurs sont réglables dans les options de l'intégration.
"""

from __future__ import annotations

DOMAIN = "aliud_collecteur"
NOM = "Aliud — collecteur de médias"

PLATEFORMES = ["sensor"]

# Les médias livrés, dans l'ordre où un passage les lit. Reddit est en dernier
# parce qu'il est le seul à exiger des identifiants, et le seul dont la porte
# peut se refermer sans prévenir — relevé du 29/08/2026 : reddit.com bloque un
# client anonyme au niveau réseau, `robots.txt` compris.
MEDIAS = ("rss", "arctic", "hackernews", "lobsters", "reddit")
MEDIAS_SANS_IDENTIFIANTS = ("rss", "arctic", "hackernews", "lobsters")

CONF_MEDIAS = "medias"

# Ce que les trois médias ouverts envoient comme agent. Il se nomme et il porte
# une adresse : un opérateur qui veut savoir qui le lit doit pouvoir le demander.
AGENT_PAR_DEFAUT = (
    "aliud-collecteur/0.3 (+https://github.com/Aliud-Corp/aliud-collecteur)"
)

# ── Configuration, telle que le config flow la range ────────────────────────
CONF_REDDIT_CLIENT_ID = "reddit_client_id"
CONF_REDDIT_CLIENT_SECRET = "reddit_client_secret"
CONF_REDDIT_USER_AGENT = "reddit_user_agent"

# Le cookie de session, autorisé par la clause 4 de l'ADR 0034 depuis le
# 31/08/2026. Il vaut pour un compte du studio, et il pèse plus lourd qu'un
# jeton : il publie, vote et modère. Le client enregistré reste préféré.
CONF_REDDIT_COOKIE = "reddit_cookie"

# Les médias qui savent lire un cookie. `x` y figure avant son collecteur : ce
# qui manque à X est le code qui moissonne, pas la place où ranger sa session.
MEDIAS_A_COOKIE = ("reddit",)


def cle_cookie(media: str) -> str:
    return f"{media}_cookie"


def cle_expiration(media: str) -> str:
    return f"{media}_cookie_expire"


# L'état d'un cookie, tel que le capteur et l'écran le montrent.
COOKIE_ABSENT = "absent"
COOKIE_VALIDE = "valide"
COOKIE_BIENTOT = "bientot"
COOKIE_EXPIRE = "expire"
COOKIE_SANS_DATE = "sans_date"

# Trois jours : de quoi voir l'avertissement un matin et refaire l'export le
# soir. Plus court ne laisserait pas le temps, plus long crierait au loup.
COOKIE_ALERTE_JOURS = 3

CONF_S3_ENDPOINT = "s3_endpoint"
CONF_S3_REGION = "s3_region"
CONF_S3_BUCKET = "s3_bucket"
CONF_S3_ACCESS_KEY = "s3_access_key"
CONF_S3_SECRET_KEY = "s3_secret_key"
CONF_S3_PREFIXE = "s3_prefixe"

# ── Options, réglables après coup ───────────────────────────────────────────
OPT_HEURE = "heure"
OPT_MINUTE = "minute"
OPT_DEBIT = "debit_par_minute"
OPT_GIGUE_MIN = "gigue_min"
OPT_GIGUE_MAX = "gigue_max"
OPT_TENTATIVES = "tentatives_par_source"
OPT_BUDGET = "budget_secondes"
OPT_PAR_SOURCE = "elements_par_source"
OPT_FENETRE = "fenetre"
OPT_DECALAGE = "decalage_jours"
OPT_FENETRE_JOURS = "fenetre_jours"
OPT_GARDER_BRUT = "garder_brut"
OPT_RELEVES_GARDES = "releves_gardes"
OPT_DISPOSITION = "disposition"

HEURE_DEFAUT = 6
MINUTE_DEFAUT = 30
DEBIT_DEFAUT = 30
GIGUE_MIN_DEFAUT = 0.5
GIGUE_MAX_DEFAUT = 2.0
TENTATIVES_DEFAUT = 3
BUDGET_DEFAUT = 1800  # trente minutes ; un passage nominal en prend trois
PAR_SOURCE_DEFAUT = 25
FENETRE_DEFAUT = "day"

# Arctic Shift capture une publication à sa création puis la recapture plus tard.
# Mesuré sur r/programming le 29/08/2026 : à J-0, dix-sept publications sur
# dix-huit sont à un point ; à J-3, le maximum est à 700 points. Classer la
# veille reviendrait donc à classer des zéros, d'où ces deux jours de décalage.
DECALAGE_DEFAUT = 2
FENETRE_JOURS_DEFAUT = 2
GARDER_BRUT_DEFAUT = True
RELEVES_GARDES_DEFAUT = 7

# Comment les relevés se rangent dans le bucket. Deux dispositions, et le choix
# n'est pas cosmétique : il décide de ce qu'un `ls` d'un préfixe rend.
#
#   media_puis_date  reddit/2026/08/31/reddit-20260831T063012Z.json.gz
#                    « tout ce que ce média a rendu », un média à la fois
#   date_puis_media  2026-08-31/reddit/reddit-20260831T063012Z.json.gz
#                    « tout ce qui est tombé ce jour-là », tous médias confondus
#
# La première sert un lecteur qui suit une source. La seconde sert un lecteur
# qui reprend une journée — c'est celle qu'on veut quand l'archive se relit par
# date plutôt que par origine.
DISPOSITIONS = ("media_puis_date", "date_puis_media")
DISPOSITION_DEFAUT = "media_puis_date"

# En dessous de ce reste annoncé par la source, l'ordonnanceur étire son
# intervalle pour tenir jusqu'à la remise à zéro.
SEUIL_DE_FREIN = 20

# ── Chemins et fichiers ─────────────────────────────────────────────────────
DOSSIER = "aliud_collecteur"
FICHIER_SOURCES = "sources-{media}.txt"
SCHEMA_RELEVE = 1

# Vingt passages, soit trois semaines à raison d'un par jour. Assez pour voir
# une source qui se tait toujours, assez peu pour que l'état reste lisible.
JOURNAL_MAX = 20

STOCKAGE_VERSION = 1
STOCKAGE_CLE = f"{DOMAIN}.etat"

SERVICE_COLLECTER = "collecter"
SIGNAL_PASSAGE = f"{DOMAIN}_passage"

RESULTAT_SUCCES = "succes"
RESULTAT_PARTIEL = "partiel"
RESULTAT_ECHEC = "echec"

# Ce que le relevé est devenu. Distinct du verdict du passage : une collecte
# complète qui n'a nulle part où aller reste une collecte complète, et c'est ce
# mot-là qui dit qu'aucun octet n'est parti.
DEPOT_ENVOYE = "envoye"
DEPOT_NON_CONFIGURE = "non_configure"
DEPOT_DESACTIVE = "desactive"
DEPOT_REFUSE = "refuse"

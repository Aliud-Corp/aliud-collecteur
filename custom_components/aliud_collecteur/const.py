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

# ── Configuration, telle que le config flow la range ────────────────────────
CONF_REDDIT_CLIENT_ID = "reddit_client_id"
CONF_REDDIT_CLIENT_SECRET = "reddit_client_secret"
CONF_REDDIT_USER_AGENT = "reddit_user_agent"

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
OPT_GARDER_BRUT = "garder_brut"
OPT_RELEVES_GARDES = "releves_gardes"

HEURE_DEFAUT = 6
MINUTE_DEFAUT = 30
DEBIT_DEFAUT = 30
GIGUE_MIN_DEFAUT = 0.5
GIGUE_MAX_DEFAUT = 2.0
TENTATIVES_DEFAUT = 3
BUDGET_DEFAUT = 1800  # trente minutes ; un passage nominal en prend trois
PAR_SOURCE_DEFAUT = 25
FENETRE_DEFAUT = "day"
GARDER_BRUT_DEFAUT = True
RELEVES_GARDES_DEFAUT = 7

# En dessous de ce reste annoncé par la source, l'ordonnanceur étire son
# intervalle pour tenir jusqu'à la remise à zéro.
SEUIL_DE_FREIN = 20

# ── Chemins et fichiers ─────────────────────────────────────────────────────
DOSSIER = "aliud_collecteur"
FICHIER_SOURCES = "sources-{media}.txt"
SCHEMA_RELEVE = 1

STOCKAGE_VERSION = 1
STOCKAGE_CLE = f"{DOMAIN}.etat"

SERVICE_COLLECTER = "collecter"
SIGNAL_PASSAGE = f"{DOMAIN}_passage"

RESULTAT_SUCCES = "succes"
RESULTAT_PARTIEL = "partiel"
RESULTAT_ECHEC = "echec"

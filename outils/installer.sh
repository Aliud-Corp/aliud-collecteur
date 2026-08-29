#!/usr/bin/env bash
# Pose le greffon sur un Home Assistant, depuis ce dépôt, en une commande.
#
# POURQUOI CE SCRIPT PLUTÔT QU'UNE LISTE D'ÉTAPES
# Copier un répertoire dans `/config/custom_components` puis redémarrer, c'est
# quatre commandes qu'on retape de mémoire et qu'on rate une fois sur trois. Ce
# qui suit est la même chose, écrite une fois, avec ses contrôles préalables.
#
# CE QU'IL SUPPOSE
# L'add-on « Advanced SSH & Web Terminal » démarré, et une clé publique déclarée
# dans sa configuration. Le module se copie par `tar` sur un tube plutôt que par
# `scp -r` : l'add-on n'embarque pas toujours `scp` côté serveur, alors que `tar`
# est dans BusyBox.
#
# CE QU'IL NE FAIT PAS
# Il ne configure rien. Les identifiants Reddit se saisissent à l'écran, après le
# redémarrage, dans Paramètres → Appareils et services → Ajouter une intégration.

set -euo pipefail

HOTE="${1:-${HA_HOTE:-homeassistant.local}}"
PORT="${HA_PORT:-22}"
UTILISATEUR="${HA_UTILISATEUR:-root}"
CIBLE="${HA_CONFIG:-/config}"
REDEMARRER="${HA_REDEMARRER:-demander}"

ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE="$ICI/custom_components/aliud_collecteur"
DOMAINE="aliud_collecteur"

echoerr() { printf '%s\n' "$*" >&2; }

usage() {
  cat <<'USAGE'
Usage : outils/installer.sh [hôte]

  hôte              Nom ou adresse du Home Assistant. Défaut : homeassistant.local

Variables :
  HA_PORT           Port SSH de l'add-on. Défaut : 22
  HA_UTILISATEUR    Défaut : root
  HA_CONFIG         Répertoire de configuration. Défaut : /config
  HA_REDEMARRER     oui | non | demander (défaut)

Exemples :
  outils/installer.sh
  outils/installer.sh 192.168.1.42
  HA_PORT=22222 HA_REDEMARRER=oui outils/installer.sh homeassistant.local
USAGE
}

case "${1:-}" in -h|--help|help) usage; exit 0 ;; esac

[ -d "$MODULE" ] || { echoerr "introuvable : $MODULE"; exit 2; }
[ -f "$MODULE/manifest.json" ] || { echoerr "pas de manifest.json dans $MODULE"; exit 2; }

ssh_ha() { ssh -p "$PORT" -o BatchMode=yes -o ConnectTimeout=8 "$UTILISATEUR@$HOTE" "$@"; }

printf 'Hôte    : %s@%s:%s\n' "$UTILISATEUR" "$HOTE" "$PORT"
printf 'Module  : %s\n' "$(grep -o '"version": *"[^"]*"' "$MODULE/manifest.json" | cut -d'"' -f4)"

if ! ssh_ha true 2>/dev/null; then
  echoerr ""
  echoerr "SSH n'a pas répondu sur $UTILISATEUR@$HOTE:$PORT."
  echoerr ""
  echoerr "Ce qui manque, dans l'ordre où ça se vérifie :"
  echoerr "  1. L'add-on « Advanced SSH & Web Terminal » est démarré."
  echoerr "  2. Sa configuration porte ta clé publique dans 'authorized_keys',"
  echoerr "     et son option 'Protection Mode' est désactivée pour que"
  echoerr "     'ha core restart' fonctionne depuis la session."
  echoerr "  3. Le port : l'add-on écoute souvent sur un autre que 22."
  echoerr "     Le sien se lit dans son onglet Configuration → Network."
  echoerr "  4. '$HOTE' se résout. Sinon, passe l'adresse IP en argument."
  exit 3
fi

ssh_ha "mkdir -p '$CIBLE/custom_components'"

# L'ancien module part avant que le neuf arrive : un fichier retiré d'une version
# à l'autre resterait sinon en place, et Python l'importerait encore.
ssh_ha "rm -rf '$CIBLE/custom_components/$DOMAINE'"

tar -C "$ICI/custom_components" \
    --exclude='__pycache__' --exclude='*.pyc' \
    -czf - "$DOMAINE" \
  | ssh_ha "tar -C '$CIBLE/custom_components' -xzf -"

POSE=$(ssh_ha "ls '$CIBLE/custom_components/$DOMAINE' | wc -l" | tr -d ' ')
printf 'Posé    : %s fichiers dans %s/custom_components/%s\n' "$POSE" "$CIBLE" "$DOMAINE"

if [ "$REDEMARRER" = "demander" ]; then
  printf '\nRedémarrer Home Assistant maintenant ? [o/N] '
  read -r reponse
  case "$reponse" in [oO]*) REDEMARRER=oui ;; *) REDEMARRER=non ;; esac
fi

if [ "$REDEMARRER" = "oui" ]; then
  echo "Redémarrage…"
  ssh_ha "ha core restart"
  echo "Fait. Paramètres → Appareils et services → Ajouter une intégration → Aliud."
else
  cat <<'SUITE'

Le module est posé, pas encore chargé : Home Assistant n'importe les
custom_components qu'au démarrage.

  Paramètres → Système → Redémarrer
  puis Paramètres → Appareils et services → Ajouter une intégration → Aliud

Laisse les cinq champs de stockage vides tant que le bucket n'existe pas.
SUITE
fi

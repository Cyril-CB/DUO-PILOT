#!/usr/bin/env bash
# Fresh dedicated Ubuntu VPS installation. Authentication stays on this host.
set -euo pipefail
umask 077
trap 'echo "Installation interrompue. Les fichiers personnels sont conservés. Si des services ont déjà démarré, consulte le dépannage dans deploy/README-VPS.md avant de relancer." >&2' ERR
if [[ $EUID -ne 0 ]]; then
    echo 'Lancer avec sudo bash deploy/install-ubuntu.sh' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ! ${VERSION_ID:-} =~ ^(24\.04|26\.04)$ || $(uname -m) != x86_64 ]]; then
    echo 'Cet installateur cible Ubuntu LTS 24.04 ou 26.04, x86_64.' >&2
    exit 1
fi
APP=/opt/duopilot/app
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -f /opt/duopilot/.installed-v0.4.1 ]]; then
    echo 'DUO PILOT est déjà installé. Suite : sudo duopilotctl config ; voir deploy/README-VPS.md.'
    exit 0
fi
for folder in /opt/duopilot /opt/duopilot/app /opt/duopilot/venv /home/duopilot /var/backups/duopilot; do
    if [[ -L $folder ]]; then echo "Lien symbolique refusé : $folder" >&2; exit 1; fi
done
if systemctl is-active --quiet duopilot-worker.service || systemctl is-active --quiet duopilot-web.service; then
    echo 'Une installation tourne déjà. Utilise la procédure de mise à jour documentée.' >&2
    exit 1
fi
# Do not remove another workload’s packages or replace a pre-existing app.
if [[ -f $APP/app.py && ! -f /opt/duopilot/.install-in-progress ]]; then
    echo 'Dossier application déjà présent. Arrêt sans remplacement ; voir la procédure de migration.' >&2
    exit 1
fi
for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
    package_status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
    if [[ $package_status == 'install ok installed' ]]; then
        echo "Le paquet $package doit être examiné avant d’installer Docker CE. Aucun paquet existant supprimé." >&2
        exit 1
    fi
done
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv unzip

install -d -m 0755 /etc/apt/keyrings
curl --fail --silent --show-error --location --retry 3 \
    https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
chmod 0644 /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker.service

if ! id duopilot >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash duopilot
elif [[ $(getent passwd duopilot | cut -d: -f6) != /home/duopilot ]]; then
    echo 'Le compte duopilot existe avec un autre dossier personnel. Arrêt sans modification du compte.' >&2
    exit 1
fi
usermod -aG docker duopilot
chmod 0700 /home/duopilot
install -d -m 0755 /opt/duopilot
touch /opt/duopilot/.install-in-progress
install -d -o duopilot -g duopilot -m 0700 "$APP" "$APP/instance" /var/backups/duopilot
install -d -o duopilot -g duopilot -m 0755 /opt/duopilot/venv
install -d -o duopilot -g duopilot -m 0700 /home/duopilot/.codex /home/duopilot/.claude
install -d -o duopilot -g duopilot -m 0755 /home/duopilot/.local /home/duopilot/.local/bin

# Copy packaged code only. Never copy or overwrite an environment or a database.
python3 - "$SOURCE_DIR" "$APP" <<'PY'
from pathlib import Path
import shutil
import sys
source, target = map(Path, sys.argv[1:])
excluded = {'.env', 'instance', '.venv', 'venv', '.git', '__pycache__', '.pytest_cache'}
for item in source.rglob('*'):
    relative = item.relative_to(source)
    if any(p in excluded for p in relative.parts) or item.suffix in {'.pyc', '.pyo'}:
        continue
    if item.is_symlink():
        raise SystemExit('Archive contenant un lien inattendu ; copie arrêtée.')
    destination = target / relative
    if destination.is_symlink():
        raise SystemExit('Installation contenant un lien inattendu ; copie arrêtée.')
    if item.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
PY
chown -R duopilot:duopilot "$APP"

# runuser preserves cwd. Do not inherit a private /home/ubuntu directory that
# duopilot cannot enter again (notably when the CLI installer invokes find).
cd "$APP"

as_duo() {
    runuser -u duopilot -- env -i \
        HOME=/home/duopilot USER=duopilot LOGNAME=duopilot LANG=C.UTF-8 \
        PATH=/home/duopilot/.local/bin:/usr/local/bin:/usr/bin:/bin \
        CODEX_HOME=/home/duopilot/.codex CLAUDE_CONFIG_DIR=/home/duopilot/.claude "$@"
}
as_duo python3 -m venv /opt/duopilot/venv
as_duo /opt/duopilot/venv/bin/python -m pip install --upgrade pip
as_duo /opt/duopilot/venv/bin/python -m pip install -r "$APP/requirements-vps.txt"
as_duo /opt/duopilot/venv/bin/python "$APP/deploy/configure.py" init

# Official installers run under the service account. Nothing authenticates as root.
CLI_TMP=$(mktemp -d /tmp/duopilot-install.XXXXXXXX)
chown duopilot:duopilot "$CLI_TMP"
cleanup() { rm -rf -- "$CLI_TMP"; }
trap cleanup EXIT
if [[ ! -x /home/duopilot/.local/bin/codex ]]; then
    curl --fail --silent --show-error --location --retry 3 \
        https://chatgpt.com/codex/install.sh -o "$CLI_TMP/codex.sh"
    chown duopilot:duopilot "$CLI_TMP/codex.sh"
    as_duo env CODEX_NON_INTERACTIVE=1 sh "$CLI_TMP/codex.sh"
fi
if [[ ! -x /home/duopilot/.local/bin/claude ]]; then
    curl --fail --silent --show-error --location --retry 3 \
        https://claude.ai/install.sh -o "$CLI_TMP/claude.sh"
    chown duopilot:duopilot "$CLI_TMP/claude.sh"
    as_duo bash "$CLI_TMP/claude.sh" stable
fi
as_duo /home/duopilot/.local/bin/codex --version
as_duo /home/duopilot/.local/bin/claude --version
as_duo /opt/duopilot/venv/bin/python -c 'from duopilot import create_app; create_app()'

install -m 0755 "$APP/deploy/duopilotctl" /usr/local/bin/duopilotctl
for unit in "$APP"/deploy/systemd/duopilot-*; do
    install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --now duopilot-web.service duopilot-worker.service
systemctl enable --now duopilot-backup.timer
# The AI timer stays off until CLI authentication and explicit activation.
systemctl disable --now duopilot-daily.timer
WEB_READY=0
for attempt in {1..20}; do
    if curl --fail --silent --max-time 3 --output /dev/null http://127.0.0.1:5055/; then
        WEB_READY=1
        break
    fi
    sleep 1
done
if [[ $WEB_READY != 1 ]]; then
    echo 'L’interface ne répond pas. Consulter sudo duopilotctl logs.' >&2
    exit 1
fi
systemctl is-active --quiet duopilot-worker.service
systemctl start duopilot-backup.service
touch /opt/duopilot/.installed-v0.4.1
rm -f /opt/duopilot/.install-in-progress
echo 'Installation terminée. Interface : 127.0.0.1:5055 sur le VPS, par tunnel SSH.'
echo 'Suite : sudo duopilotctl login-codex ; sudo duopilotctl login-claude ; sudo duopilotctl config'
echo 'Guide : /opt/duopilot/app/deploy/README-VPS.md'

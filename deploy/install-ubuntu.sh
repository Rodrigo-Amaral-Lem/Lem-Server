#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# First-install host tool, not a routine update: run as the installation account with sudo rights.
# Changes host packages/firewall, generates private .env/credentials, creates volumes and starts services.
# Checks existing installation/database markers before creating credentials. See docs/deployment.md.

# First install on a fresh Ubuntu 24.04/26.04 server. Run as lem, not with sudo bash.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
fail() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }
trap 'printf "Instalação interrompida. Consulte o erro acima; não apague os volumes Docker.\n" >&2' ERR
[[ $EUID -ne 0 ]] || fail 'Execute como lem: bash deploy/install-ubuntu.sh (o script usa sudo quando necessário).'
[[ -f ../server/run.py && -f ../shared/checklist-v1.json ]] || fail 'Faltam ficheiros: copie e extraia o projeto completo.'
source /etc/os-release
[[ ${ID:-} == ubuntu ]] || fail 'Este instalador requer Ubuntu 24.04 ou 26.04 LTS.'
case ${VERSION_ID:-} in
    24.04) lem_ubuntu_suite=noble ;;
    26.04) lem_ubuntu_suite=resolute ;;
    *) fail 'Este instalador foi preparado para Ubuntu 24.04 ou 26.04 LTS.' ;;
esac
[[ ! -e .env ]] || fail 'Já existe deploy/.env. Não será substituído. Este instalador é apenas para a primeira instalação.'
[[ -t 0 ]] || fail 'Execute num terminal interativo local ou SSH para escolher o email do administrador.'
command -v sudo >/dev/null || fail 'Instale sudo e atribua as permissões ao utilizador lem.'
sudo -v
# Do not replace a web service which may have been installed since preparation.
if [[ -n $(sudo ss -H -ltn '( sport = :80 or sport = :443 )') ]]; then
    fail 'Portas 80/443 ocupadas. Reveja os serviços existentes antes de instalar.'
fi
lem_domain=${LEM_DOMAIN:-lem.rodrod.pt}
[[ $lem_domain =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]] || fail 'Domínio inválido.'
getent ahosts "$lem_domain" >/dev/null || fail 'O domínio ainda não resolve no DNS.'
printf 'Domínio: %s\n' "$lem_domain"
read -r -p 'Nome da primeira conta de administrador: ' lem_admin_name
lem_admin_name=$(python3 -c 'import sys; n=sys.argv[1].strip(); print(n); sys.exit(0 if 1 <= len(n) <= 120 and n.isprintable() else 1)' "$lem_admin_name") || fail 'Indique um nome entre 1 e 120 caracteres, sem caracteres de controlo.'
read -r -p 'Email do primeiro administrador [admin@lem.pt]: ' lem_admin_email
lem_admin_email=${lem_admin_email:-admin@lem.pt}
[[ $lem_admin_email =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || fail 'Email inválido.'
lem_ssh_port=${LEM_SSH_PORT:-22}
if [[ -n ${SSH_CONNECTION:-} && -z ${LEM_SSH_PORT:-} ]]; then
    read -r _ _ _ lem_ssh_port <<< "$SSH_CONNECTION"
fi
[[ $lem_ssh_port =~ ^[0-9]+$ ]] && ((lem_ssh_port>=1 && lem_ssh_port<=65535)) || fail 'Porta SSH inválida.'
printf 'A instalar Docker/PostgreSQL/API/HTTPS; SSH preservado na porta %s.\n' "$lem_ssh_port"
sudo apt-get update
sudo apt-get install -y ca-certificates curl openssl ufw
if ! command -v docker >/dev/null; then
    # Inspect conflicting distro packages without silently removing them.
    conflict=$(dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n' docker.io docker-compose docker-compose-v2 podman-docker containerd runc 2>/dev/null || true)
    if [[ $conflict == *' installed'* ]]; then fail 'Há pacotes Docker/containerd de outra origem. Reveja-os antes de instalar Docker CE.'; fi
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$lem_ubuntu_suite" "$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker
sudo docker compose version
# A fresh .env must never be paired with a previously initialized database.
[[ -z $(sudo docker ps -aq --filter label=com.docker.compose.project=lem) ]] || fail 'Já existem contentores Lem. Use a atualização ou desinstale a instalação anterior antes de instalar.'
if sudo docker volume inspect lem_pgdata >/dev/null 2>&1; then
    fail 'Já existe o volume lem_pgdata. Não serão geradas novas credenciais para uma base de dados existente. Use a atualização ou recupere a configuração anterior.'
fi
# Random hex strings avoid dotenv/URL escaping errors. No password is sent to ChatGPT.
lem_db_password=$(openssl rand -hex 24)
lem_admin_password=$(openssl rand -hex 24)
# noclobber makes creation atomic and prevents replacement of an existing configuration.
(set -o noclobber; printf 'LEM_DOMAIN=%s\nPOSTGRES_PASSWORD=%s\nADMIN_EMAIL=%s\nADMIN_PASSWORD=%s\nLEM_MIN_CLIENT_VERSION=0.2.0\nLEM_LATEST_CLIENT_VERSION=0.2.0\nLEM_UPDATE_URL=\n' "$lem_domain" "$lem_db_password" "$lem_admin_email" "$lem_admin_password" > .env)
chmod 600 .env
python3 - "$lem_admin_name" <<'PY'
import sys
# Single-quoted dotenv values preserve names containing spaces, dollars or #.
with open('.env', 'a') as stream:
    stream.write("ADMIN_NAME='" + sys.argv[1].replace("'", "\\'") + "'\n")
PY
printf '\nCredenciais iniciais guardadas em %s/.env\nNome: %s\nEmail: %s\nPassword inicial: %s\n' "$PWD" "$lem_admin_name" "$lem_admin_email" "$lem_admin_password"
printf 'Guarde a password num gestor de passwords. Ainda falta iniciar e verificar os serviços.\n'
unset lem_db_password lem_admin_password
sudo ufw allow "$lem_ssh_port/tcp"
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo docker volume create lem_caddydata >/dev/null
sudo docker volume create lem_caddyconfig >/dev/null
sudo docker compose config --quiet
sudo docker compose up -d --build
printf '\nA aguardar a API e o certificado HTTPS…\n'
lem_ready=0
for ((i=0;i<36;i++)); do
    if curl --fail --silent --show-error --connect-timeout 4 --max-time 6 "https://$lem_domain/api/health" > /tmp/LEM-health-$$.json 2>/dev/null; then lem_ready=1;break;fi
    sleep 5
done
rm -f /tmp/LEM-health-$$.json
if [[ $lem_ready != 1 ]]; then
    printf 'Os serviços foram iniciados, mas a verificação HTTPS falhou.\nVerifique DNS A/AAAA e portas 80/443 na firewall do fornecedor.\nDiagnóstico: cd %q && sudo docker compose logs --tail=80 api proxy\n' "$PWD" >&2
    exit 1
fi
printf '\nServiço disponível em https://%s\nEmail inicial: %s\n' "$lem_domain" "$lem_admin_email"
printf 'A password inicial foi apresentada após criar deploy/.env. Para a consultar localmente: sed -n '\''s/^ADMIN_PASSWORD=//p'\'' deploy/.env (a partir da pasta do projeto).\n'
printf 'Na app: Conta → servidor HTTPS acima → entrar.\n'
printf 'Backup: cd %q && ./backup.sh\n' "$PWD"

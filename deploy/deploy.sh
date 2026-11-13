#!/usr/bin/env bash
# Stages: validate local arguments -> package -> SSH transfer -> remote bootstrap.
# See docs/developer/platform-and-deployment.md for the local/remote responsibility split.
# Run on the personal Linux laptop, not on the server.
set -euo pipefail
umask 077
fail() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'HELP'
Uso: bash deploy/deploy.sh [opções]
Atualiza uma instalação existente por SSH, com backup e conservação dos dados.

  --host HOST          Servidor/alias SSH (lem.rodrod.pt)
  --user USER          Utilizador SSH (lem)
  --port PORT          Porta SSH (22)
  --key FILE           Chave privada; por defeito usa a configuração SSH/agent
  --remote-path PATH   Instalação remota (auto: única instalação em ~/apps)
  --domain DOMAIN      Confirmar o domínio existente, sem alterar .env
  --project-path PATH  Pasta LEM; por defeito a pasta que contém deploy/
  --package-only FILE  Criar ZIP de deployment sem ligar ao servidor
  --reset-database     APAGA a base de testes; conserva .env e certificados
  -h, --help           Mostrar esta ajuda

Exemplo: bash deploy/deploy.sh --key ~/.ssh/lem_server
HELP
}
lem_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
lem_project=$(cd -- "$lem_script_dir/.." && pwd -P)
lem_host=lem.rodrod.pt
lem_user=lem
lem_port=22
lem_key=''
lem_remote_path=auto
lem_domain=''
lem_output=''
lem_reset=0
while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --reset-database) lem_reset=1;shift ;;
        --host|--user|--port|--key|--remote-path|--domain|--project-path|--package-only)
            (($#>=2)) && [[ -n $2 ]] || fail "Falta o valor de $1."
            case "$1" in
                --host) lem_host=$2;; --user) lem_user=$2;; --port) lem_port=$2;;
                --key) lem_key=$2;; --remote-path) lem_remote_path=$2;; --domain) lem_domain=$2;;
                --project-path) lem_project=$2;; --package-only) lem_output=$2;;
            esac
            shift 2 ;;
        *) fail "Opção desconhecida: $1. Use --help." ;;
    esac
done
[[ $lem_host =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail 'Host inválido.'
[[ $lem_user =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || fail 'Utilizador inválido.'
[[ $lem_port =~ ^[0-9]{1,5}$ ]] && ((10#$lem_port>=1 && 10#$lem_port<=65535)) || fail 'Porta inválida.'
lem_port=$((10#$lem_port))
[[ -z $lem_domain || $lem_domain =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]] || fail 'Domínio inválido.'
[[ $lem_remote_path != *$'\n'* && $lem_remote_path != *$'\r'* ]] || fail 'Caminho remoto inválido.'
[[ -d $lem_project ]] || fail 'Pasta do projeto inexistente.'
lem_project=$(cd -- "$lem_project" && pwd -P)
for file in server/run.py server/requirements.txt shared/checklist-v1.json deploy/compose.yml deploy/remote-deploy.sh deploy/find-installation.py deploy/prepare-config.py deploy/deploy-package.py deploy/deploy-bootstrap.py; do
    [[ -f $lem_project/$file ]] || fail "Projeto incompleto: $file"
done
command -v python3 >/dev/null || fail 'Instale Python 3 no laptop.'
if [[ -n $lem_key ]]; then
    [[ -f $lem_key && -r $lem_key ]] || fail 'Chave SSH inexistente ou sem acesso de leitura.'
    lem_key=$(python3 -c 'import pathlib,sys;print(pathlib.Path(sys.argv[1]).resolve())' "$lem_key")
fi
lem_temp=$(mktemp -d -t lem-deploy-XXXXXXXX)
trap 'rm -rf -- "$lem_temp"' EXIT
lem_release=$(python3 -c 'import uuid;print(uuid.uuid4().hex)')
lem_remote_stage=".cache/lem-deploy-$lem_release"
lem_archive="$lem_temp/payload.zip"
lem_sha=$(python3 "$lem_project/deploy/deploy-package.py" "$lem_project" "$lem_archive")
if [[ -n $lem_output ]]; then
    cp -- "$lem_archive" "$lem_output"
    printf 'Pacote criado sem ligação ao servidor: %s\nSHA256: %s\n' "$lem_output" "$lem_sha"
    exit 0
fi
for tool in ssh scp; do command -v "$tool" >/dev/null || fail "Instale OpenSSH Client ($tool) no laptop."; done
lem_ssh=(-o ConnectTimeout=15 -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -p "$lem_port")
lem_scp=(-o ConnectTimeout=15 -P "$lem_port")
if [[ -n $lem_key ]]; then lem_ssh+=(-i "$lem_key");lem_scp+=(-i "$lem_key");fi
lem_target="$lem_user@$lem_host"
if [[ $lem_reset == 1 ]]; then
    printf 'MODO RESET: os dados da base de testes serão apagados; .env e certificados conservados.\n'
else
    printf 'MODO ATUALIZAÇÃO: dados conservados e backup automático antes de substituir código.\n'
fi
printf 'Destino SSH: %s | Pasta: %s\n' "$lem_target" "$lem_remote_path"
ssh "${lem_ssh[@]}" "$lem_target" "command -v python3 >/dev/null && command -v docker >/dev/null && umask 077 && mkdir -p $lem_remote_stage"
scp "${lem_scp[@]}" "$lem_archive" "$lem_target:$lem_remote_stage/payload.zip"
scp "${lem_scp[@]}" "$lem_project/deploy/deploy-bootstrap.py" "$lem_target:$lem_remote_stage/bootstrap.py"
# Remote paths/domain are data encoded as JSON, never evaluated as shell syntax.
lem_config=$(python3 - "$lem_remote_path" "$lem_reset" "$lem_release" "$lem_domain" "$lem_sha" "$lem_remote_stage" <<'PY'
import base64,json,sys
path,reset,release,domain,sha,stage=sys.argv[1:]
print(base64.b64encode(json.dumps(dict(remote_path=path,reset=reset=='1',release=release,domain=domain,archive_sha=sha,transfer_dir=stage)).encode()).decode())
PY
)
# A PTY permits sudo/passphrase interaction directly through SSH.
if ! ssh "${lem_ssh[@]}" -tt "$lem_target" "python3 $lem_remote_stage/bootstrap.py $lem_config"; then
    printf 'Deploy falhou. A pasta temporária remota foi conservada para diagnóstico: ~/%s\n' "$lem_remote_stage" >&2
    exit 1
fi
ssh "${lem_ssh[@]}" "$lem_target" "rm -rf -- $lem_remote_stage"
printf 'Deploy terminado. Recompile a app Qt separadamente quando necessário.\n'

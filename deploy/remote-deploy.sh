#!/usr/bin/env bash
# Internal host pipeline: positional TARGET RESET RELEASE DOMAIN, supplied by validated SSH frontends.
# Mutates the selected installation, builds containers, backs up or explicitly resets data and checks health.
# Failure is not transactional rollback. Preserve config/TLS volumes and inspect errors before retrying.

# Stages: validate/lock -> inspect preserved volumes -> build -> backup/reset -> replace -> health check.
# Do not reorder these operations: building precedes stopping the running API.
# Shared remote operation for deploy.ps1 and deploy.sh.
set -euo pipefail
umask 077
fail(){ printf 'ERRO: %s\n' "$*" >&2;exit 1; }
trap 'printf "Deploy interrompido (linha %s). Consulte o erro; não remova volumes de certificados.\n" "$LINENO" >&2' ERR
[[ $# -eq 4 ]] || fail 'Uso interno: remote-deploy.sh TARGET RESET RELEASE DOMAIN'
lem_target=$1;lem_reset=$2;lem_release=$3;lem_expected_domain=$4
[[ $lem_reset == 0 || $lem_reset == 1 ]] || fail 'Flag reset inválida.'
[[ $lem_release =~ ^[a-f0-9]{32}$ ]] || fail 'Release inválida.'
lem_source=$(cd "$(dirname "$0")/.." && pwd -P)
lem_stage=$(dirname "$lem_source")
lem_target=$(realpath -e -- "$lem_target")
[[ $lem_target != / && $lem_target != "$HOME" && $lem_target != "$lem_source" ]] || fail 'Diretório remoto inválido.'
for file in deploy/compose.yml deploy/.env server/run.py shared/checklist-v1.json;do [[ -f $lem_target/$file ]] || fail 'Instalação existente incompleta.';done
[[ -w $lem_target && -w $lem_target/deploy ]] || fail 'Instalação sem acesso de escrita.'
for file in server/run.py server/requirements.txt shared/checklist-v1.json deploy/compose.yml deploy/Caddyfile deploy/Dockerfile deploy/prepare-config.py;do [[ -f $lem_source/$file ]] || fail "Pacote incompleto: $file";done
for tool in python3 docker curl flock;do command -v "$tool" >/dev/null || fail "Falta instalar: $tool";done
exec 9>"$lem_target/deploy/.deploy.lock";flock -n 9 || fail 'Já existe outro deploy em execução.'
sudo -v
cd "$lem_target/deploy"
sudo docker compose version
# Save only non-secret identifiers from the old Compose configuration.
sudo docker compose config --format json | python3 -c 'import json,sys
c=json.load(sys.stdin);db=c.get("services",{}).get("db",{}).get("environment",{})
print(json.dumps({"name":c["name"],"volumes":{k:{"name":v["name"]} for k,v in c["volumes"].items()},"database":{k:db.get(k,"") for k in ("POSTGRES_USER","POSTGRES_DB")}}))' > "$lem_stage/old-metadata.json"
lem_old_project=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$lem_stage/old-metadata.json")
[[ $lem_old_project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail 'Projeto Compose inválido.'
lem_domain=$(python3 "$lem_source/deploy/prepare-config.py" --domain "$lem_target/deploy/.env")
[[ -z $lem_expected_domain || $lem_expected_domain == "$lem_domain" ]] || fail 'Domain difere do domínio configurado; configuração não alterada.'
if ! python3 -c 'import json,sys
c=json.load(open(sys.argv[1]));sys.exit(0 if c["name"]=="lem" and c["database"]=={"POSTGRES_USER":"lem","POSTGRES_DB":"lem"} else 1)' "$lem_stage/old-metadata.json";then
    [[ $lem_reset == 1 ]] || fail 'A mudança de nomes do servidor precisa de recriar a base de dados. Use --reset-database no Linux ou -ResetDatabase no PowerShell. Nenhum dado foi apagado.'
fi
lem_old_db_volume=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["volumes"]["pgdata"]["name"])' "$lem_stage/old-metadata.json")
[[ $lem_old_db_volume =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail 'Volume de dados inválido.'
if [[ $lem_old_project != lem ]];then
    if sudo docker volume inspect lem_pgdata >/dev/null 2>&1;then fail 'Já existe lem_pgdata. Não será apagado um volume de outra instalação.';fi
    [[ -z $(sudo docker ps --all --filter label=com.docker.compose.project=lem --format '{{.ID}}') ]] || fail 'Já existe outro projeto lem neste servidor.'
fi
# The prepared .env stays outside the Docker build context and is not printed.
python3 "$lem_source/deploy/prepare-config.py" "$lem_target/deploy/.env" "$lem_stage/old-metadata.json" "$lem_stage/next.env"
for lem_tls_key in caddydata caddyconfig;do
    lem_tls_volume=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["volumes"][sys.argv[2]]["name"])' "$lem_stage/old-metadata.json" "$lem_tls_key")
    sudo docker volume inspect "$lem_tls_volume" >/dev/null || fail "Volume HTTPS em falta: $lem_tls_volume. Nada foi apagado."
done
lem_old_dc=(sudo docker compose --project-name "$lem_old_project" --env-file "$lem_target/deploy/.env" -f "$lem_target/deploy/compose.yml")
lem_staged_dc=(sudo docker compose --project-name lem --env-file "$lem_stage/next.env" -f "$lem_source/deploy/compose.yml")
"${lem_staged_dc[@]}" config --quiet
printf '\nA construir a API LEM antes de parar serviços…\n'
"${lem_staged_dc[@]}" build api
if [[ $lem_reset == 1 ]];then
    sudo docker volume inspect "$lem_old_db_volume" | python3 -c 'import json,sys
v=json.load(sys.stdin)[0];labels=v.get("Labels") or {}
if labels.get("com.docker.compose.project")!=sys.argv[1] or labels.get("com.docker.compose.volume")!="pgdata":raise SystemExit("Volume de outra instalação; reset recusado")' "$lem_old_project"
    printf '\nRESET LEM: serão apagados utilizadores, contratos, carregadores, reports e fotografias.\n'
else
    mkdir -p "$lem_target/deploy/backups";chmod 700 "$lem_target/deploy/backups"
    lem_backup="$lem_target/deploy/backups/lem-before-$lem_release.dump"
    printf '\nA criar backup da base de dados; a API continua em execução.\nDestino: %s\n' "$lem_backup"
    printf 'A validar autorização sudo para o backup…\n'
    sudo -v
    # A large build can outlive sudo's credential cache. Authenticate explicitly
    # above; never leave an invisible password prompt inside the redirected dump.
    # Verbose output goes to stderr; stdout remains a valid custom-format dump.
    # Compose keeps stdin attached even with -T. Disable interaction and detach
    # stdin to avoid terminal job-control suspension (SIGTTIN) over SSH/sudo.
    # Bound connection and lock waits without limiting the size of a valid dump.
    if ! sudo -n "${lem_old_dc[@]:1}" exec -T --interactive=false -e PGCONNECT_TIMEOUT=15 db pg_dump -U lem -d lem --no-password --format=custom --verbose --lock-wait-timeout=60s < /dev/null > "$lem_backup.partial";then
        fail "Backup falhou; API não parada e código não substituído. Consulte o erro PostgreSQL acima. Backup incompleto: $lem_backup.partial"
    fi
    mv -- "$lem_backup.partial" "$lem_backup";printf 'Backup: %s\n' "$lem_backup"
fi
lem_previous="$lem_target/.deploy-backups/$lem_release"
mkdir -p "$lem_previous/deploy"
for file in compose.yml Dockerfile Caddyfile .env;do cp -p -- "$lem_target/deploy/$file" "$lem_previous/deploy/$file";done
"${lem_old_dc[@]}" stop api
if [[ $lem_reset == 1 ]];then
    if [[ $lem_old_project != lem ]];then "${lem_old_dc[@]}" rm --stop --force api db proxy
    else "${lem_old_dc[@]}" rm --stop --force api db;fi
    sudo docker volume rm "$lem_old_db_volume"
fi
mv -- "$lem_target/server" "$lem_previous/server"
mv -- "$lem_target/shared" "$lem_previous/shared"
cp -a -- "$lem_source/server" "$lem_target/server"
cp -a -- "$lem_source/shared" "$lem_target/shared"
for file in compose.yml Dockerfile Caddyfile backup.sh install-ubuntu.sh remote-deploy.sh deploy.ps1 deploy.sh deploy-package.py deploy-bootstrap.py find-installation.py prepare-config.py remove-prototype.py uninstall-server.sh uninstall-server.py UNINSTALL.md;do
    [[ ! -f $lem_source/deploy/$file ]] || cp -p -- "$lem_source/deploy/$file" "$lem_target/deploy/$file"
done
[[ ! -f $lem_source/.dockerignore ]] || cp -p -- "$lem_source/.dockerignore" "$lem_target/.dockerignore"
mv -- "$lem_stage/next.env" "$lem_target/deploy/.env";chmod 600 "$lem_target/deploy/.env"
lem_dc=(sudo docker compose --project-name lem --env-file "$lem_target/deploy/.env" -f "$lem_target/deploy/compose.yml")
"${lem_dc[@]}" up -d --no-build db
"${lem_dc[@]}" up -d --no-build --force-recreate api proxy
lem_ready=0
for ((i=0;i<30;i++));do
    if curl --fail --silent --connect-timeout 3 --max-time 5 "https://$lem_domain/api/health" >/dev/null;then lem_ready=1;break;fi
    sleep 3
done
if [[ $lem_ready != 1 ]];then "${lem_dc[@]}" logs --tail=60 api proxy >&2;fail "Verificação HTTPS falhou. Código/configuração anteriores: $lem_previous; sem rollback automático.";fi
printf '\nDeploy LEM concluído: https://%s\nCódigo/configuração anteriores: %s\n' "$lem_domain" "$lem_previous"
if [[ $lem_reset == 1 ]];then printf 'Base de dados LEM recriada. Entre com o administrador definido no .env. Não reenvie rascunhos antigos para IDs eliminados.\n';fi

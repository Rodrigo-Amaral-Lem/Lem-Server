> **0.1.28:** O update normal conserva a pasta/configuração/volumes existentes. Para eliminar nomes de instalações anteriores, seguir [instalação limpa do protótipo](../../Common/docs/compatibility-0.1.28.md) antes de voltar a usar este fluxo.

# Deploy a partir do laptop Linux

Executar no computador pessoal, dentro do projeto completo extraído. O script envia o servidor por SSH; não compila a app Qt nem faz a instalação inicial do servidor.

## Primeira migração dos nomes do servidor

Se a instalação é anterior à 0.1.9, usar `bash deploy/deploy.sh --reset-database`. O servidor passa a usar o projeto, utilizador e base de dados `lem`. A configuração é normalizada para `LEM_DOMAIN`; as passwords e os volumes de certificados são conservados. O script recusa a migração sem reset e não apaga volumes de outras instalações. Consultar `interface-0.1.9.md`.

## Atualizar conservando os dados

```bash
cd /caminho/para/LEM
bash deploy/deploy.sh
```

Usa por defeito `lem@lem.rodrod.pt`, porta 22, a configuração/chave normal do OpenSSH e o SSH agent. Descobre automaticamente a única instalação existente em `~/apps` no servidor. Se a chave não estiver na configuração SSH/agent:

```bash
bash deploy/deploy.sh --key ~/.ssh/lem_server
```

Configuração explícita, quando necessária:

```bash
bash deploy/deploy.sh \
  --host lem.rodrod.pt \
  --user lem \
  --port 22 \
  --key ~/.ssh/lem_server \
  --remote-path apps/LEM \
  --domain lem.rodrod.pt
```

`--remote-path` aceita um caminho relativo à home do utilizador remoto ou absoluto. Pode conter espaços, usando aspas. `--domain` confirma o domínio de `deploy/.env`; não altera o domínio. O formato das chaves do `.env` pode ser atualizado pela migração. Se houver mais de uma instalação em `~/apps`, escolher explicitamente com `--remote-path`, ou fornecer um domínio que identifique uma única instalação.

O SSH pode pedir confirmação da identidade do servidor e a passphrase da chave. O `sudo` pede a password do utilizador Ubuntu diretamente na sessão SSH. O script não recolhe nem guarda passwords. Num erro, o comando termina com código diferente de zero; consultar a mensagem antes de repetir.

## Requisitos

No laptop: Bash, Python 3 (apenas biblioteca padrão) e OpenSSH Client (`ssh`/`scp`). Não requer PowerShell, Docker local nem pacotes Python adicionais. Em Ubuntu/Debian, se faltarem dependências:

```bash
sudo apt install openssh-client python3
```

No servidor: instalação existente com Docker Compose, Python 3, curl, flock, sudo e `deploy/.env`. O mesmo processo remoto de `deploy.ps1` é reutilizado.

Manter o projeto completo: `deploy.sh` usa `deploy-package.py`, `deploy-bootstrap.py`, `find-installation.py` e `remote-deploy.sh` incluídos no pacote. Não basta copiar só `deploy.sh` para uma pasta vazia.

## Processo

1. Prepara apenas `server`, `shared` e os ficheiros de deployment. Exclui `.env`, bases de dados, backups, chaves, ambientes virtuais e ficheiros temporários. Rejeita links simbólicos e normaliza os scripts `.sh` para LF.
2. Envia o ZIP e o bootstrap por SSH/SCP, respeitando host, porta e chave configurados.
3. Verifica SHA-256 e valida os caminhos do ZIP antes de extrair. Resolve a instalação remota e adquire um lock contra deploys simultâneos.
4. Constrói a nova API antes de parar o serviço existente. No modo normal, o backup PostgreSQL tem de terminar com sucesso antes de substituir o código.
5. Conserva `.env`, certificados e volumes. Guarda o código anterior em `.deploy-backups` e o dump em `deploy/backups`.
6. Recria os serviços e verifica `/api/health` por HTTPS com validação do certificado.
7. Remove a transferência temporária remota após sucesso e limpa a pasta temporária local. Uma falha durante a atualização conserva a transferência remota para diagnóstico. Não há rollback automático; o código anterior e o dump permitem recuperação manual.

O servidor é atualizado; os dados da aplicação são conservados por defeito. O script não instala nem atualiza automaticamente Docker ou os restantes requisitos do servidor.

## Inspecionar sem ligar ao servidor

```bash
bash deploy/deploy.sh --package-only /tmp/lem-deploy.zip
python3 -m zipfile -l /tmp/lem-deploy.zip
bash deploy/deploy.sh --help
```

## Reset explícito da base de testes

Apenas quando a intenção for apagar os dados de teste:

```bash
bash deploy/deploy.sh --reset-database
```

Esta opção apaga utilizadores, sessões, contratos, carregadores, reports, fotografias, importações e auditoria. Conserva `.env` e os certificados. Não há confirmação adicional: a opção é o pedido explícito de reset, tal como no PowerShell. Para uma atualização normal, executar sem esta opção.

## Validação da entrega

21 testes do novo fluxo Linux com SSH/SCP/sudo/Docker/curl simulados: pacote sem dados privados, nomes/caminhos com espaços, normalização LF, uso de chave e porta, backup, conservação do `.env`, falhas de build/backup antes da paragem, reset explícito só do volume PostgreSQL, domínio divergente, argumentos inválidos, links simbólicos, caminhos ZIP inseguros e SHA-256 incorreto. Mais 5 testes de resolução da instalação. Não houve ligação nem alteração ao servidor real.

> **0.1.28:** Para recriar o protótipo com os nomes atuais, seguir primeiro [compatibilidade e instalação limpa](../../Common/docs/compatibility-0.1.28.md). Os passos históricos abaixo não removem instalações antigas.

> Instalações anteriores à 0.1.9: seguir primeiro a migração descrita em `interface-0.1.9.md`. O instalador inicial não atualiza uma instalação existente.

# Instalação em Ubuntu

O servidor disponibiliza uma API HTTPS. A app nunca se liga diretamente à porta do PostgreSQL. Código preparado para `lem.rodrod.pt`; não foi instalado nesse servidor nesta entrega.

## Instalação guiada em servidor novo

Preparado para o servidor indicado: Ubuntu 24.04, utilizador `lem` com sudo, domínio `lem.rodrod.pt`, HTTPS e contas criadas apenas por admins. Não é necessário adicionar o utilizador ao grupo `docker`.

No computador local, depois de descarregar o ZIP atualizado:

```bash
scp -i ~/.ssh/lem_server LEM_Qt_0.1.9.zip lem@lem.rodrod.pt:~/
ssh -i ~/.ssh/lem_server lem@lem.rodrod.pt
```

Na sessão do servidor:

```bash
sudo apt-get update
sudo apt-get install -y unzip
mkdir -p ~/apps
unzip ~/LEM_Qt_0.1.9.zip -d ~/apps
cd ~/apps/LEM
bash deploy/install-ubuntu.sh
```

Adaptar o caminho/nome da chave SSH se tiver sido criado outro. Não executar o script com `sudo bash`: entrar como `lem`; o próprio script usa sudo onde é necessário. Não sobrepor esta extração a um projeto já configurado sem backup/revisão.

O script verifica Ubuntu, DNS e disponibilidade das portas, instala Docker CE/Compose pelo repositório oficial, cria `.env` com permissões 600, gera passwords aleatórias e pergunta o email do primeiro admin. Não instala Qt no servidor. Abre 80/443 e a porta SSH detetada em `SSH_CONNECTION`, ativa UFW e inicia os containers. Se for executado pela consola do fornecedor com SSH numa porta diferente de 22, indicar `LEM_SSH_PORT=<porta> bash deploy/install-ubuntu.sh`.

A password inicial é mostrada apenas na tua consola quando HTTPS estiver disponível. Guardá-la num gestor de passwords e não a enviar na conversa. Se a verificação HTTPS falhar, os containers e `.env` são conservados; diagnosticar DNS/portas e retomar com `sudo docker compose up -d --build` dentro de `deploy`. O script não substitui `.env` nem repõe passwords de uma instalação anterior.

Se o fornecedor tiver uma firewall externa, permitir também TCP 80/443 e a porta SSH. Docker pode contornar regras UFW para portas publicadas: o compose publica apenas 80/443; PostgreSQL e API não têm portas públicas.

A instalação guiada foi verificada quanto à sintaxe e estrutura, mas ainda não foi executada num Ubuntu remoto. O restante procedimento abaixo permite a instalação manual e explica manutenção/backup.

## Antes da instalação

Confirmar a versão de Ubuntu, acesso SSH e se já existe algum serviço nas portas 80/443. Se existir Nginx/Apache/Caddy, integrar a API no proxy atual em vez de executar o serviço `proxy` deste compose na mesma porta. O DNS A/AAAA deve apontar para este servidor; remover um AAAA antigo se não houver IPv6 funcional. Permitir 80/443 na firewall e manter 5432/8000 sem exposição pública.

Instalar Docker Engine e o plugin Compose segundo a documentação oficial para a versão de Ubuntu instalada: https://docs.docker.com/engine/install/ubuntu/ . Não executar scripts de instalação desconhecidos.

Copiar a pasta `LEM` para `/opt/LEM` (ou outro diretório acessível ao utilizador de deployment). Não substituir serviços existentes sem inventariar primeiro.

## Arranque

```bash
cd /opt/LEM/deploy
cp .env.example .env
chmod 600 .env
openssl rand -hex 24
nano .env
```

Colocar o resultado hexadecimal em `POSTGRES_PASSWORD`. Definir `ADMIN_EMAIL`, `ADMIN_PASSWORD` (mínimo 12 caracteres) e domínio. Nunca versionar `.env` nem enviar passwords na conversa.

```bash
sudo docker compose up -d --build
sudo docker compose logs --tail=80 api proxy
curl https://lem.rodrod.pt/api/health
```

O Caddy pede e renova o certificado HTTPS. O primeiro arranque cria o administrador. Se a base já contém utilizadores, alterações em `ADMIN_PASSWORD` não mudam passwords existentes. Para alterar uma password:

```bash
sudo docker compose exec api python manage.py reset-password --email admin@lem.pt
```

A ferramenta pede a password interativamente e revoga as sessões desse utilizador. Há um único worker nesta versão para que o limite de tentativas de login seja coerente. O compose espera que PostgreSQL esteja saudável antes de iniciar a API.

## Primeiro contrato

Na app: Conta → servidor HTTPS → entrar como administrador. Gestão → criar contrato (empresa/ano/semestre/tipo). Selecionar o contrato na lista superior. Abrir Excel → folha/linha → mapear colunas → pré-visualizar → confirmar. Criar técnico, selecioná-lo na lista de utilizadores e atribuí-lo ao contrato selecionado.

Alternativamente, `https://lem.rodrod.pt/docs` disponibiliza a documentação interativa. Indicar `X-Lem-Client-Version: 0.1.28` nos pedidos à API (incluindo no parâmetro de header apresentado pelo Swagger). Usar `POST /api/login`, copiar `token` e colocá-lo em **Authorize**. `GET /api/users` permite obter IDs dos utilizadores existentes. O acesso e os endpoints de gestão continuam sujeitos a autenticação e perfil admin; a documentação não concede acesso.

## Backup e restauro

```bash
./backup.sh
```

O dump inclui as fotografias originais porque são guardadas em `photos.data` (`bytea` PostgreSQL). Guardar cópias cifradas fora do servidor, com retenção definida. Também conservar `.env` em local seguro. Agendar o script no servidor conforme a política de backup escolhida.

Restauro de ensaio: criar uma base temporária e usar `pg_restore`, verificar contratos, reports, PDF e fotografias. Um restauro na base ativa substitui dados; realizá-lo apenas depois de um backup e numa janela de manutenção. Os volumes Docker não são um backup. Nunca usar `docker compose down -v` para uma atualização normal.

## Capacidade inicial

Para experimentar: 2 vCPU, 4 GB RAM e SSD, ajustando o disco ao volume de imagens. Limites do protótipo: 20 MB por ficheiro, 40 MP por imagem, 200 imagens/250 MB por report. Fotografias não são comprimidas na base; só a versão usada no PDF é reduzida. ZIP/PDF são gerados em memória. Monitorizar consumo e aumentar capacidade antes de aumentar carga/limites.

## Evolução

Esta versão cria as tabelas no primeiro arranque; não implementa migrações automáticas de versões do esquema. Antes de modificar tabelas numa base real, preparar uma migração Alembic, backup e ensaio de restauro. Acrescentar políticas de retenção, métricas, revisão/aprovação, recuperação de password, gestão de dispositivos e validação no terreno antes de uso operacional.

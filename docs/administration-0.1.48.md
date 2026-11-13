# Administração LEM — 0.1.48

## Na app

Na Gestão, o seletor **Área de gestão** separa **Contratos**, **Importar
carregadores** e **Contas e acessos**. Em Contas, um administrador pode escolher
**Técnico** ou **Administrador** antes de criar uma conta. O perfil de administrador
mostra um aviso sobre acesso a todas as contas e contratos. As passwords iniciais
têm pelo menos 12 caracteres; o campo é limpo após enviar.

A página Conta e ligação apresenta a identidade/perfil e versão, a ligação ao
servidor, o formulário de entrada (quando necessário) e a gestão dos dados locais.
Diagnóstico e demonstração ficam escondidos por defeito. Dez pressões seguidas em
**Usar servidor Lem** ativam/desativam essas ferramentas nesta execução da app.
Um intervalo de cinco segundos entre pressões ou fechar a página cancela a
sequência incompleta. Não é uma permissão de administração nem um acesso secreto
à API. Não se guarda este modo entre arranques.

## Ferramenta no próprio servidor

Depois do deploy, na pasta instalada do projeto:

```bash
cd /home/lem/apps/Lem
bash deploy/manage-server.sh
```

Se o utilizador não tiver acesso ao Docker, executar com `sudo bash`.
Sem argumentos, abre uma consola com ajuda e comandos; `help` repete os exemplos
e `exit` termina. Também se pode passar um comando diretamente ao script.
O script usa o Docker Compose e `.env` da sua própria pasta, independentemente da
pasta atual. Não abre portas adicionais e não imprime o conteúdo de `.env`.
O operador deve ter acesso autorizado a SSH/Docker; esta ferramenta tem poderes
de manutenção sobre a base de dados, independentemente de uma sessão na app.

## Contas

```bash
bash deploy/manage-server.sh users list
bash deploy/manage-server.sh users show tecnico@exemplo.pt
bash deploy/manage-server.sh users create --email admin@exemplo.pt --name "Novo administrador" --role admin --if-missing
bash deploy/manage-server.sh users create --email tecnico@exemplo.pt --name "Nome do técnico" --role tech
bash deploy/manage-server.sh users update tecnico@exemplo.pt --name "Nome corrigido" --email novo@exemplo.pt
bash deploy/manage-server.sh users update novo@exemplo.pt --role admin
bash deploy/manage-server.sh users assign novo@exemplo.pt CONTRATO_UUID
bash deploy/manage-server.sh users unassign novo@exemplo.pt CONTRATO_UUID
bash deploy/manage-server.sh users password novo@exemplo.pt
bash deploy/manage-server.sh users delete novo@exemplo.pt
```

ID ou email identifica uma conta. `--if-missing` reutiliza a conta se o email e
perfil já coincidirem; não altera uma conta existente com outro perfil. Funciona
mesmo numa instalação sem administrador, pois não depende do login HTTP.
As listagens incluem ID, nome, email, perfil, contratos e quantidade de reports;
nunca incluem passwords ou hashes.

As passwords existentes **não são recuperáveis**: a base de dados guarda apenas
hashes scrypt. Para definir uma nova password, o comando `users password` pede a
nova password duas vezes, sem a mostrar. Nome/email/perfil/password alterados
revogam as sessões existentes. Não é permitido apagar ou despromover o último
administrador ativo.

Se for necessário gerar e visualizar especificamente uma nova password de uma
única conta:

```bash
bash deploy/manage-server.sh users password novo@exemplo.pt --generate --show-password
```

Sem `--show-password`, a password gerada não é impressa. Para inspecionar
explicitamente o hash de uma única conta: `users password-hash EMAIL_OU_ID`.
O hash não é a password e não serve para iniciar sessão. Passwords nunca são
argumentos da linha de comandos, evitando ficarem no histórico e na lista de
processos.

Apagar uma conta exige escrever **APAGAR**. São removidas as suas sessões e
atribuições a contratos, apagados nome/email/credencial utilizável e desativado o
login. Conserva-se um UUID anonimizado para integridade dos históricos/auditoria.
Os reports e fotos são preservados e apresentados como **Sem proprietário** até
serem reatribuídos. `users list --include-deleted` inclui estas identidades
anonimizadas. A consola usa um principal `system`, sem login, para auditar as
suas operações sem as atribuir a um administrador escolhido arbitrariamente.

## Contratos e recuperação

```bash
bash deploy/manage-server.sh contracts list
bash deploy/manage-server.sh contracts list --deleted
bash deploy/manage-server.sh contracts trash CONTRATO_UUID
bash deploy/manage-server.sh contracts restore CONTRATO_UUID
bash deploy/manage-server.sh contracts purge CONTRATO_UUID
```

`trash` mantém a recuperação de 30 dias. `purge` força a eliminação definitiva de
um contrato que já esteja na área de recuperação, mesmo antes dos 30 dias, com
todos os seus carregadores/reports/fotografias. Ambos exigem **APAGAR**. O comando
de eliminação definitiva só atua no UUID indicado, não noutros contratos que
também estejam expirados. `restore` respeita o prazo original. A limpeza automática
normal continua a correr a cada minuto. Backups e cópias offline têm retenção
separada, como na versão anterior.

## Reports, incluindo fechados e sem proprietário

```bash
bash deploy/manage-server.sh reports list --contract CONTRATO_UUID
bash deploy/manage-server.sh reports list --unowned
bash deploy/manage-server.sh reports show REPORT_UUID
bash deploy/manage-server.sh reports owner REPORT_UUID tecnico@exemplo.pt --assign-access
bash deploy/manage-server.sh reports delete REPORT_UUID
```

`owner` funciona em rascunhos e fechados. `--assign-access` também atribui acesso
ao contrato ao técnico, quando necessário; sem essa opção, a operação exige que
o técnico já tenha acesso. Administradores têm acesso a todos os contratos.
A propriedade atualiza a revisão e o histórico; respostas, fotos e o snapshot
do carregador são conservados. Um dispositivo offline com revisão antiga deve
atualizar/sincronizar e pode precisar de resolver um conflito.

`delete` é definitivo, inclui as fotos e exige **APAGAR**. Deixa um marcador de
report apagado para impedir que um dispositivo antigo recrie o mesmo UUID.
Os detalhes de `reports show` incluem estado, revisão, carregador e proprietário;
não carregam fotografias binárias.

## Carregadores de contratos já importados

```bash
bash deploy/manage-server.sh chargers list CONTRATO_UUID
bash deploy/manage-server.sh chargers add CONTRATO_UUID --external-id NOVO-ID --set 'model=SICHARGE' --set 'serial=00123' --set 'connectors=CCS2, CCS2'
bash deploy/manage-server.sh chargers update CONTRATO_UUID CARREGADOR_UUID --set 'address=Rua Nova 10' --set 'postal_code=1000-001' --set 'connectors=Mennekes Type 2'
bash deploy/manage-server.sh chargers update CONTRATO_UUID CARREGADOR_UUID --details-json - < detalhes.json
```

`--set campo=valor` é repetível. Valores JSON válidos podem ser números/listas;
os restantes são texto. IDs com zeros iniciais como `00123` mantêm-se texto.
O objeto JSON de `--details-json` é mesclado com os detalhes existentes: chaves
não indicadas são conservadas. Modelo e ID externo são obrigatórios na criação;
IDs externos são únicos dentro do contrato. A configuração de conectores é
validada. Campos usuais: `model`, `serial`, `address`, `postal_code`, `cpe`,
`connectors`, `latitude`, `longitude` (ver também os campos de importação).

Com o wrapper Docker, use `--details-json - < detalhes.json` para ler um ficheiro
do servidor anfitrião; caminhos normais são relativos ao sistema de ficheiros
do contentor. Na consola interativa, prefira `--set`; a leitura de JSON por stdin
é indicada para comandos diretos. Reports existentes conservam os seus snapshots;
novas intervenções usam os dados corrigidos. Atualize o catálogo na app para os
ver. Esta ferramenta não substitui as importações Excel existentes.

## Deploy e validação

Atualizar servidor e recompilar a app para 0.1.48 (Android code 160). A política
incluída exige 0.1.48. Não são necessárias alterações destrutivas da base de dados.
As confirmações da consola acontecem antes dos bloqueios de escrita, para não
prender técnicos enquanto o operador decide o que escrever.

Os testes usam bases descartáveis SQLite, QML com backend simulado, um wrapper
Docker simulado e testes nativos Qt. Cobrem bootstrap/perfis, privacidade de
passwords, último administrador, confirmações, contas apagadas e reports órfãos,
reatribuição de fechados, exclusão de reports/fotos, purge de um só contrato,
edição de carregadores e snapshots. O teste de UI verifica criação de admins e
os dez toques para ativar/desativar diagnóstico. Não foi executado Docker real,
PostgreSQL real, deploy remoto nem um build físico Android/Windows neste ambiente.

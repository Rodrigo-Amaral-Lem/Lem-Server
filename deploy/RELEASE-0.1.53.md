# Lem 0.1.53

O instalador pergunta o nome da primeira conta, além do email, e guarda `ADMIN_NAME` em `deploy/.env`. Docker Compose passa esse nome à API. O nome só é usado quando a base de dados está vazia; não altera contas existentes. Instalações anteriores sem esta variável mantêm o nome predefinido Administrador.

Na app, a importação Stations passa a usar sempre o cabeçalho da primeira linha. Foram removidos os controlos de seleção da linha e mapeamento de colunas. A escolha da folha continua disponível; o formato reconhecido é selecionado inicialmente. Os nomes/posições e campos obrigatórios continuam a ser validados pelo servidor.

A pré-visualização apresenta todos os registos válidos, numa lista com scroll, e todos os campos de origem por carregador. Erros são apresentados separadamente e bloqueiam a confirmação. O servidor mantém a amostra de dez linhas para apps anteriores, mas fornece também a lista completa à nova interface. Se o servidor antigo só devolver a amostra, a nova app pede a atualização e não confirma uma importação com pré-visualização incompleta.

Atualize o servidor e recompile a app. Não é necessário apagar a base de dados. A versão mínima compatível continua a ser 0.1.52; a última versão passa a 0.1.53. As melhorias anteriores de instalação e desinstalação permanecem incluídas.

## Diagnóstico do backup no deploy remoto

O build termina antes do backup da base de dados. O deploy passa a anunciar o
backup e o destino antes de executar `pg_dump`, e mostra o progresso detalhado
em stderr, sem misturar mensagens com os bytes do ficheiro `.dump`.
A autorização sudo é renovada explicitamente depois do build; a execução do
backup usa sudo não interativo. A ligação tem um limite de 15 segundos e a
espera por locks de tabelas um limite de 60 segundos. Estes limites não são
um limite total de duração do backup.

Se o backup falhar, o deploy termina antes de parar a API ou substituir o código.
O `.partial` fica disponível para diagnóstico e nunca é promovido a backup
concluído. Não se deve usar reset da base de dados para contornar esta situação.

Para observar um deploy já em curso, abrir outra sessão SSH e executar na pasta
`deploy` da instalação:

```sh
sudo docker compose exec -T db psql -U lem -d lem -c "SELECT pid, application_name, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers, now()-query_start AS elapsed FROM pg_stat_activity WHERE application_name = 'pg_dump';"
ls -lh backups/*.partial
```

Uma lista de bloqueadores não vazia identifica sessões que impedem o backup.
Não terminar essas sessões sem perceber a transação em curso. O tamanho do
`.partial` pode ficar constante durante fases de preparação; isoladamente isso
não prova um bloqueio. A ausência de `pg_dump` exige investigar o comando
Docker/sudo ou se o build já devolveu controlo ao script.

### R3 — backup sem entrada interativa

O diagnóstico no servidor mostrou o processo Compose suspenso (`T`,
`do_signal_stop`) e `pg_dump` bloqueado em `anon_pipe_write`, com o ficheiro
`.partial` a zero bytes e espaço livre disponível. O PostgreSQL mostrava
`ClientRead`, sem sessões bloqueadoras. Isto localiza a paragem no processo
cliente/encaminhamento de saída; a causa exata do sinal de suspensão não foi
capturada. A entrada interativa do Compose em contexto SSH/sudo é uma causa
plausível e evitável.

`remote-deploy.sh` e `backup.sh` passam a usar
`docker compose exec -T --interactive=false` e stdin de `/dev/null` no backup.
`-T` por si só não desativa a entrada interativa. O teste de regressão injeta
texto na entrada do deploy e verifica que o comando de backup não o recebe,
que a execução Compose é não interativa e que o dump é concluído.
A correção ainda precisa de confirmação no servidor real.

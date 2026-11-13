> Instalações anteriores à 0.1.9: seguir primeiro a migração descrita em `interface-0.1.9.md`. O instalador inicial não atualiza uma instalação existente.

# Atualizar para Stations / título LEM

Esta atualização precisa de **app e API**. O servidor continua a usar as mesmas tabelas e volumes; não há migração nem importação automática do ficheiro fornecido. Não executar o instalador inicial de novo nem substituir `deploy/.env`.

## Servidor

No computador onde descarregaste o ZIP:

```bash
scp -i ~/.ssh/lem_server LEM_Qt_0.1.9.zip lem@lem.rodrod.pt:~/
ssh -i ~/.ssh/lem_server lem@lem.rodrod.pt
```

Na sessão SSH, fazer backup e substituir apenas o código do servidor:

```bash
cd ~/apps/LEM/deploy
./backup.sh
cd ~/apps
unzip -o ~/LEM_Qt_0.1.9.zip 'LEM/server/*' -d .
cd ~/apps/LEM/deploy
sudo docker compose up -d --build api
sudo docker compose logs --tail=60 api
curl --fail https://lem.rodrod.pt/api/health
```

O backup tem de terminar com sucesso antes de continuar para a extração/build. Se usaste outra pasta, adaptar os caminhos. Estas operações não substituem `.env`, não removem volumes e não alteram utilizadores ou reports existentes.

## App

Fechar a app. Extrair o ZIP atualizado e copiar a pasta `app` sobre a pasta `app` do projeto existente. Na raiz `LEM`, reconfigurar com `--fresh` para garantir que todas as fontes novas são compiladas, mesmo que os ficheiros extraídos tenham datas normalizadas:

```bash
cmake --fresh -S app -B app/build/linux -G Ninja \
  -DCMAKE_PREFIX_PATH="$HOME/Qt/6.12.0/gcc_64" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build app/build/linux --parallel
./app/build/linux/LEM
```

Adaptar o caminho do Qt. No Qt Creator, executar Clean/Rebuild com o kit habitual. O título passa a LEM; as definições, rascunhos e cache anteriores mantêm-se porque a pasta interna da aplicação permanece a mesma.

## Importar

Entrar como admin, selecionar o contrato e ir a Gestão → Abrir Excel. Escolher o XLSM. A folha Stations e os atributos são reconhecidos automaticamente; os dez obrigatórios têm `*`. Pré-visualizar e confirmar. C/D/E/G podem ficar vazias. Os macros não são executados.

Quando houver um erro, a pré-visualização apresenta a célula/linha e bloqueia a confirmação. O ficheiro fornecido nesta conversa tinha 51 IDs válidos; esta entrega não o importa automaticamente no servidor.

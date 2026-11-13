> **0.1.28:** O update normal conserva a pasta/configuração/volumes existentes. Para eliminar nomes de instalações anteriores, seguir [instalação limpa do protótipo](../../Common/docs/compatibility-0.1.28.md) antes de voltar a usar este fluxo.

# Deploy por PowerShell / SSH

Usar a partir da pasta extraída `LEM` no computador pessoal. Não necessita de Qt, Docker ou Python no Windows para enviar o servidor. Precisa de OpenSSH Client (`ssh` e `scp`) e de acesso SSH já configurado. O servidor já deve ter a instalação inicial a funcionar, Docker Compose e o ficheiro `deploy/.env`.

## Primeira migração dos nomes do servidor

Na primeira atualização de uma instalação anterior à 0.1.9, executar `.\deploy\deploy.ps1 -ResetDatabase`. A base de dados é recriada com o nome/utilizador `lem`; a configuração passa a `LEM_DOMAIN`. As passwords e certificados são conservados. Depois desta migração, os deploys normais voltam a conservar a base de dados. Consultar `interface-0.1.9.md`.

## Atualizar conservando os dados

```powershell
cd "C:\Projetos\LEM"
.\deploy\deploy.ps1
```

Valores predefinidos: servidor `lem.rodrod.pt`, utilizador `lem`, porta SSH `22`, pasta remota descoberta automaticamente entre as instalações existentes em `~/apps` (se existir mais de uma, indicar `-RemotePath`). A chave segue a configuração normal do OpenSSH/agent. As chaves privadas e passwords não são colocadas no script nem no pacote.

## Recriar os dados do servidor de testes

```powershell
.\deploy\deploy.ps1 -ResetDatabase
```

Esta opção **apaga utilizadores, sessões, contratos, carregadores, reports, fotografias, importações e auditoria** da base de dados desta aplicação. Não pede outra confirmação: a opção `-ResetDatabase` é o pedido explícito de reset. Não a usar num servidor com dados a conservar.

Conserva `.env`, backups anteriores, chaves SSH e volumes de certificados do Caddy. O administrador é recriado usando `ADMIN_EMAIL` e a **password inicial `ADMIN_PASSWORD` do `.env`**, que pode diferir de uma password alterada posteriormente. Terminar a sessão na app e entrar novamente depois do reset. Os rascunhos locais antigos não são apagados pelo deploy; não devem ser reenviados para IDs eliminados do servidor.

Para alterar a password do admin depois do deploy, no servidor:

```bash
cd ~/apps/LEM/deploy
sudo docker compose exec api python manage.py reset-password --email admin@lem.pt
```

Adaptar o email ao `.env`. A password é pedida interativamente no servidor, não enviada na conversa.

## Outras configurações

```powershell
.\deploy\deploy.ps1 `
  -ServerHost lem.rodrod.pt `
  -UserName lem `
  -Port 22 `
  -KeyPath "$env:USERPROFILE\.ssh\lem_server" `
  -RemotePath "apps/LEM" `
  -Domain "lem.rodrod.pt" `
  -ResetDatabase
```

`RemotePath` pode ser relativo à home ou absoluto (`/opt/LEM`). A pasta deve pertencer/ser gravável pelo utilizador SSH e conter a instalação existente. `Domain` é opcional: se fornecido, deve coincidir com `LEM_DOMAIN` no servidor; o script não muda esse ficheiro. `ServerHost` também pode ser o alias SSH/IP usado para entrar, enquanto o domínio HTTPS é lido do `.env`.

Se o Windows bloquear a execução do script descarregado, executar apenas esta sessão/script com:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy\deploy.ps1 -ResetDatabase
```

Não é necessário alterar permanentemente a política de execução. O comando SSH pode pedir confirmação da identidade do servidor ou a passphrase da chave, conforme a tua configuração. Quando aparecer `[sudo] password for lem:`, é a password do utilizador Ubuntu; é introduzida diretamente na sessão SSH. O script não guarda passwords.

## O que acontece

1. Empacota `server`, `shared` e os ficheiros de deploy, excluindo `.env`, bases locais, backups, ambientes virtuais e chaves.
2. Normaliza os `.sh` para LF; envia o ZIP por SCP e confirma o SHA-256 no destino.
3. Valida o caminho existente e usa um lock para impedir dois deploys em simultâneo.
4. Valida Compose e constrói a imagem **antes** de parar a API ou apagar dados. Uma falha de build conserva os containers em execução.
5. No modo normal, faz um dump PostgreSQL e só prossegue se terminar com sucesso. No modo reset, verifica que o volume `pgdata` pertence a este projeto e apaga apenas esse volume, sem apagar certificados.
6. Conserva o código anterior em `.deploy-backups/<release>`, substitui o código e recria os serviços API/proxy, mantendo a configuração.
7. Verifica `https://<LEM_DOMAIN>/api/health` com validação TLS e elimina o pacote temporário após sucesso.

Não faz rollback automático nem migrações de esquema; os diretórios de código anterior e dumps ficam disponíveis para recuperação manual. Num erro posterior à paragem, consultar a linha indicada e os logs. O script não faz a instalação inicial nem altera SSH/firewall/DNS.

**Este deploy atualiza o servidor. Não compila nem instala a app Qt no teu computador/telemóvel.** Recompilar a app separadamente quando existirem alterações em `app`.

## Inspecionar o pacote sem ligar ao servidor

```powershell
.\deploy\deploy.ps1 -PackageOnly "$PWD\deploy-inspecao.zip"
```

Inspecionar esse ZIP se necessário. O diretório temporário local é eliminado no fim.

## Validação

Validado novamente em 10-11-2026 com PowerShell 7.6.6 em Linux: **14 testes aprovados**. Empacotamento real, caminhos com espaços, exclusão de ficheiros privados, bootstrap/extracção e comandos Bash remotos executados numa instalação temporária com SSH/SCP/Docker/curl simulados. Testes de atualização normal, reset limitado ao volume correto, falha de build, volume de outro projeto e domínio incorreto. Não executado neste ensaio num Windows real, com Windows PowerShell 5.1, Docker real ou no servidor do utilizador. Ver [resultados e verificação local no Windows](powershell-validation-2026-11-10.md).

```bash
PWSH=/caminho/pwsh python -m pytest deploy/tests -q
```

## Correção de ZIP no Windows PowerShell

O empacotamento escreve agora explicitamente separadores `/`, sem depender dos separadores produzidos por `ZipFile.CreateFromDirectory` no .NET Framework do Windows. O servidor normaliza também separadores `\` de pacotes Windows antes de validar/extrair. Caminhos absolutos, travessia com `..`, ficheiros privados, links e colisões de caminhos normalizados continuam bloqueados. O erro de validação acontece antes de executar Docker ou reset.

Os testes incluem um ZIP com separadores Windows simulados e caminhos inválidos/misturados/duplicados. Para corrigir uma cópia anterior do projeto basta substituir `deploy/deploy.ps1` pelo atualizado; não é necessário alterar manualmente o servidor antes de repetir o deploy.

No Windows PowerShell 5.1, o script carrega explicitamente `System.IO.Compression` e `System.IO.Compression.FileSystem` antes de usar ZipArchiveMode/ZipFile.

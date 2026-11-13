# Lem 0.1.55 — Identificação do carregador e cadastro comum

## Perguntas EST

A checklist passa à versão 4. A ordem inicial é:

| Pergunta | Conteúdo | Fotografia |
|---|---|---|
| EST.01 | Fotografias interior e exterior antes da intervenção | Obrigatória, como anteriormente |
| EST.02 | Indique o fabricante do carregador | Opcional |
| EST.03 | Indique o modelo completo do carregador | Opcional |
| EST.04 | Indique o número de série do carregador — antiga EST.02 | Obrigatória, conservando a regra anterior |
| EST.05 | Fotografia da chapa de características | Obrigatória |
| EST.06 | Verifique o ID da estação — antiga EST.03 | Obrigatória, conservando a regra anterior |

As restantes perguntas EST avançam três posições, sem serem eliminadas. O grupo
passa a ter 17 perguntas. Os restantes seis grupos, a cópia QVE e a assinatura
mantêm-se. Mantém-se a dispensa de fotografia quando a resposta é N/A.

Ao criar um report, EST.02, EST.03 e EST.04 são preenchidas, quando disponíveis,
a partir de `manufacturer`, `model` e `serial` da ficha importada. Continuam
editáveis pelo técnico proprietário.

## Separação entre carregador e contrato

A tabela `stations` guarda a ficha física, com um identificador interno próprio,
um ID externo único, os detalhes e os campos confirmados pelo técnico. Um ID
externo exatamente igual é tratado como o mesmo carregador em contratos distintos.

A tabela histórica `chargers` passa a representar a associação entre contrato e
ficha física, através de `station_id`. Mantém o seu UUID para conservar as
referências dos reports, permissões, fotografias e marcadores de eliminação.
Mantém também uma cópia dos detalhes antigos exclusivamente para recuperação da
migração; os dados ativos são lidos da ficha comum. Essa cópia antiga não é
carregada nos catálogos normais.

Importar o mesmo ID noutro contrato reutiliza a ficha física e cria outra
associação, sem duplicar o carregador. Os reports continuam associados ao seu
contrato: ter acesso ao carregador num contrato não dá acesso aos reports de
outro. A eliminação definitiva de um contrato remove a associação e os seus
reports, conservando a ficha comum e as associações dos outros contratos.

## Correções ao concluir

Fabricante, modelo e série só alteram o cadastro quando o report é concluído.
Rascunhos e conclusões recusadas não alteram os dados no servidor. Valores vazios
ou N/A não apagam os campos da ficha.

O servidor compara as respostas com a ficha congelada no início do report e só
aplica os campos que o técnico efetivamente alterou. A conclusão e a correção
são confirmadas na mesma transação. O snapshot desse report passa a apresentar
os dados corrigidos, incluindo no cabeçalho do PDF; os reports anteriores
conservam os seus snapshots.

Se outro report tiver corrigido o mesmo campo entretanto para um valor diferente,
o servidor recusa a conclusão com conflito, conservando o rascunho e a correção
mais recente. Respostas antigas que não foram alteradas não repõem valores
antigos no cadastro. A app atualiza as cópias locais dos catálogos e confirma os
dados comuns quando recebe a resposta do servidor. Offline, as correções ficam
pendentes de sincronização.

Os campos confirmados no fecho ficam protegidos contra reimportações de folhas
antigas. Uma nova correção num report pode alterá-los novamente. As alterações
administrativas explícitas continuam disponíveis pela consola. O ficheiro Excel
original não é modificado.

## Atualização e dados existentes

Atualize primeiro o servidor e depois recompile e instale a app 0.1.55. A versão
mínima e a última versão passam a 0.1.55. O deploy normal continua a criar backup
antes de substituir o código.

A migração é aditiva: cria o cadastro comum e associa os registos existentes,
conservando os UUIDs dos vínculos e os snapshots dos reports. Nos IDs repetidos,
os detalhes do ano/semestre mais recente prevalecem; empates usam uma ordem
estável dos UUIDs. Os detalhes originais de cada vínculo ficam conservados para
consulta técnica. Não é obrigatório apagar a base de dados.

Os reports já existentes no servidor mantêm a checklist congelada. A sincronização
passa a enviar explicitamente a versão da checklist em cada atualização. Um
rascunho antigo que ainda não exista no servidor não pode ser criado com a nova
numeração: é recusado e conservado localmente para consulta. Deve criar uma nova
intervenção para usar os novos campos.

Se preferir reiniciar completamente o protótipo, utilize o reset explícito já
existente no deploy e limpe também os dados locais antigos das apps. Não foi
executado nenhum reset nem deploy remoto nesta revisão.

## Validação

- 320 testes locais de servidor/deploy aprovados; 28 opcionais por executar por
  falta do Excel original e de PowerShell neste ambiente.
- Novos testes: um carregador em dois contratos, correções no fecho, preservação
  de reports fechados, rejeição de fecho incompleto, conflito entre correções,
  proteção contra reimportação antiga, permissões entre contratos, migração
  idempotente, preservação do cadastro ao purgar um contrato e proteção contra
  reutilização de números de perguntas de uma checklist antiga.
- Testes nativos Qt 6.12.0: preenchimento importado, falha de conclusão sem foto,
  atualização dos catálogos locais de vários contratos, reabertura offline,
  confirmação do servidor, persistência, QVE, assinatura e PDF offline.
- Interface: navegação/revisão do report em largura de telemóvel, os sete grupos,
  assinatura, cópia QVE e validação das medições.
- Migração executada em SQLite. DDL das tabelas e índices compilado para
  PostgreSQL; não foi executada uma migração num PostgreSQL real nesta revisão.
  Não foi instalado um novo APK num telefone.

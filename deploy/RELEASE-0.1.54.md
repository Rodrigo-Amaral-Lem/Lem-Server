# Lem 0.1.54 — Organização das perguntas

Os novos reports usam a checklist versão 3, na mesma ordem na app e no PDF.
A assinatura em FIN.02 interpreta a última indicação do pedido como referente
à antiga PG04.02 (Nome do técnico).

| Ordem | Grupo | Alterações |
|---|---|---|
| 1 | SEG — Segurança | Seis perguntas com os textos pedidos; SEG.01 requer fotografia. |
| 2 | EST — Estado Geral | Antigo PC01; nova verificação do ID em EST.03. EST.02, EST.03, EST.10 e EST.13 requerem fotografia, além da fotografia anterior EST.01. |
| 3 | SITE — Site | Antigo PG01, com as mesmas perguntas. |
| 4 | QVE — QVE e Botoneira | Antigo PG02; QVE.01 requer fotografia; nova QVE.03 verifica dupla sinalização. A fotografia anterior do quadro passa a QVE.04. |
| 5 | MAN — Manutenção | Antigo PC02 sem a primeira pergunta. Proteção diferencial QVE e Carregador. MAN.13: teste manual do dispositivo. |
| 6 | CARG — Testes de Carga | Antigo PC03; mantém uma pergunta por conector físico, incluindo conectores repetidos. |
| 7 | FIN — Informações Gerais e Final | FIN.01: fotografia após intervenção. FIN.02: nome e assinatura desenhada do técnico. FIN.03: observações. |

PG03 foi removido. As medições são agora MAN.04 a MAN.10 (tensões) e MAN.18
(resistência de terra). Mantêm-se os limites e a apresentação/validação das
medições. Mantém-se a regra existente de dispensar a fotografia quando a
resposta é N/A.

## Copiar QVE

No grupo QVE de um rascunho editável, usar **Copiar QVE de outro carregador…**.
Escolher um report de outro ID do mesmo contrato e confirmar que os carregadores
partilham o QVE. A cópia substitui apenas as respostas, observações e evidências
QVE do destino. A origem é conservada. A referência à origem e a data ficam
visíveis na app e no PDF.

As fotografias e os vídeos recebem novos identificadores e ficheiros privados.
Não existem ficheiros partilhados cuja remoção afete a origem. Online, a app
pede os metadados e descarrega apenas as evidências QVE necessárias. Offline,
é necessário ter o report de origem e essas evidências disponíveis no dispositivo.
Reports da checklist antiga não podem servir de origem para este grupo novo.

Uma falha no download, na integridade de uma evidência ou na gravação cancela a
cópia sem substituir parcialmente o destino. Mantêm-se os limites por report.
As evidências anteriores do destino ficam marcadas para remoção no servidor.
O técnico deve rever as respostas copiadas antes de concluir.

## Assinatura

FIN.02 mantém o nome do técnico, inicialmente preenchido com o nome da conta.
O botão **Assinar** abre uma área para desenhar com o dedo ou com o rato, com
opções Limpar, Cancelar e Guardar. A assinatura é opcional; o pedido não indicou
que fosse obrigatória para concluir o report.

A assinatura é guardada como PNG, com o nome e a data UTC da recolha. Entra no
PDF, no ZIP e na sincronização normal, e pode ser recolhida offline. Substituir
a assinatura elimina a anterior do destino. Alterar o nome do técnico invalida
e remove a assinatura anterior. Não é permitido recortá-la como uma fotografia.
Se existir uma assinatura declarada, o servidor exige a respetiva imagem PNG no
mesmo report e em FIN.02 antes de aceitar a conclusão. Trata-se de uma imagem de
assinatura manuscrita, sem certificação criptográfica.

## Atualização

1. Atualizar o servidor com o deploy normal do pacote 0.1.54.
2. Recompilar e instalar a app 0.1.54. A versão mínima passa a 0.1.54, evitando
   que clientes com a checklist antiga criem respostas com identificadores incompatíveis.
3. Criar novos reports para usar esta organização.

Não é obrigatório apagar a base de dados. Os reports existentes conservam a
checklist congelada e as respostas anteriores. Para começar completamente de
novo no protótipo, pode usar o reset explícito já existente no deploy, mas deve
também remover os dados locais antigos das apps para não tentar reenviá-los.
As correções R3 do deploy não interativo estão incluídas.

## Validação local

- 312 testes de servidor/deploy aprovados; 28 opcionais ignorados por falta do
  Excel original e de PowerShell neste ambiente. Os testes posteriores do PDF
  e das assinaturas também passaram (36 verificações).
- Código nativo compilado com headers e bibliotecas Qt 6.12.0. Passaram cópia QVE,
  evidências independentes, falha atómica, substituição/remoção de assinatura,
  alteração de nome e reabertura do armazenamento local.
- Passaram os testes nativos de persistência, medições, conectores, PDF offline,
  vídeos e recorte. A exportação com assinatura foi gerada e inspecionada.
- Interface QML testada em largura de telemóvel (390 px) e desktop, incluindo
  os sete grupos, confirmação da cópia e geração do PNG de assinatura. Também
  passaram navegação/revisão, entradas de medição e controlos de evidências.
- Não foi instalado um novo APK num telefone nem executado deploy no servidor
  remoto. O download QVE por rede usa os endpoints existentes; a cópia local e
  os casos de falha foram testados no controlador nativo.

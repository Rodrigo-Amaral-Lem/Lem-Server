# Lem 0.1.56 — Respostas, cópias e seleção múltipla

## MAN

MAN.14, MAN.15, MAN.16, MAN.17 e MAN.18 passam a apresentar botões OK/NOK,
conservando a possibilidade de registar um resultado/valor ou N/A. Selecionar
OK/NOK substitui o valor da resposta; para voltar a registar o valor basta
preencher o campo. NOK continua a exigir observação.

- MAN.18: **Valor da resistência da terra**.
- MAN.19: **Verifique o estado dos filtros**.

MAN.18 aceita uma medição numérica em Ω, OK, NOK ou Não Aplicável na app, na
validação do servidor e na exportação PDF. As restantes medições mantêm os seus
limites e formatos.

## Testes de carga

Todos os CARG.01.xx gerados para os conectores físicos exigem fotografia quando
a resposta é OK ou NOK. N/A dispensa a fotografia, mas exige observação para
explicar, por exemplo, a ausência de conector ou veículo compatível.

CARG.05 fica **Verifique o correto funcionamento dos meters no carregador - tire
fotografias**, com a mesma regra de fotografia e de observação em N/A. Um vídeo
não satisfaz uma fotografia obrigatória. O preenchimento é validado na revisão
visual, na conclusão local e independentemente no servidor.

## Copiar QVE e medições

A cópia QVE dá sempre prioridade ao report guardado no dispositivo, mesmo que
este esteja totalmente sincronizado e exista ligação ao servidor. Utiliza as
respostas e evidências dessa cópia. Só descarrega os ficheiros que faltam no
dispositivo; não substitui as respostas locais por uma nova leitura do servidor.
Se o report de origem não estiver guardado, pede os metadados ao servidor.

No grupo MAN passa a existir **Copiar Medições de outro carregador…**. A operação
copia exclusivamente MAN.04 a MAN.10: valores, observações e fotografias/vídeos
associados. MAN.01–03 e MAN.11–20 conservam os seus dados. A origem é outro ID do
mesmo contrato, escolhido e confirmado pelo técnico. A origem/data da cópia
ficam visíveis no PDF.

Ambas as cópias:

- Mantêm editáveis os restantes campos do report enquanto carregam evidências.
- Bloqueiam apenas os itens de destino que vão ser substituídos; na cópia MAN,
  os outros itens do próprio grupo MAN continuam editáveis.
- Preservam as alterações feitas nos outros campos durante a operação.
- Criam ficheiros e identificadores novos, sem alterar a origem.
- Só substituem o destino depois de todos os ficheiros terem sido guardados.
- Conservam o destino se houver falha, falta de evidências offline, ficheiro
  danificado ou excesso dos limites do report.

Concluir o report, trocar de report/contrato ou de sessão fica suspenso até
terminar a cópia. A sincronização e a limpeza automática de cache também não
interferem com a operação.

## Várias fotografias ou vídeos

Os seletores de fotografias e de vídeos permitem selecionar vários ficheiros
numa só operação (`OpenFiles`). Os ficheiros são adicionados sequencialmente,
permitindo processar os eventos da interface entre ficheiros. Mantêm-se as
validações existentes: imagens suportadas, vídeo MP4/3GP até 60 segundos e
20 MB por evidência, 200 evidências e 250 MB por report.

Um ficheiro inválido é recusado sem impedir os outros da seleção. A mensagem
final informa quantos foram adicionados e quantos foram recusados. Os originais
não são apagados. A conclusão do report espera pelo fim da seleção.

## Atualização

Atualizar primeiro o servidor, depois recompilar/instalar a app 0.1.56. A versão
mínima e a última versão passam a 0.1.56; a checklist passa à versão 5. Não há
nova alteração ao esquema da base de dados relativamente à 0.1.55, nem é
necessário reset. As alterações de cadastro comum da 0.1.55 estão incluídas.

As regras novas aplicam-se aos reports novos. Reports existentes no servidor
conservam a sua checklist congelada. Rascunhos de uma checklist anterior que
ainda não existam no servidor são conservados localmente e recusados para não
mudar o significado das respostas; criar uma nova intervenção para usar as
regras novas.

## Validação

- 338 testes locais de servidor/deploy aprovados; 28 opcionais por executar por
  falta de PowerShell e do Excel original neste ambiente.
- Novos testes de fotografias e observações N/A por conector e em CARG.05,
  rejeição de vídeo como fotografia e respostas manuais MAN.14–18.
- Controlador nativo Qt 6.12: prioridade da origem local já sincronizada sem
  acesso HTTP, cópia QVE de fotografia e vídeo, cópia do subconjunto MAN,
  preservação das edições simultâneas, proteção da origem e seleção múltipla
  com fotografia, vídeo e um ficheiro inválido.
- Passaram também os testes nativos de persistência, assinatura, medições e PDF
  offline, e os testes da interface de navegação, avaliação manual, observação
  N/A, bloqueio por grupo e seletores em modo múltiplo.
- Não foi instalado um novo APK num telefone nem feito deploy remoto. A seleção
  múltipla foi configurada e testada na interface Qt; falta confirmar o seletor
  nativo do Android no dispositivo real. O percurso HTTP da cópia usa os mesmos
  endpoints existentes; os testes novos do controlador exercitam a cópia local.

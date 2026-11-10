# Contrato de consistência

Uma versão agregada por contrato começa em1 e avança exatamente uma vez por novo
comando comprometido. Cada versão tem um evento. A revisão do contrato avança
somente em aditivo de teto; a revisão da medição avança somente em revisão de
linhas. Mesmo alteração numericamente igual é um novo comando se a chave é nova.

DRAFT não reserva. SUBMITTED reserva todas as linhas atomicamente. Receber libera
toda a reserva e soma somente a parte aceita; sobra não fica pendente. RETURNED
libera tudo e permite nova revisão DRAFT. ACCEPTED/ACCEPTED_PARTIAL são terminais
exceto estorno explícito integral, que produz REVERSED. Não há edição silenciosa,
exclusão de história, reserva automática ou término jurídico de contrato.

Atores/papéis e limites são fixados no bootstrap. Membros herdam apenas os papéis
semeados; gestor externo ao contrato não ganha acesso automático. Um contrato
inclui o criador e um par solicitante/fiscal de pessoas distintas. Auditores
puros não escrevem; papéis combinados usam união. Solicitantes leem conteúdo e
recibos de suas medições; gestor/fiscal/auditor leem todas. Todos os membros leem
cabeçalho e saldos. Histórico completo é reservado a papéis elevados, evitando
confundir redação de dados com lacunas de versão.

Dentro de `BEGIN IMMEDIATE`: autoridade/ownership, idempotência, CAS, estado e
novas quotas precedem aplicação do plano. Chave é contrato+operation_id; payload
canônico e SHA256 são comparados conjuntamente, não usados como autenticação.
Recibo de replay é o original, com saldos daquela versão. Um ator sem permissão
não obtém dados de cache. Para criar contrato, exige-se MANAGER antes de consultar
o objeto; ID de medição pertencente a outro solicitante fica oculto antes de
chave/idempotência ou duplicidade.

Erros fechados: INVALID_INPUT, INVALID_LIMIT, ALREADY_EXISTS, NOT_FOUND, FORBIDDEN,
SELF_REVIEW_FORBIDDEN, STALE_VERSION, STATE_CONFLICT, IDEMPOTENCY_CONFLICT,
INSUFFICIENT_BALANCE, AMENDMENT_FLOOR, ALREADY_REVERSED, LIMIT_EXCEEDED, CLOSED,
WRONG_THREAD, BUSY, INCOMPATIBLE_STORE, CORRUPT_STORE, STORAGE_ERROR,
COMMIT_UNCERTAIN. Falha nativa não vira insuficiência de saldo. BUSY ao adquirir
transação é operacional e sem despacho. COMMIT negado com rollback confirmado é
STORAGE_ERROR/NO_EFFECT; resultado/rollback não estabelecido é UNKNOWN e inutiliza
o handle, que deve ser reaberto para reconciliação pela mesma operação.

`close` é idempotente na thread dona. Identidade da thread precede CLOSED, que
precede metadados inválidos. Outra thread abre outro Ledger. `open` não cria nem
migra banco; valida schema conhecido, referências, constraints e saldos/fatos.
Arquivo/SQL adulterado diretamente, filesystem hostil, crashes e perda de energia
não recebem garantia de integridade criptográfica ou resultado devolvido.

Oráculos anteriores ao primeiro core:196 sequências pequenas de deltas assinados,
336 casos de piso de aditivo,39 casos literais de admissão e7 páginas. Casos exatos
de bytes e COMMIT incerto usam seams privados do provedor, identificados nos testes;
não são descritos como entrada pública do SDK. O corpus não é prova de completude
sobre toda entrada possível. Testes também observam SQLite somente leitura para
comprovar ausência de mutação nas recusas, incluindo operações/eventos/fatos.

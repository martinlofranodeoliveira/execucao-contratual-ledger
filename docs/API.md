# API e operação local

`LedgerHTTPService` abre somente `127.0.0.1` em porta efêmera. O host fornece um
`SyntheticAuthority` com IDs de atores conhecidos; tokens novos são gerados em
memória. `demo` prepara e usa clientes no mesmo harness e não imprime credenciais.
Não há endpoint para obter token, escolher ator/papel ou executar SQL.

Cada POST usa `Content-Type: application/json` e `Authorization: Bearer <token>`.
Rotas fixas: `POST /v1/commands/<action>`, onde `action` é um dos oito nomes abaixo.
Todos os corpos têm `contract_id`, `operation_id` e `expected_version`.

| action | Demais campos |
|---|---|
| create_contract | items: objetos item_id/unit/scale/authorized_ticks; member_ids |
| create_measurement | measurement_id; lines: objetos item_id/ticks |
| revise_measurement | measurement_id; lines |
| submit_measurement | measurement_id |
| receive_measurement | measurement_id; accepted_lines; reason opcional no recebimento completo |
| return_measurement | measurement_id; reason |
| amend_contract | new_limits: todos os itens como item_id/ticks; reason |
| reverse_receipt | measurement_id; reason |

Recebimento inclui explicitamente todos os itens submetidos, inclusive zeros,
e ao menos uma quantidade positiva. Devolução integral usa `return_measurement`.
Redução de teto não pode ficar abaixo de recebido+reservado. Estorno é integral
do recibo e preserva recebimento original; não reabre/resubmete medição.

GETs: `/v1/contracts/<id>`, `/measurements/<id>` e `/operations/<id>` sob o caminho
do contrato. `/v1/contracts/<id>/history` aceita `after_version`, `through_version`
e `limit` como parâmetros decimais únicos; guarde `through_version` entre páginas.
Uma resposta de sucesso tem `result` com DTOs imutáveis convertidos para JSON.
Escritas retornam `receipt` e `replayed`; criação/commit novo usa 201, replay/leitura200.

| HTTP | Significado |
|---|---|
| 400 | JSON/schema/tipos inválidos, campos desconhecidos ou identidade em comandos |
| 401 | token ausente ou inválido, antes do SDK |
| 403 | ator reconhecido sem capacidade ou autorrevisão |
| 404 | objeto fora do escopo, não encontrado ou rota ausente |
| 409 | CAS/identidade/estado/saldo/teto em conflito |
| 413 | corpo ou quota/saída acima do limite |
| 414 / 431 | linha de requisição/cabeçalhos acima dos limites |
| 503 | BUSY, capacidade de workers ou falha operacional/resultado incerto |

Erro fechado: `{"error":{"code":"STALE_VERSION","outcome":"NO_EFFECT","current_version":4}}`.
Versão atual aparece somente em STALE_VERSION autorizado. Não há eco de SQL,
caminho, motivo, token ou texto arbitrário da exceção. Falha de entrega após commit
não desfaz fatos; preserve o ID, consulte a operação ou repita o mesmo conteúdo.

Entrada HTTP: linha até1024 bytes, cabeçalhos até8192/32 entradas/4096 por linha,
corpo até65536 bytes, timeout de socket2s, quatro workers e backlog8. JSON duplicado,
NaN/Infinity, UTF-8 malformado, Transfer-Encoding, cabeçalhos duplicados e campos
extras são recusados. Uma requisição por conexão. Saída é preparada até1MiB;
prefixos de escrita em sockets/stdout não são atômicos. Timeout de socket não é
uma garantia de deadline para todas as chamadas nativas de SQLite.

## CLI confiável

`init DB` lê bootstrap JSON limitado no stdin: `actors` com actor_id/roles e
`limits` opcional. Esse bootstrap é configuração explícita do host, não autoridade
obtida do JSON de uma requisição HTTP. `execute DB --actor ID` recebe um comando
com `action` e os campos acima; JSON com actor_id/roles é recusado. `read DB --actor
ID --contract ID` aceita `--measurement`, `--operation` ou `--history`, e âncoras.

Saídas:0 sucesso;2 entrada/uso inválido;3 recusa de domínio/permissão/quota;4 falha
operacional/IO/BUSY/UNKNOWN. Em uma escrita com falha de stdout depois do commit,
o estado persistido continua disponível por operation_id. Help/erro também tratam
falhas de flush sem deixar o shutdown do CPython substituir o código por120.

O console normal e `python -I -m execucao_contratual_ledger.cli` são superfícies
separadas verificadas em ambiente controlado. Hosts com sitecustomize, PYTHONPATH,
extensões pré-carregadas ou código arbitrário não fazem parte desse isolamento.

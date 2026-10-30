# ChronosAI

Registro de execuções de agentes, políticas para um ledger SQLite local e ferramentas de demonstração de recuperação.

O Chronos registra entradas, saídas e memória por etapa, aplica políticas antes de confirmar SQL no ledger configurado e permite criar uma continuação explícita a partir de registros anteriores. Inclui Studio web, SDK Python e uma interface de ferramentas JSON-RPC por stdio/HTTP.

## Escopo e limites

- SQL é executado **somente no ledger SQLite configurado**, tabela `orders`. O argumento `database` não conecta a bancos externos.
- Cada instrução pode afetar no máximo uma linha. O runtime restringe operações pelo autorizador do SQLite, verifica contagem de alterações e impacto financeiro e só confirma após todas as políticas.
- DDL, controle de transações, anexação de bancos, acesso a outras tabelas, triggers/views e comandos fora do subconjunto permitido são recusados. Isso também pode recusar algumas CTEs.
- O limite de reembolso é 500 USD por operação/valor final, respeitando também o valor do pedido. Valores ausentes, negativos, não numéricos e não finitos são recusados.
- Shell é bloqueado por padrão. Permissões para comandos exatos precisam ser configuradas por código confiável. Não existe sandbox de processos ou garantia de reversão de callbacks externos.
- Pagamentos, limpeza de servidores e autocorreção dos cenários são **simulações predefinidas**. Não há integração operacional com Stripe ou Kubernetes.
- Replay copia registros e memória e exige um callback de continuação. **Não restaura estado de banco, arquivos ou serviços externos**, nem garante idempotência de ações externas.
- O armazenamento é um histórico local mutável, não um registro imutável ou assinado. O hash identifica o conteúdo de cada etapa; não é uma certificação de integridade.
- Tokens e custos apresentados são estimativas fornecidas pelos chamadores. Na ausência de informação ficam em zero e `metrics_kind=NOT_REPORTED`. Latência de execução é medida; não há promessa de 1 ms/2 ms.
- Cache é exato, expira e é opt-in para raciocínio puro; ferramentas e leituras de dados mutáveis nunca usam cache no engine. A chave inclui entrada, memória e `cache_version`. Similaridade textual não autoriza reutilização.

## Instalação e execução

Requer Python 3.10 ou superior. No PowerShell, a partir deste checkout:

```powershell
python -m pip install -e ".[dev]"
$env:CHRONOS_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
# Copie este token para o campo de acesso do Studio e guarde-o com segurança.
Write-Output $env:CHRONOS_API_TOKEN
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Abra <http://127.0.0.1:8000>. A API exige `Authorization: Bearer <token>`; sem um `CHRONOS_API_TOKEN` de pelo menos 32 caracteres, as rotas protegidas retornam 503. Token incorreto/ausente retorna 401. O Studio mantém o token somente em memória da página, inclusive nas exportações; não usa localStorage ou URL para credenciais.

Opcionalmente configure `CHRONOS_READ_TOKEN`, diferente do token administrativo e com pelo menos 32 caracteres, para permitir somente GET/HEAD. Esta é uma aplicação de operador único, sem isolamento entre organizações; use instâncias e bancos separados para limites de confiança diferentes. Para acesso remoto, configure HTTPS no proxy e preserve o cabeçalho de autorização. Use **um único processo/worker** por conjunto de bancos: o histórico ativo é mantido em memória no processo.

Configurações:

| Variável | Comportamento |
|---|---|
| `CHRONOS_API_TOKEN` | Token administrativo obrigatório para API/HTTP MCP |
| `CHRONOS_READ_TOKEN` | Token opcional de leitura |
| `CHRONOS_DATA_DIR` | Diretório dos arquivos SQLite; padrão: checkout |
| `CHRONOS_TRACE_DB` | Caminho explícito do banco de histórico |
| `CHRONOS_LEDGER_DB` | Caminho explícito do ledger |
| `CHRONOS_DEMO_MODE=1` | Habilita cenários/reset e usa arquivos `chronos_demo_*.db` por padrão |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | Credenciais opcionais para provedor externo |

No modo normal um banco novo começa vazio. Bancos existentes são preservados; reiniciar não reseta pedidos nem cria cenários. Execuções persistidas são recuperadas; as que estavam RUNNING passam a INTERRUPTED, sem retomar automaticamente efeitos externos.

## Demonstração isolada

Antes de iniciar o servidor:

```powershell
$env:CHRONOS_DEMO_MODE = "1"
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

O token continua obrigatório. O ledger de demonstração é populado com 250 pedidos fictícios somente quando é criado. O reset é uma ação explícita e existe apenas neste modo. Não aponte os caminhos de demonstração para bancos operacionais.

O interpretador local (`provider="builtin"`) não é um LLM. Ele aceita consultas explícitas e estornos com um único pedido e valor:

- `Consulte o pedido ORD-9921`
- `Estorne ORD-9921 com 45`

Pedidos ambíguos, negados ou sem alvo/valor são recusados, sem inventar uma transferência. Se um provedor externo configurado falhar, a operação falha; não há troca silenciosa para o interpretador local.

## SDK

```python
from chronos import ChronosEngine, ChronosSDK

engine = ChronosEngine(
    db_path="example_demo_traces.db",
    ledger_db_path="example_demo_ledger.db",
    demo_mode=True,
)
sdk = ChronosSDK(engine)

@sdk.step(name="execute_sql", local_sql=True)
def execute_sql(query: str, database: str = "ledger"):
    # Assinatura dos argumentos. No modo local, o engine executa o SQL;
    # este corpo não é chamado, evitando executar a mesma alteração duas vezes.
    raise AssertionError("SQL is executed by the configured local ledger")

with sdk.trace_run("Example", "Scoped local refund"):
    result = execute_sql(
        "UPDATE orders SET refund_usd=45, status='REFUNDED' WHERE order_id='ORD-9921'"
    )
```

Ferramentas decoradas exigem um contexto `trace_run`. Argumentos posicionais, mistos e padrões são vinculados à assinatura antes da inspeção. O estado final é persistido mesmo em caso de falha. Após bloqueio, novos passos são recusados; `self_heal=True` é uma opção explícita para que o chamador processe a observação e tente uma ação corrigida.

Callbacks SQL externos são recusados por padrão, inclusive nos interceptadores. `local_sql=True` direciona a execução exclusivamente ao ledger local e não chama o callback SQL. Outros bancos exigem um adaptador transacional específico, ainda não implementado. Os adaptadores de funções são síncronos; ferramentas assíncronas exigem implementação específica.

Para funções puras de raciocínio, pode-se habilitar `use_cache=True` e informar `cache_version`. Atualize a versão quando mudar o modelo, código ou configuração e inclua na entrada todo contexto externo relevante. O TTL padrão é 300 segundos, com limite de 1.024 entradas.

## MCP e HTTP

```powershell
python -m chronos.mcp_server
```

Stdio é uma interface local do processo: seu acesso depende das permissões do sistema operacional e do cliente que o iniciou. HTTP usa `POST /mcp`, protegido pelo mesmo token da API. Não conecte stdio a usuários não confiáveis por um transporte sem autenticação.

Ferramentas disponíveis: `chronos_inspect_tool`, `chronos_execute_safe_sql`, `chronos_run_agent_prompt` e `chronos_get_ledger`. Execução SQL via MCP também registra uma execução. Não há ferramenta MCP de replay nem comando CLI `chronos replay`; use `ChronosEngine.fork_from_step` com continuação explícita. O endpoint HTTP implementa despacho JSON-RPC básico, sem prometer compatibilidade com todos os transportes/clientes MCP.

## Verificação

```powershell
python -m pytest -q
```

Os testes usam bancos temporários, credenciais fictícias e não usam chaves externas do ambiente. Para incluir o teste em navegador:

```powershell
python -m pip install -e ".[dev,browser-tests]"
python -m playwright install chromium
python -m pytest -q
```

A regressão cobre SQL em massa/CTE/RETURNING, validações antes do commit, limites financeiros, shell, assinatura de funções, bloqueio de execução, cache, concorrência, recuperação após reinício, autenticação/autorização e XSS/exportação no Studio. Sem o extra de navegador, esse teste é explicitamente pulado.

O CSS utilitário do Studio é distribuído localmente. Para reconstruí-lo após alterar classes:

```powershell
npx --yes tailwindcss@3.4.17 -i ./static/tailwind-input.css -o ./static/studio.css --content ./static/index.html --minify
```

## Auditoria

Veja [o relatório original](docs/AUDITORIA-2026-10-30.md) e [as correções e mudanças de compatibilidade](docs/CORRECOES-2026-10-30.md).

Licença Apache 2.0; veja [LICENSE](LICENSE).

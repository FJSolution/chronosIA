#  ChronosAI — Deterministic Time-Travel Debugger, Shadow Firewall & Subgraph Cache for AI Agents

> **"O Git + Datadog Determinístico para Agentes Autônomos."**

##  Visão Geral (Por que este projeto vale milhões)

À medida que empresas migram de simples chatbots para **Agentes Autônomos** que executam queries SQL, comandos de terminal e movimentações financeiras, três gargalos de bilhões de dólares surgem:
1. **Não-determinismo em Produção:** Quando um agente falha no passo 47, engenheiros não conseguem reproduzir o erro sem gastar milhares de tokens re-executando os 46 passos anteriores.
2. **Risco Catastrófico (Blast Radius):** Um único *context drift* faz o LLM emitir um `UPDATE orders SET status='REFUNDED'` sem cláusula `WHERE`, ou um `rm -rf` em produção.
3. **Desperdício de Inferência (GPU/Tokens):** 80% das execuções de agentes repetem sub-grafos idênticos de leitura e raciocínio.

O **ChronosAI** resolve os três problemas em uma única camada de *Runtime Control Plane*:
* ** Deterministic Time-Travel Fork (`chronos/engine.py`):** Clona os passos `1 .. N-1` a partir de snapshots imutáveis em **0ms e 0 tokens ($0.00)**, permitindo editar o prompt/payload do passo `N` e continuar a execução com segurança.
* ** ShadowFirewall (`chronos/firewall.py`):** Intercepta chamadas de ferramentas (`SQL`, `Bash`, `Stripe`, `PII`) antes de tocarem a produção, simula o *Blast Radius* em um ambiente *shadow* e bloqueia mutações destrutivas automaticamente.
* ** Semantic Subgraph Cache (`chronos/cache.py`):** Endereçamento por conteúdo (`SHA-256` normalizado) de sub-rotinas de agentes, reduzindo latência e custo de API em até 80%.

---

##  Como Executar o ChronosAI Studio

```bash
# 1. Rodar a suíte de testes automatizados
pytest -v

# 2. Iniciar o servidor FastAPI + ChronosAI Studio UI
uvicorn server:app --reload --port 8000
```

Acesse **`http://127.0.0.1:8000`** no navegador para abrir o **ChronosAI Studio**.

---

##  Exemplo de Uso do SDK Python (`chronos/sdk.py`)

```python
from chronos import ChronosSDK

sdk = ChronosSDK()

@sdk.step(step_type="TOOL_CALL", name="execute_sql")
def execute_sql(query: str, database: str = "prod_db"):
    return {"status": "COMMITTED", "query": query}

with sdk.trace_run(agent_name="FinOps-Agent", scenario="Order Refund #9921") as run:
    # Se o LLM tentar rodar SQL sem WHERE, o ShadowFirewall bloqueia automaticamente!
    execute_sql(query="UPDATE orders SET status = 'REFUNDED' WHERE order_id = 'ORD-9921';")
```

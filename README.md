<div align="center">

# ⏱️ ChronosAI
### Deterministic Time-Travel Debugger, Shadow Firewall & Subgraph Cache for Autonomous AI Agents

[![License](https://img.shields.io/badge/License-Apache_2.0-indigo.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Control_Plane-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![SQLite SAVEPOINT](https://img.shields.io/badge/Sandbox-SQLite_SAVEPOINT-0ea5e9.svg)](chronos/sandbox_db.py)
[![MCP Server](https://img.shields.io/badge/MCP-JSON--RPC_2.0-8b5cf6.svg)](chronos/mcp_server.py)
[![Tests](https://img.shields.io/badge/Tests-6%2F6_Passing-10b981.svg)](test_chronos.py)

**[English](#-overview-english) • [Português](#-visão-geral-português)**

<br />

<img src="docs/chronos-studio-preview.png" alt="ChronosAI Studio — Deterministic Time-Travel & SQLite SAVEPOINT Firewall" width="100%" />

</div>

---

## 🌍 Overview (English)

> **"The Deterministic Git + Zero-Trust Firewall for Production AI Agents."**

As enterprises transition from simple chatbots to **Autonomous Agents** capable of executing SQL queries, shell commands, and financial transactions, three billion-dollar bottlenecks emerge:
1. **Non-Deterministic Failures:** When an agent fails at Step 47, engineers cannot reproduce the failure locally without re-running (and paying for) Steps 1–46.
2. **Catastrophic Blast Radius:** A single context drift causes an LLM to emit an unbounded `UPDATE orders SET status='REFUNDED'` without a `WHERE` clause, or run `rm -rf` in production.
3. **Redundant Inference Spend:** Up to 80% of agent workflows repeat identical data-fetching and reasoning subgraphs.

**ChronosAI** solves all three in a lightweight, drop-in runtime:
- **🛡️ Transactional Shadow Firewall ([`chronos/sandbox_db.py`](chronos/sandbox_db.py) & [`chronos/firewall.py`](chronos/firewall.py)):** Executes candidate SQL queries inside an isolated **`SAVEPOINT`**, inspects `EXPLAIN QUERY PLAN` for full-table scans, measures exact row mutations and financial impact, and executes an automatic **`ROLLBACK TO SAVEPOINT`** before production data is harmed.
- **⏪ Deterministic Time-Travel Fork ([`chronos/engine.py`](chronos/engine.py)):** Clones Steps `1 .. N-1` from immutable WAL snapshots in **1ms at $0.00 (0 tokens)**, allowing engineers to patch Step `N` and resume execution deterministically.
- **🔌 Official MCP Server ([`chronos/mcp_server.py`](chronos/mcp_server.py)):** Exposes Zero-Trust SQLite `SAVEPOINT` validation and Time-Travel replay over **Model Context Protocol (JSON-RPC 2.0)** for Cursor, Claude Desktop, and Antigravity.
- **⚡ Semantic Subgraph Cache ([`chronos/cache.py`](chronos/cache.py)):** Content-addressable (`SHA-256`) intent normalization that serves validated agent sub-routines in `2ms`.

### Architecture

```mermaid
flowchart LR
    A["Autonomous Agent / LLM"] -->|Tool Call / Step| B["Chronos SDK (@sdk.step)"]
    B --> C{"Subgraph Cache\n(SHA-256)"}
    C -->|Hit: 0 Tokens / 2ms| D["Return Validated Output"]
    C -->|Miss| E["ShadowFirewall Gate"]
    E -->|SQLite SAVEPOINT +\nEXPLAIN QUERY PLAN| F{"Policy Check\n(Risk < 90?)"}
    F -->|Unsafe: >1 row / No WHERE| G["ROLLBACK TO SAVEPOINT\n+ Halt Run"]
    F -->|Safe: Primary Key Scoped| H["RELEASE SAVEPOINT (COMMIT)\n+ Save WAL Snapshot"]
    G -.->|1-Click Time-Travel Fork\n(0 Tokens on Steps 1..N-1)| E
```

---

## 🇧🇷 Visão Geral (Português)

> **"O Freio de Emergência + Máquina do Tempo para Agentes de IA em Produção."**

O **ChronosAI** é uma plataforma open-core de governança, auditoria e replay determinístico para agentes autônomos:
1. **Shadow Firewall Transacional (`SAVEPOINT`):** Antes que uma query SQL gerada pela IA altere o banco de dados de produção, o Chronos testa a query dentro de um `SAVEPOINT` real no SQLite, analisa o `EXPLAIN QUERY PLAN` e, se detectar mutação em massa (como um `UPDATE` sem `WHERE` ou `DROP TABLE`), executa **`ROLLBACK` automático** em milissegundos.
2. **Time-Travel Debugging (Máquina do Tempo):** Se o agente falhar ou for bloqueado no Passo #5, você não precisa gastar tokens refazendo os Passos #1 a #4. O Chronos restaura a memória congelada em **1ms com 0 tokens gastos** e executa apenas a partir do passo corrigido.
3. **Agente de IA ao Vivo + Servidor MCP:** Digite comandos em linguagem natural diretamente no Studio ou conecte o servidor MCP (`python -m chronos.mcp_server`) no **Cursor / Claude Desktop**.

---

## 🚀 Quickstart (Como Rodar em 30 Segundos)

```bash
# 1. Clone the repository
git clone https://github.com/FJSolution/chronosIA.git
cd chronosIA

# 2. Install in editable mode
pip install -e ".[dev]"

# 3. Run the automated End-to-End Test Suite
pytest -v

# 4. Launch the Chronos Studio Control Plane
python -m uvicorn server:app --reload --port 8000
```

Abra **`http://127.0.0.1:8000`** no seu navegador para acessar o **Chronos Studio**.

---

## 💻 SDK Usage (Integração em 2 Linhas)

```python
from chronos import ChronosSDK

sdk = ChronosSDK()

@sdk.step(step_type="TOOL_CALL", name="execute_sql")
def execute_sql(query: str, database: str = "prod_ledger"):
    return {"status": "COMMITTED", "query": query}

with sdk.trace_run(agent_name="FinOps-Agent", scenario="Order Refund #9921") as run:
    # Se o LLM tentar rodar SQL sem WHERE, o ShadowFirewall faz ROLLBACK no SAVEPOINT automaticamente!
    execute_sql(query="UPDATE orders SET status = 'REFUNDED' WHERE order_id = 'ORD-9921';")
```

---

## 📂 Estrutura do Projeto

| Arquivo | Descrição |
| :--- | :--- |
| [`chronos/engine.py`](chronos/engine.py) | Motor de grafos DAG, gravação de snapshots e `fork_from_step` (Time-Travel). |
| [`chronos/sandbox_db.py`](chronos/sandbox_db.py) | Banco SQLite real (`250 pedidos`) com execução isolada via `SAVEPOINT` / `ROLLBACK` e `EXPLAIN QUERY PLAN`. |
| [`chronos/firewall.py`](chronos/firewall.py) | Motor de regras Zero-Trust (`SQL-001`, `SQL-002`, `FIN-001`, `SYS-001`, `SEC-001`). |
| [`chronos/llm_agent.py`](chronos/llm_agent.py) | Agente de IA ao vivo multi-provedor (Gemini, OpenAI, Ollama e motor NL-to-SQL nativo). |
| [`chronos/mcp_server.py`](chronos/mcp_server.py) | Servidor oficial **Model Context Protocol (JSON-RPC 2.0)** via `stdio` e HTTP. |
| [`chronos/cache.py`](chronos/cache.py) | Cache semântico endereçado por conteúdo (`SHA-256`) com cálculo de ROI. |
| [`chronos/storage.py`](chronos/storage.py) | Persistência em SQLite (`PRAGMA journal_mode=WAL`) e exportador de pacotes `.chronos.json`. |
| [`chronos/sdk.py`](chronos/sdk.py) | Decoradores Python (`@sdk.step`, `sdk.trace_run`) para fácil integração. |
| [`server.py`](server.py) | API FastAPI do Control Plane. |
| [`static/index.html`](static/index.html) | Interface web interativa do **Chronos Studio**. |
| [`test_chronos.py`](test_chronos.py) | Suíte de testes ponta a ponta (`pytest`). |

---

## 📄 License

Distribuído sob a licença **Apache 2.0**. Veja [`LICENSE`](LICENSE) para mais detalhes.

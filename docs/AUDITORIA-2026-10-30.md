# Auditoria técnica do ChronosAI

Registro do estado anterior às correções. Veja [as correções posteriores](CORRECOES-2026-10-30.md).

Data: 30/10/2026. Escopo: código local, configurações, interface e verificações isoladas. Nenhuma correção funcional foi aplicada. Os bancos originais foram preservados.

## Parecer

O ChronosAI é um protótipo funcional de observabilidade e controle de ferramentas para agentes de IA. Registra etapas, testa consultas SQL em SQLite, aplica regras, reutiliza respostas e permite criar uma execução derivada de um histórico. O público pretendido é quem desenvolve agentes com acesso a bancos, pagamentos ou operações de infraestrutura.

Serve hoje para demonstrações, experimentação e desenvolvimento local. As garantias anunciadas de firewall zero-trust, replay determinístico, snapshots imutáveis e proteção de produção excedem o que a implementação demonstra. Foram reproduzidas falhas que permitem alterações indevidas, respostas incorretas do cache e perda de acesso ao histórico após reinício.

## O que existe e para que serve

| Recurso | Implementação encontrada | Limite relevante |
|---|---|---|
| Studio | Página HTML/JavaScript e API FastAPI para execuções, passos, políticas, cache, ledger e exportação | Sem autenticação própria; renderização insegura de conteúdo variável |
| Registro de execução | Lista sequencial de etapas, entradas, saídas e cópias de memória em SQLite | Não é um executor geral de DAG; registros podem ser sobrescritos |
| Firewall SQL | Executa uma consulta dentro de SAVEPOINT, observa efeitos e decide commit/rollback | Atua no ledger SQLite configurado; o argumento `database` não seleciona nem valida um banco externo |
| Regras financeiras, shell e segredos | Limite de `amount_usd`, expressões regulares e retorno de violações | Não existe sandbox de processos/container nem implementação de vault |
| Fork/replay | Copia passos anteriores e sua memória; chama uma continuação fornecida | Não restaura banco, arquivos, serviços externos ou estado completo do processo |
| Cache | Hash de entrada e similaridade de palavras/trigramas em vetores de 128 dimensões | Não inclui memória, versão de dados ou validade temporal; pode confundir intenções opostas |
| Agente por linguagem natural | Código para chamar Gemini, OpenAI e Ollama; fallback por regras locais | Chamadas externas não foram testadas; fallback pode interpretar consulta como mutação |
| Integrações | Decorador Python, interceptador de chamadas no formato OpenAI e adaptação de ferramentas por atributos | Cobertura real de frameworks não demonstrada por testes com suas bibliotecas |
| MCP | Quatro ferramentas, despacho JSON-RPC, stdio e endpoint HTTP | Compatibilidade ponta a ponta com clientes não foi validada; não expõe ferramenta de fork/replay |
| Cenários FinOps/SRE e autocorreção | Exemplos programados; SQL altera o ledger de demonstração | Stripe, liberação de disco e reflexão corretiva são respostas predefinidas, sem execução real desses serviços |

O banco é populado com 250 pedidos fictícios. Os nomes de produção nos cenários não comprovam integração com infraestrutura de produção.

## Método e resultados

- Leitura dos módulos Python, README, empacotamento e trechos relevantes da interface.
- Execução da suíte em cópia temporária do projeto, sem copiar os bancos originais e removendo as chaves Gemini/OpenAI do ambiente do subprocesso: **8 testes passaram em 8,44 segundos**. Houve um aviso de depreciação no cliente de testes instalado.
- Provas adicionais em bancos SQLite em memória e em diretórios temporários; verificações da API pelo TestClient na cópia isolada.
- Nenhum pagamento, comando destrutivo de sistema ou chamada a provedor de IA foi executado nas provas adicionais. Os exemplos de shell foram apenas enviados ao avaliador de regras.
- Não foram feitos pentest de implantação, auditoria de dependências por CVE, teste de carga ou validação visual em navegador. O achado de XSS é de análise estática.

## Achados prioritários

### 1. Crítico — alteração em massa aprovada com contagem zero

Local: `chronos/sandbox_db.py:153`, `chronos/sandbox_db.py:176`, `chronos/firewall.py:78`.

A consulta abaixo foi aprovada e persistiu a alteração dos 250 pedidos, enquanto o resultado indicou `estimated_rows_mutated=0`:

```sql
WITH ids AS (SELECT order_id FROM orders)
UPDATE orders SET status='REFUNDED'
WHERE order_id IN (SELECT order_id FROM ids)
```

A implementação converte contagens negativas de `cursor.rowcount` em zero. Nesta forma de consulta a contagem não informou as linhas alteradas. A checagem posterior confiou no zero e na presença de WHERE.

Correção: restringir instruções aceitas, medir alterações de forma confiável incluindo efeitos indiretos e usar uma única decisão de política. Uma contagem desconhecida deve impedir aprovação automática.

### 2. Crítico — commit antes da decisão completa do firewall

Local: `chronos/firewall.py:64`, `chronos/firewall.py:113`, `chronos/sandbox_db.py:184`.

Uma atualização de `refund_usd=900` no pedido ORD-9921, acompanhada de `amount_usd=900`, retornou `allowed=false` e `REQUIRE_APPROVAL`, mas o valor 900 já estava persistido. O SQL é confirmado antes das verificações financeira e de segredos.

Separadamente, uma atualização de `refund_usd=9000` sem o argumento `amount_usd` foi aprovada, apesar do limite configurado de 500. O impacto financeiro medido no banco não é usado para impor esse limite.

Correção: avaliar todas as políticas dentro da mesma unidade transacional e confirmar apenas após aprovação integral; validar o efeito financeiro efetivo.

### 3. Alto — API permite consulta e mutação sem autenticação

Local: `server.py:75`, `server.py:120`, `server.py:150`.

Sem credenciais, as chamadas de visão geral, reset e execução SQL via MCP retornaram HTTP 200. Não há autenticação, autorização por recurso ou separação de usuários no código examinado. Se a aplicação for disponibilizada a terceiros, esses terceiros terão acesso a essas operações, salvo proteção externa não presente no projeto.

Correção: autenticação e autorização nas operações, isolamento por usuário/organização e separação explícita entre demonstração e dados operacionais.

### 4. Alto — argumentos posicionais contornam a regra financeira

Local: `chronos/sdk.py:63`; padrão semelhante em `chronos/integrations.py:39`.

Uma função decorada como `wire_transfer`, chamada com `transfer(25000)`, executou o callback e foi marcada COMPLETED. O payload virou `{'args': [25000]}`; a regra procurou `amount_usd`, encontrou ausência e avaliou zero.

Correção: vincular argumentos à assinatura real da função, incluindo valores padrão, e validar um esquema obrigatório por ferramenta antes da execução.

### 5. Alto — cache devolve resposta para intenção oposta ou memória diferente

Local: `chronos/cache.py:84`, `chronos/cache.py:153`, `chronos/engine.py:190`.

Provas reproduzidas:

- Cache de “Reembolsar o pedido ORD-9921 com valor 120” foi reutilizado para “Nao reembolsar o pedido ORD-9921 com valor 120”, com similaridade 0,9574 e o limite padrão de 0,86.
- Uma função dependente de memória foi executada com valor 10. Na segunda chamada, a memória era 99, mas o cache retornou 10 sem chamar a função novamente.

O cache também é habilitado por padrão para ferramentas com efeitos colaterais. O firewall SQL roda antes da consulta ao cache, podendo alterar o banco enquanto o callback é omitido por um hit.

Correção: cache apenas em operações explicitamente apropriadas, chave com contexto/versões relevantes, expiração e invalidação; desabilitar aproximação semântica em decisões ou ações sensíveis.

### 6. Alto — bloqueio não interrompe obrigatoriamente a execução

Local: `chronos/engine.py:123`, `chronos/engine.py:183`, `chronos/engine.py:219`.

Após uma ferramenta bloqueada mudar o run para HALTED_BY_FIREWALL, uma chamada seguinte de `record_step` executou outro callback. O status é apenas um registro; não existe uma barreira de execução para runs encerrados/bloqueados.

Correção: máquina de estados que impeça novos efeitos depois do bloqueio e distinga explicitamente um fluxo autorizado de recuperação.

### 7. Alto — reinício reseta ledger e não recupera histórico na aplicação

Local: `server.py:28`, `server.py:29`, `chronos/engine.py:97`, `chronos/storage.py:66`, `chronos/sdk.py:37`.

O módulo do servidor chama `reset_seed_data()` ao ser importado. O engine inicia com um dicionário vazio e não chama `load_all_runs()`. Em teste de reabertura, o banco possuía um run, mas o novo engine expunha zero. Os registros permanecem no banco, porém não ficam disponíveis nas rotas e no fork que consultam a memória.

O context manager do SDK também marcou COMPLETED em memória, mantendo RUNNING no registro persistido.

Correção: seed explícito apenas para demonstração, reidratação de execuções e persistência de todas as transições de estado.

### 8. Alto — conteúdo controlável é interpolado em HTML

Local: `static/index.html:922`, `static/index.html:951`, `static/index.html:1002`, `static/index.html:1298`, `static/index.html:1403`.

Prompts entram em `run.scenario`; cenário, dados do ledger e resultados são interpolados em `innerHTML` sem escape nesses caminhos. Isso cria caminho para XSS no Studio. Constatação por fluxo de dados e leitura de código; não houve execução de payload em navegador.

Correção: construir nós e usar `textContent` para conteúdo variável; sanitizar somente quando HTML for intencional.

### 9. Alto — proteção de shell é uma lista limitada de padrões

Local: `chronos/firewall.py:141`.

O texto `rm -fr /var/log/*` recebeu `allowed=true`, enquanto a expressão procura `rm -rf`. Não há execução isolada de shell apesar das mensagens citarem container e namespace. A aprovação é inadequada como barreira geral antes de callbacks externos.

Correção: ferramentas estreitas com operações e argumentos permitidos, privilégio mínimo e isolamento real quando necessário; não depender de blacklist textual.

### 10. Médio — replay e auditoria não sustentam as garantias anunciadas

Local: `chronos/engine.py:258`, `chronos/engine.py:311`, `chronos/storage.py:39`, `chronos/storage.py:72`.

O fork copia objetos e a memória registrada; não reconstrói estado externo. Sem `continuation_fn`, um fork do passo 1 retornou FORKED_SUCCESS com zero passos. O hash cobre tipo, nome e entrada, não o snapshot completo. A gravação faz UPSERT, sem trilha imutável ou validação de integridade. A exportação menciona `chronos replay`, mas não há CLI registrada nem implementação desse comando no projeto inspecionado.

Correção: explicitar limites, exigir continuação válida, tratar efeitos e idempotência, implementar recuperação/importação e definir as garantias de integridade necessárias.

### 11. Médio — métricas estimadas e ações simuladas parecem resultados operacionais

Local: `chronos/engine.py:132`, `chronos/engine.py:210`, `chronos/engine.py:246`, `chronos/engine.py:303`, `chronos/llm_agent.py`, `chronos/scenarios.py`, `chronos/integrations.py`.

Tokens e custos são números informados/constantes; latências de cache e replay são fixadas em 2 ms e 1 ms. Mesmo o fallback sem LLM recebe custos e tokens estimados. A autocorreção de demonstração utiliza SQL e explicações predefinidas. O agente retorna COMMITTED_TO_SQLITE em seu callback genérico mesmo para ferramentas que não são SQL.

Correção: distinguir medições, estimativas e simulações na API e na interface; coletar uso real quando disponível; evitar afirmar execução de serviços que não ocorreu.

### 12. Médio — lacunas de robustez e distribuição

Local: `chronos/sandbox_db.py:22`, `chronos/storage.py:21`, `server.py:28`, `pyproject.toml:16`, `chronos/llm_agent.py:12`.

- Conexões SQLite globais são compartilhadas com `check_same_thread=False`, sem sincronização da operação completa e com nome fixo de savepoint. Rotas síncronas podem executar concorrentemente. Risco identificado por inspeção; corrida não reproduzida nesta auditoria.
- `httpx` é importado em tempo de execução, mas só está declarado no extra `dev`. Uma instalação básica não tem a dependência de runtime garantida.
- O fallback de linguagem natural contém decisões perigosas para uso geral: uma frase com ORD-9921 é encaminhada a UPDATE de reembolso mesmo sem pedido de alteração; pedidos pequenos de transferência podem ser transformados em 25.000 na regra de demonstração.
- As oito verificações existentes cobrem cenários dirigidos; não cobrem os desvios reproduzidos acima, reinício, concorrência ou renderização de conteúdo não confiável.

## Ordem sugerida de trabalho

1. Corrigir atomização das políticas, contagem de efeitos SQL, argumentos do SDK, estado de bloqueio e cache; adicionar testes de regressão dos casos reproduzidos.
2. Corrigir autenticação/autorização e renderização HTML antes de qualquer exposição a terceiros.
3. Separar modo demo, remover reset automático, recuperar histórico e persistir corretamente os estados finais.
4. Definir o contrato real de replay, ferramentas externas, idempotência e aprovação humana; substituir simulações onde houver necessidade de integração operacional.
5. Ajustar métricas, documentação, dependências e verificações de concorrência/compatibilidade.

O valor do projeto está em tornar visíveis as ações de agentes e permitir experimentar políticas e recuperação. Sua adoção como controle de segurança depende da resolução dos achados críticos e altos, com validação em cenários reais de integração.

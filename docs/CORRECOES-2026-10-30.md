# Correções da auditoria — 30/10/2026

Este documento descreve a implementação posterior à auditoria original. Os bancos existentes não foram resetados, e nenhuma chamada externa de IA, pagamento ou shell operacional foi usada na validação.

## Alterações por achado

| Achado original | Correção |
|---|---|
| SQL em massa aprovado como zero linhas | Medição por diferença de `total_changes` após consumir `RETURNING`, com transação protegida por lock; restrições no autorizador do SQLite; bloqueio de DDL, triggers/views, outras tabelas, conexão/transações, REPLACE e operações não permitidas |
| Commit anterior à decisão financeira/segredos | Políticas de entrada antes do SQL; validação dos efeitos financeiros dentro da transação antes do commit; limite aplicado mesmo sem `amount_usd` no payload |
| API sem autenticação | Bearer token obrigatório; configuração ausente falha fechada; token opcional de leitura; operações de demonstração recusadas fora desse modo |
| Argumentos posicionais contornando limite | Binding da assinatura real da função, incluindo padrões e argumentos mistos; callbacks recebem os mesmos valores vinculados |
| Cache confundindo negação/contexto | Reutilização exclusivamente exata; entrada, memória e versão na chave; opt-in somente para raciocínio puro; TTL, tamanho limitado, cópias defensivas e invalidação em chamadas de ferramentas |
| Execução continua após bloqueio | `record_step` aceita somente runs ativos em RUNNING; recuperação exige a opção explícita `self_heal` |
| Reset no reinício e histórico invisível | Removidos reset e cenários automáticos; seed apenas na criação explícita de demo; recuperação dos runs e marcação de execuções interrompidas; finalização do SDK persistida |
| XSS no Studio | Escape contextual de texto/JSON/atributos de handlers, login com token só em memória e exportação autenticada; remoção dos scripts remotos e CSS distribuído localmente |
| Regex de shell contornável | Shell negado por padrão; somente comandos exatos cadastrados por código confiável são elegíveis; nenhum container/namespace fictício é anunciado |
| Replay e trilha com garantias excessivas | Continuação obrigatória, falha em continuação vazia/erro, hash completo do conteúdo da etapa; removida referência a CLI inexistente e declarados limites de estado externo e integridade |
| Simulações/métricas confundidas com resultados reais | Modo DEMO nas execuções e aviso no Studio; pagamentos/shell de exemplo identificados como simulados; custos/tokens classificados e latência real medida; sem fallback silencioso após falha de provedor |
| Concorrência, distribuição e testes | Locks de conexão/runtime, transação iniciada antes de capturar estado, `httpx` como dependência normal e regressões de API, SDK, SQL, reinício, concorrência e navegador |

## Mudanças de compatibilidade

- Configure `CHRONOS_API_TOKEN` com pelo menos 32 caracteres para acessar a API e entrar no Studio. `CHRONOS_READ_TOKEN` é opcional e permite somente leitura.
- Demonstrações, reset e continuações prontas da interface requerem `CHRONOS_DEMO_MODE=1`. Os arquivos padrão de demonstração são separados dos arquivos normais.
- SQL por SDK/interceptador exige `local_sql=True`, executa exclusivamente no ledger configurado e **não chama o callback SQL**. Isso elimina a execução duplicada e a falsa validação de outro banco por uma cópia local.
- Callbacks de SQL externo, objetos de ferramenta com caminhos assíncronos não protegidos e ferramentas ausentes no registry são recusados.
- Ferramentas decoradas precisam de `trace_run`; fora dele não executam silenciosamente sem proteção.
- Cache aproximado foi desabilitado. Funções puras podem optar pelo cache exato e devem atualizar `cache_version` quando suas dependências mudarem.
- O interpretador offline aceita leituras explícitas e estornos com alvo/valor. Não inventa pagamentos, não transforma consultas em estornos e não troca de provedor silenciosamente.
- Falhas de validação retornam erros, não resultados simulados de sucesso. Clientes devem tratar HTTP 400/401/403/503.

## Verificação

Resultado final: **68 testes passaram**, incluindo Chromium headless, em 4,48 segundos na execução final. Houve um aviso de depreciação da combinação Starlette/httpx instalada, sem falhas funcionais. `git diff --check` não apontou problemas de whitespace.

Os testes usam diretórios temporários ou bancos em memória; o servidor dos testes recebe configuração isolada antes da importação. As credenciais de provedores externos são removidas desse processo de teste.

A suíte inclui os oito cenários anteriores ajustados ao novo contrato e regressões dos desvios da auditoria. O teste de navegador executa Chromium headless e roteia as requisições ao TestClient local, sem serviço externo: verifica login inválido/válido, exportação com autenticação e renderização de payloads HTML em cenário, memória, argumentos e saída.

Comandos:

```text
python -m pytest -q
git diff --check
```

Para habilitar a verificação de navegador em outra máquina, instale o extra `browser-tests` e o Chromium do Playwright, conforme o README.

## Limites que permanecem explícitos

O sistema continua sendo um runtime local de operador único. Não passou a implementar pagamento real, sandbox de processos, isolamento multitenant, recuperação de efeitos externos, journal imutável/assinado ou compatibilidade universal de frameworks/MCP. Essas capacidades não são simuladas como garantias de produção. Use um único worker por conjunto de bancos e adaptadores próprios para efeitos externos; funções Python registradas são código confiável e não ficam isoladas pelo runtime.

As decisões sobre contagem de efeitos e restrição de instruções seguem a documentação primária de [total_changes](https://www.sqlite.org/c3ref/total_changes.html) e do [autorizador SQLite](https://www.sqlite.org/c3ref/set_authorizer.html).

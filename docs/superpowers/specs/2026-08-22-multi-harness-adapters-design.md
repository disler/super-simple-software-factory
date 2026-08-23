# Multi-harness adapters para o SSSF

Data: 2026-08-22
Status: design aprovado; especificação aguardando revisão

## Contexto

O SSSF controla o ciclo de desenvolvimento por meio de ADWs em Python. O
controle de sequência, retries, envelopes, gates, permissões e tracing pertence
ao código, enquanto o harness executa somente uma fase agentiva delimitada.

A implementação atual executa apenas o Pi. O schema aceita `claude_code`, mas o
respectivo módulo é um stub, e o Codex não está representado. Este marco adiciona
Claude Code e Codex sem transformar o SSSF em uma camada de APIs de modelos e
sem alterar os doze workflows existentes.

## Objetivo

Permitir que cada agente configurado no roster escolha, de forma independente:

- harness: Pi, Claude Code ou Codex;
- modelo;
- effort;
- capacidades portáteis;
- limites de escrita.

Uma task poderá usar Claude Code em uma fase e Codex em outra. Fases
determinísticas, como executar testes, lint, typecheck e build, continuarão sob
controle do Python e não receberão modelo.

## Fora do escopo

Este marco não inclui:

- VMs descartáveis ou sandboxes remotos;
- Git worktrees ou branches por execução;
- merge, push ou criação de pull request;
- fan-out, best-of-N ou Herdr;
- gerenciamento de credenciais ou API keys;
- chamadas diretas às APIs dos provedores;
- novas fases ou alterações nos workflows existentes;
- suíte automatizada de testes.

Worktrees, branches isoladas e integração explícita formarão um segundo marco.

## Decisão arquitetural

O núcleo chamará uma interface comum, implementada por um adapter por harness:

```text
ADW -> agents.py -> HarnessAdapter -> Pi | Claude Code | Codex
                     |
                     v
          eventos, sessão e resultado normalizados
                     |
                     v
             envelopes -> gates -> tracing -> retry
```

O adapter encapsula somente diferenças do CLI. Renderização de prompts,
validação Pydantic, gates, retries, proteção por diff e persistência continuam
no núcleo.

### Contratos compartilhados

Os contratos terão tipos explícitos e responsabilidades separadas:

- `HarnessRequest`: prompts, modelo, effort, capabilities, diretório de trabalho,
  schema de saída e sessão anterior;
- `HarnessResult`: resposta final, sessão efetiva, uso e metadados disponíveis;
- `HarnessEvent`: evento normalizado para console e tracer;
- `HarnessAdapter`: valida, constrói e executa uma chamada;
- `HarnessRegistry`: resolve `pi`, `claude_code` ou `codex`.

O request sempre recebe um `cwd` explícito. No primeiro marco ele aponta para o
checkout atual; no marco de worktrees poderá apontar para o checkout isolado sem
alterar a interface.

### Implementações

- `agent_pi.py` será adaptado ao contrato comum, preservando seu comportamento.
- `agent_cc.py` deixará de ser stub e implementará Claude Code.
- `agent_codex.py` implementará Codex.
- `agents.py` deixará de importar ou tipar resultados específicos do Pi.

Cada adapter será responsável por:

- verificar o executável e capacidades exigidas;
- traduzir modelo, effort, tools e permissões;
- construir o comando nativo sem fallback silencioso;
- consumir e persistir JSONL;
- normalizar eventos e uso disponíveis;
- criar ou retomar sessões;
- notificar criação e encerramento do processo;
- produzir erros tipados sem expor credenciais ou prompts completos.

## Configuração

O YAML existente continuará válido. `effort` será o campo preferido, e
`thinking` continuará aceito como alias legado.

```yaml
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash
  effort: medium

agents:
  - name: planner
    coding_agent: claude_code
    model: opus
    effort: max

  - name: builder
    coding_agent: codex
    model: gpt-5.6-terra
    effort: high

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-sol
    effort: xhigh
```

### Regras de resolução

- `coding_agent` aceita `pi`, `claude_code` e `codex`.
- Defaults continuam podendo ser sobrescritos por agente.
- `thinking` sozinho é convertido para o campo normalizado `effort`.
- `thinking` e `effort` iguais são aceitos.
- Valores diferentes nos dois campos causam erro de configuração.
- Modelo e effort são enviados ao harness sem substituição ou downgrade.
- Cada adapter valida os níveis de effort que consegue representar.
- Modelo indisponível para a versão, conta ou autenticação falha na fase.
- Não haverá catálogo estático global de modelos.
- Não haverá fallback automático de modelo ou harness.

`harness_engineering` permanece exclusivo do Pi neste marco. Uma lista não
vazia em Claude Code ou Codex causará erro antecipado. Ferramentas registradas
por extensões Pi, incluindo `subagent_*`, não serão simuladas nos outros
harnesses.

## Capabilities portáteis

O vocabulário canônico será:

- `read`;
- `search`;
- `shell`;
- `write`;
- `edit`.

Aliases legados do Pi continuam válidos:

- `bash` -> `shell`;
- `grep`, `find` e `ls` -> `search`.

Pi e Claude Code traduzirão capabilities para allowlists nativas. Como Codex
não possui correspondência individual para toda combinação, seu adapter aceitará
somente perfis que possa impor:

- inspeção: `read + search + shell`;
- mutação: inspeção + `write + edit`.

Uma combinação Codex fora desses perfis falhará antes de abrir a fase. Nenhum
adapter ignorará uma restrição que não consiga aplicar.

O adapter Pi também continuará aceitando nomes adicionais registrados pelos
arquivos de `harness_engineering`. Esses nomes são extensões nativas, não fazem
parte do vocabulário portátil e só são válidos quando o mesmo agente carrega a
extensão que os registra.

## Permissões e sandbox

`writes` continuará sendo uma política de orquestração para mutações no repositório,
não um sandbox de filesystem independente:

- `writes: []`: agente read-only em relação ao repositório;
- lista de caminhos: somente esses caminhos podem permanecer alterados;
- chave ausente: escrita irrestrita, exceto em `protected_files`;
- o runtime em `data_dir` permanece sempre gravável pelo SSSF.

Os adapters inferirão as permissões:

- Pi usa a allowlist existente, mas não possui sandbox nativo: workloads não
  confiáveis exigem isolamento externo;
- Claude Code usa tools restritas, modo não interativo, sandbox nativo e falha
  se o sandbox exigido não estiver disponível;
- Codex usa `read-only` somente com o perfil de inspeção e `writes: []`; usa
  `workspace-write` somente com o perfil de mutação e writes omitido ou não
  vazio, sempre com `approval_policy=never` e sem a opção que desativa o
  sandbox.

O monitor antes/depois pelo Git continuará como salvaguarda de integridade em
todos os harnesses: faz backup byte a byte de caminhos Git-visíveis ordinários,
restaura mudanças negadas quando possível e falha a fase. Ele não é sandbox nem
protege contra `.git`, caminhos ignorados, nós especiais, symlink/TOCTOU ou
processos deliberadamente destacados. Mudança em HEAD/ref/index será reportada,
sem restauração automática, para inspeção do operador. O primeiro marco ainda
trabalha no checkout atual; isolamento por worktree/checkout descartável é o
próximo marco explicitamente adiado.

## Fluxo de execução

Para cada fase agentiva:

1. resolver defaults e configuração do agente;
2. validar harness, effort, capabilities e extensões;
3. renderizar os mesmos templates `system.md` e `user.md`;
4. gerar o JSON Schema a partir do envelope Pydantic;
5. resolver ou criar a sessão do harness;
6. executar o adapter no `cwd` fornecido;
7. normalizar eventos JSONL e enviá-los ao tracer;
8. obter o resultado estruturado;
9. validar novamente o resultado com Pydantic;
10. executar gates e proteção de escrita;
11. reenviar correções na mesma sessão quando permitido;
12. persistir envelope, sessão, eventos e resultado final.

### Prompts e schema

- Pi mantém sua interface de system prompt e seu parser atual.
- Claude Code recebe o system prompt por `--system-prompt` e o schema por
  `--json-schema`.
- Codex recebe o mesmo system prompt por `developer_instructions` e o schema por
  `--output-schema`.
- O user prompt é a mensagem da chamada em todos os adapters.
- Instruções nativas do repositório, como `AGENTS.md` e `CLAUDE.md`, continuam
  aditivas; o SSSF não tenta apagar o contexto próprio do harness.
- A validação Pydantic do núcleo é obrigatória mesmo quando o CLI valida o schema.

## Sessões e retries

`agent_map.json` registrará agente, harness, modelo e session ID.

- Pi continua usando o mesmo session ID como create-or-continue.
- Claude Code usa um UUID na criação e `--resume` em chamadas posteriores.
- Codex captura o ID emitido no JSONL e usa `codex exec resume`.
- Uma sessão só é retomada quando `{agente, harness, modelo}` coincide.
- Mudar somente effort não invalida a sessão.
- Mudar harness ou modelo cria uma sessão nova e substitui a sessão corrente do
  agente.
- Entradas legadas sem harness são interpretadas como sessões Pi.
- Retries de JSON e gates usam a sessão já aberta.

## Erros

O núcleo distinguirá pelo menos:

- harness ausente ou incompatível;
- configuração inválida;
- capability sem tradução segura;
- modelo ou effort rejeitado;
- autenticação ausente;
- processo com exit code não zero;
- JSONL malformado;
- resultado final ausente;
- envelope inválido;
- gate reprovado;
- violação de escrita.

Mensagens incluirão harness, agente, fase, valor ofensivo e formato esperado. A
saída bruta será preservada no diretório da sessão, mas mensagens de erro e
eventos não registrarão credenciais nem o conteúdo integral dos prompts.

Somente envelope inválido e gate reprovado entram nos retries já delimitados.
Falhas de CLI, autenticação, protocolo ou permissão encerram a fase sem trocar de
harness.

## Observabilidade

O SQLite e o visualizer manterão compatibilidade com os eventos atuais. O
tracing registrará, quando fornecido pelo CLI:

- harness;
- modelo;
- effort;
- session ID;
- início e fim do processo;
- exit code;
- eventos de tools normalizados;
- tokens, custo e ocupação de contexto disponíveis.

Campos indisponíveis em um harness serão nulos, nunca fabricados. Arquivos
JSONL brutos continuarão sendo a fonte de diagnóstico.

## Instalação e migração

O `install.py` continuará estampando o SSSF em qualquer repositório. Ele também:

- instalará os três módulos de adapter;
- reportará quais CLIs foram encontrados e suas versões;
- não exigirá que todos os CLIs estejam presentes;
- não testará autenticação nem armazenará chaves;
- manterá Pi como default para configurações legadas.

A ausência de um CLI selecionado será erro somente no preflight da respectiva
fase. README e referências descreverão configuração híbrida, capabilities,
effort, sessões, sandboxes, diagnóstico e migração de `thinking`.

## Validação do marco

Por decisão do projeto, não será adicionada suíte automatizada. A validação de
entrega consistirá em:

1. smoke test real e read-only com Claude Code;
2. envelope tipado válido;
3. segunda chamada retomando a mesma sessão;
4. smoke test equivalente com Codex;
5. confirmação no tracing dos harnesses, modelos e IDs de sessão;
6. confirmação de que nenhum arquivo do repositório foi alterado;
7. nenhuma operação de commit, push, merge ou criação de PR.

Os modelos usados no smoke serão escolhidos entre os disponíveis nas contas já
autenticadas. Testes funcionais mais amplos serão executados posteriormente pelo
proprietário do repositório.

## Critérios de aceite

O marco estará aceito quando:

- Pi continuar executando configurações existentes;
- Claude Code executar uma fase completa com envelope, tracing e retomada;
- Codex executar a mesma interface com envelope, tracing e retomada;
- uma task puder configurar harness, modelo e effort por agente;
- combinações inválidas falharem sem fallback silencioso;
- fases determinísticas permanecerem independentes de modelo;
- os workflows atuais não precisarem conhecer flags específicas dos CLIs;
- documentação e instalador representarem o comportamento implementado;
- os dois smoke tests acordados passarem sem modificar o repositório.

## Marco seguinte

Uma especificação separada cobrirá:

- branch `sssf/<adw_id>` por execução;
- worktree persistente por run;
- sandbox apontado para a worktree;
- manutenção da branch quando a execução falhar;
- integração local explícita e somente fast-forward;
- push e abertura de PR somente por comando explícito;
- limpeza explícita de worktree e branch.

Nenhuma dessas operações será antecipada pelo marco multi-harness.

## Referências oficiais

- Claude Code CLI: <https://code.claude.com/docs/en/cli-reference>
- Claude Code headless: <https://code.claude.com/docs/en/headless>
- Claude Code sandboxing: <https://code.claude.com/docs/en/sandboxing>
- Codex developer commands: <https://learn.chatgpt.com/docs/developer-commands?surface=cli>
- Codex configuration: <https://learn.chatgpt.com/docs/config-file/config-reference>

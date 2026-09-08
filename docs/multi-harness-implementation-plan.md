# Plano de implementação: múltiplos harnesses no SSSF

## Objetivo

Permitir que cada agente use o harness configurado, mantendo o Python como
orquestrador do fluxo:

```text
ADW Python -> adapter configurado -> Pi, Claude Code, Codex ou outra CLI
```

O adapter executa uma chamada de agente. Fases, retries, gates, permissões,
handoffs, SQLite e aceitação continuam onde já estão.

## Limites de escopo

- Não alterar a estrutura dos ADWs.
- Não criar um novo framework de agentes.
- Não mudar envelopes, gates ou banco sem necessidade comprovada.
- Não renomear agora `coding_agent` ou `thinking`.
- Não unificar todas as opções particulares das CLIs.
- Não implementar todos os harnesses de uma vez.
- Não modificar o visualizador se os eventos atuais forem suficientes.
- Não armazenar ou gerenciar credenciais no SSSF.

O primeiro incremento suporta Pi, Claude Code e Codex. Kiro, Antigravity e
OpenCode entram depois, um por vez, reutilizando o mesmo contrato.

## Configuração esperada

Preservar o formato atual e apenas ampliar os valores aceitos:

```yaml
agents:
  - name: planner
    coding_agent: claude_code
    model: opus
    thinking: xhigh

  - name: builder
    coding_agent: codex
    model: gpt-5.6-sol
    thinking: high

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-sol
    thinking: high
    writes: []
```

`thinking` será traduzido para o parâmetro de effort de cada CLI.

## Design mínimo

### Contrato do adapter

Criar uma interface pequena:

```python
class HarnessAdapter(Protocol):
    def validate(self, agent: AgentConfig) -> list[str]: ...
    def run(
        self,
        request: HarnessRequest,
        on_event: Callable[[dict], None],
        on_spawn: Callable[[int], None],
        on_exit: Callable[[int], None],
    ) -> HarnessResult: ...
```

Tipos comuns:

```python
class HarnessRequest(BaseModel):
    prompt: str
    system_prompt: str
    model: str
    thinking: str
    session_id: str | None = None
    cwd: str
    raw_output_path: str
    tools: list[str] | None = None
    harness_engineering: list[str] = Field(default_factory=list)

class HarnessResult(BaseModel):
    text: str
    returncode: int
    session_id: str | None = None
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    context_tokens: int = 0
    context_window: int = 0
```

Não adicionar capability negotiation, sistema de plugins ou hierarquia de
classes neste momento. Quando um adapter não suportar uma opção solicitada,
`validate()` deve retornar um erro objetivo.

### Registry

Criar um registry simples em `adw_modules/harnesses.py`:

```python
ADAPTERS = {
    "pi": PiAdapter(),
    "claude_code": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}
```

`agents.py` resolve o adapter uma vez e usa somente o contrato comum. Não deve
conter `if` específico para cada CLI.

## Aplicação de SOLID

### Single Responsibility

- `agents.py`: prepara e valida a chamada do SSSF.
- `harnesses.py`: seleciona o adapter.
- Cada adapter: monta o comando, executa a CLI e interpreta sua saída.
- `permissions.py`: continua responsável pelos limites de escrita.

### Liskov Substitution

Todos os adapters devem:

- receber o mesmo `HarnessRequest`;
- retornar `HarnessResult` válido;
- emitir eventos durante a execução;
- retornar um ID utilizável nos retries, quando houver sessão;
- transformar falha da CLI em erro da fase;
- não executar gates, retries ou commits.

Se uma CLI não cumprir esse contrato mínimo, ela não deve ser adicionada até que
exista uma forma confiável de adaptação.

## Mudanças necessárias

### 1. Caracterizar o comportamento atual

Adicionar testes focados em:

- seleção e validação do Pi;
- criação e reutilização da sessão;
- parsing da resposta final;
- propagação de eventos e erros;
- retry usando a mesma sessão.

Esses testes protegem a refatoração sem ampliar comportamento.

### 2. Extrair o Pi para o contrato comum

- Generalizar `PiRequest` e `PiResult` para `HarnessRequest` e `HarnessResult`.
- Encapsular o comportamento existente em `PiAdapter`.
- Preservar a regra atual do Pi: `returncode` diferente de zero só causa erro
  quando nenhuma resposta utilizável foi produzida.
- Criar o registry.
- Substituir a chamada direta a `agent_pi.run()` em `agents.py`.
- Confirmar que os ADWs continuam iguais.

### 3. Implementar Claude Code

Criar `ClaudeCodeAdapter` em `agent_claudecode.py` usando:

- `claude -p` para execução não interativa;
- `--model` para o modelo;
- `--effort` para `thinking`;
- `--output-format stream-json` para eventos;
- `--resume` para correções na mesma sessão;
- `--json-schema` quando possível.

O adapter deve usar o login já mantido pelo Claude Code. O SSSF não lê nem
persiste a credencial.

### 4. Implementar Codex

Criar `agent_codex.py` usando:

- `codex exec --json`;
- `--model` para o modelo;
- `model_reasoning_effort` para `thinking`;
- `--output-schema` quando possível;
- `thread_id` para continuar com `codex exec resume`.

O adapter deve usar a autenticação já mantida pelo Codex CLI.

### 5. Validar um fluxo misto

Executar um ADW com:

```text
planner  -> Claude Code
builder  -> Codex
test     -> Python
reviewer -> Codex
```

Confirmar:

- ordem das fases inalterada;
- modelo e effort corretos;
- retries preservam a sessão;
- envelopes e gates continuam funcionando;
- eventos aparecem no SQLite;
- permissões continuam sendo aplicadas.

## Sessões

Manter `agent_map.json`, acrescentando apenas o necessário:

```json
{
  "planner": {
    "session_id": "...",
    "model": "opus",
    "coding_agent": "claude_code"
  }
}
```

Uma sessão só pode ser reutilizada quando `coding_agent` e `model` forem os
mesmos. A validação atual, que considera apenas o modelo, deve incluir também o
harness.

Após cada chamada, `agents.py` deve adotar o `session_id` retornado no
`HarnessResult`. Esse valor passa a ser usado nos retries e salvo no
`agent_map.json`. Claude Code precisa de UUID válido; Codex fornece o
`thread_id` na primeira execução. O ID previamente gerado pelo SSSF não deve
substituir o ID real retornado pelo harness.

## Eventos e métricas

Cada adapter traduz somente os eventos necessários para o formato que o tracer
já consome:

- resposta final;
- início e fim de ferramenta;
- sessão;
- tokens e custo, quando fornecidos;
- erro.

O stream original continua em `raw_output.jsonl`. Métricas ausentes não devem
impedir a execução.

## Ferramentas e permissões

No primeiro incremento:

- manter `tools` como está para Pi;
- criar mapeamento direto e pequeno para Claude Code;
- usar sandbox nativo do Codex quando aplicável;
- manter `permissions.enforce()` como verificação adicional.

`harness_engineering` permanece exclusivo do Pi no MVP. Se estiver preenchido
para Claude Code ou Codex, a validação deve falhar com mensagem objetiva, sem
ignorar a configuração nem tentar converter extensões.

Não criar agora uma linguagem universal de permissões. Isso só será necessário
se a duplicação entre três adapters se tornar concreta.

## Testes

Usar CLIs falsas e fixtures JSONL; testes automatizados não devem consumir
modelos reais.

Testes mínimos por adapter:

- comando inicial contém modelo e effort;
- continuação usa o ID correto;
- resposta final é extraída;
- eventos de ferramenta são encaminhados;
- Claude Code e Codex falham com exit code diferente de zero;
- Pi preserva a regra atual para exit code diferente de zero com resposta útil;
- stdout e stderr não bloqueiam o processo.

Adicionar um smoke test real e read-only por harness, executado apenas
manualmente.

## Arquivos previstos

| Arquivo | Mudança |
|---|---|
| `data_types.py` | Tipos comuns e novos valores de `coding_agent` |
| `harnesses.py` | Interface e registry |
| `agent_pi.py` | Adapter do comportamento atual |
| `agent_claudecode.py` | Adapter do Claude Code |
| `agent_codex.py` | Adapter do Codex |
| `agents.py` | Resolução e chamada pelo contrato comum |
| `sssf.config.yaml` | Exemplo de configuração mista |
| testes | Contratos e fixtures dos três adapters |
| documentação | Valores suportados e limitações |

## Adapters posteriores

Adicionar somente após Pi, Claude Code e Codex estarem estáveis:

1. Antigravity, pois possui headless, JSON, modelo, effort e resume.
2. OpenCode, pois possui execução não interativa, JSON, modelo e sessão.
3. Kiro, considerando que seu modo headless atualmente exige API key.

Cada inclusão deve ser uma mudança independente, com seus próprios testes. Não
alterar o contrato comum para acomodar uma particularidade que possa permanecer
isolada dentro do adapter.

## Critérios de aceite

- Os ADWs não conhecem adapters concretos.
- Pi mantém o comportamento atual.
- Claude Code e Codex podem ser selecionados por agente.
- Modelo e `thinking` chegam corretamente ao harness.
- Retry continua a sessão correta.
- Gates, permissões e aceitação permanecem independentes do harness.
- Nenhuma credencial é armazenada pelo SSSF.
- A suíte usa fixtures, sem chamadas pagas.

## Fora do escopo inicial

- Implementar Kiro, Antigravity e OpenCode junto com o MVP.
- Criar SDK próprio de agentes.
- Padronizar plugins, MCPs ou subagentes entre harnesses.
- Migrar o schema do banco sem necessidade.
- Alterar os ADWs ou o visualizador por preferência arquitetural.
- Refatorar permissões, quality checks ou commits sem relação direta com os
  adapters.

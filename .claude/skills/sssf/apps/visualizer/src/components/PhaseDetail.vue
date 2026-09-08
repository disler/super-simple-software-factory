<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type {
  AgentEndPayload,
  Envelope,
  EventRow,
  GateCheck,
  GateResult,
  Phase,
  ToolCallPayload,
} from '../lib/types'
import {
  Activity,
  AlignLeft,
  Brain,
  Fingerprint,
  Inbox,
  MessagesSquare,
  Package,
  Receipt,
  ShieldCheck,
  SlidersHorizontal,
  SquareTerminal,
} from 'lucide-vue-next'
import { fmtClock, payloadOk, ts } from '../lib/format'
import { serverNow } from '../lib/clock'
import { highlightJson, highlightJsonText } from '../lib/highlight'
import { eventLabel, parseAgentStart, parseToolCall } from '../lib/events'
import { modelIcon, modelName } from '../lib/models'
import { fetchPrompts, type PromptsResponse } from '../lib/api'
import { renderMarkdown } from '../lib/markdown'
import StatusChip from './StatusChip.vue'
import StatChip from './StatChip.vue'
import DetailSection from './DetailSection.vue'

const props = defineProps<{
  phase: Phase
  events: EventRow[]
  envelopes: Envelope[]
  gates: GateResult[]
}>()

defineEmits<{ close: [] }>()

const phaseEvents = computed(() =>
  props.events.filter((e) => e.phase_id === props.phase.phase_id).sort((a, b) => a.rowid - b.rowid),
)

const phaseGates = computed(() =>
  props.gates
    .filter((g) => g.phase_id === props.phase.phase_id)
    .sort((a, b) => (a.attempt ?? 0) - (b.attempt ?? 0) || a.id - b.id),
)

const phaseOutputs = computed(() =>
  props.envelopes
    .filter((e) => e.phase_id === props.phase.phase_id)
    .sort((a, b) => (a.attempt ?? 0) - (b.attempt ?? 0)),
)

// The agent's configuration, carried on its phase's `agent_start` event.
// Older rows carry only model/thinking — rows render what was recorded.
const agentConfig = computed(() => {
  if (props.phase.kind !== 'agent') return null
  const start = phaseEvents.value.find((e) => e.type === 'agent_start')
  return start ? parseAgentStart(start) : null
})

interface UsageRow {
  label: string
  tokens: number
  cost: number
  /** Total gets a rule above it; reasoning is indented under output. */
  kind?: 'total' | 'nested'
  title?: string
}

/**
 * What this phase's agent run spent, off its `agent_end` event.
 *
 * Returns null for non-agent phases and for a run still in flight. Older runs
 * recorded only a lump `cost`, so the breakdown is optional and rows are built
 * from whatever was written.
 */
const phaseUsage = computed<{ rows: UsageRow[]; partial: boolean } | null>(() => {
  if (props.phase.kind !== 'agent') return null
  const end = phaseEvents.value.find((e) => e.type === 'agent_end')
  if (!end) return null
  let payload: AgentEndPayload = {}
  try {
    payload = JSON.parse(end.payload_json ?? '{}') as AgentEndPayload
  } catch {
    // A malformed payload is a missing panel, never a broken detail view.
  }
  const u = payload.usage
  if (!u) {
    // Pre-breakdown run: the event's own token count and the lump cost still hold.
    return {
      partial: true,
      rows: [{ label: 'total', tokens: end.tokens ?? 0, cost: payload.cost ?? 0, kind: 'total' }],
    }
  }
  const rows: UsageRow[] = [
    { label: 'input', tokens: u.input_tokens, cost: u.input_cost },
    { label: 'output', tokens: u.output_tokens, cost: u.output_cost },
  ]
  if (u.reasoning_tokens) {
    // Thinking bills at the output rate, so its share of the output cost is
    // exact arithmetic — but it is already INSIDE the output row above.
    const share = u.output_tokens ? (u.output_cost * u.reasoning_tokens) / u.output_tokens : 0
    rows.push({
      label: 'thinking',
      tokens: u.reasoning_tokens,
      cost: share,
      kind: 'nested',
      title: 'Thinking tokens — part of output above, billed at the output rate. Not added to the total.',
    })
  }
  rows.push(
    { label: 'cache read', tokens: u.cache_read_tokens, cost: u.cache_read_cost },
    { label: 'cache write', tokens: u.cache_write_tokens, cost: u.cache_write_cost },
    { label: 'total', tokens: u.total_tokens, cost: u.total_cost, kind: 'total' },
  )
  return { rows, partial: false }
})

const NUM = new Intl.NumberFormat('en-US')

/** Per-component costs run to fractions of a cent; four places keeps them real. */
function money(n: number): string {
  if (!n) return '$0'
  return n < 0.0001 ? '<$0.0001' : `$${n.toFixed(4)}`
}

// The engineer's incoming ask, logged by every ADW's request phase as a
// `log` event with an `input` payload — surfaced as its own section.
const requestText = computed(() => {
  if (props.phase.kind !== 'engineer') return null
  for (const e of phaseEvents.value) {
    if (e.type !== 'log' || !e.payload_json) continue
    try {
      const p: unknown = JSON.parse(e.payload_json)
      if (p && typeof p === 'object' && 'input' in p) {
        const input = (p as { input?: unknown }).input
        if (typeof input === 'string' && input.trim()) return input
      }
    } catch {
      /* not JSON — skip */
    }
  }
  return null
})

const phaseDurationMs = computed(() => {
  const start = ts(props.phase.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = props.phase.status === 'running' ? serverNow() : ts(props.phase.ended_at)
  return Number.isFinite(end) ? end - start : NaN
})

function violations(g: GateResult): string[] {
  try {
    const v: unknown = JSON.parse(g.violations_json ?? '[]')
    if (Array.isArray(v)) return v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x)))
  } catch {
    /* keep raw below */
  }
  return g.violations_json ? [g.violations_json] : []
}

// New-tracer gate rows carry per-item evidence in checks_json. null means no
// evidence was recorded (legacy row → plain non-expandable line); "[]" means
// the gate ran and inspected nothing — a real, different answer.
function gateChecks(g: GateResult): GateCheck[] | null {
  const raw = g.checks_json
  if (raw == null) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return null
    return parsed
      .filter((c): c is Record<string, unknown> => c !== null && typeof c === 'object')
      .map((c) => ({
        item: typeof c.item === 'string' ? c.item : '',
        ok: c.ok === true,
        note: typeof c.note === 'string' ? c.note : '',
      }))
  } catch {
    return null
  }
}

/** Collapsed-row count label: mixed gates surface how many items failed. */
function checksLabel(checks: GateCheck[]): string {
  const failed = checks.filter((c) => !c.ok).length
  return failed > 0 ? `${failed} of ${checks.length} failed` : String(checks.length)
}

const openGates = reactive(new Set<number>())

function toggleGate(id: number) {
  if (openGates.has(id)) openGates.delete(id)
  else openGates.add(id)
}

function eventDurationMs(e: EventRow): number {
  const a = ts(e.started_at)
  const b = ts(e.ended_at)
  if (Number.isFinite(a) && Number.isFinite(b)) return b - a
  // tool_call rows on older tracers have no ended_at — the payload's
  // duration_ms (when the coding agent reported one) is the source of truth.
  if (e.type === 'tool_call') {
    const call = parseToolCall(e)
    if (call?.duration_ms != null) return call.duration_ms
  }
  return NaN
}

const expanded = reactive(new Set<string>())

function toggle(e: EventRow) {
  if (expanded.has(e.event_id)) expanded.delete(e.event_id)
  else expanded.add(e.event_id)
}

/** Rich tool_call payload for the expanded panel, when the event has one. */
function richCall(e: EventRow): ToolCallPayload | null {
  return e.type === 'tool_call' ? parseToolCall(e) : null
}

// Safe for v-html: highlightJsonText/highlightJson escape all input.
function argsHtml(call: ToolCallPayload | null): string {
  return highlightJsonText(JSON.stringify(call?.args ?? {}, null, 2))
}

// ── Collapsible sections ─────────────────────────────────────────────────────
// Every left-column section can be shown/hidden. The watch keys off phase_id
// (a stable string), so the 500ms poll replacing the phase object never resets
// a user's open/closed choices — only selecting a different phase does.

// Every section starts closed — the engineer opens exactly what they need.
const DEFAULT_OPEN_SECTIONS: string[] = []
const openSections = reactive(new Set<string>(DEFAULT_OPEN_SECTIONS))

watch(
  () => props.phase.phase_id,
  () => {
    openSections.clear()
    for (const s of DEFAULT_OPEN_SECTIONS) openSections.add(s)
    openGates.clear()
  },
)

function toggleSection(id: string) {
  if (openSections.has(id)) openSections.delete(id)
  else openSections.add(id)
}

const typeClass: Record<string, string> = {
  gate_fail: 't-red',
  error: 't-red',
  gate_pass: 't-green',
  tool_call: 't-cyan',
  handoff: 't-violet',
  agent_start: 't-purple',
  agent_end: 't-green',
}

// ── Compiled prompts ─────────────────────────────────────────────────────────
// The exact system/user prompts sent to this phase's agent, fetched once per
// (adw_id, agent) and cached for the trace view's lifetime.

const prompts = ref<PromptsResponse | null>(null)
const promptsState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const promptCache = new Map<string, PromptsResponse>()
const openPanels = reactive(new Set<string>())
const rawView = reactive(new Set<string>())

// The trace view replaces the phase object on every poll tick, so the watch
// getter re-fires constantly — only react when the (adw_id, agent) key truly
// changes, or open panels would snap shut twice a second.
let lastPromptKey: string | null | undefined
watch(
  () => [props.phase.adw_id, props.phase.owner, props.phase.kind] as const,
  async ([adwId, owner, kind]) => {
    const key = kind === 'agent' && owner ? `${adwId}:${owner}` : null
    if (key === lastPromptKey) return
    lastPromptKey = key
    openPanels.clear()
    rawView.clear()
    if (key === null || !owner) {
      prompts.value = null
      promptsState.value = 'idle'
      return
    }
    const cached = promptCache.get(key)
    if (cached) {
      prompts.value = cached
      promptsState.value = 'ready'
      return
    }
    promptsState.value = 'loading'
    try {
      const result = await fetchPrompts(adwId, owner)
      promptCache.set(key, result)
      prompts.value = result
      promptsState.value = 'ready'
    } catch {
      promptsState.value = 'error'
    }
  },
  { immediate: true },
)

interface PromptPanel {
  id: string
  title: string
  text: string
  html: string
  lines: number
}

const promptPanels = computed<PromptPanel[]>(() => {
  const p = prompts.value
  if (!p) return []
  const panels: PromptPanel[] = []
  for (const [id, title, text] of [
    ['system', 'system prompt', p.system],
    ['user', 'user prompt', p.user],
  ] as const) {
    if (text == null) continue
    panels.push({ id, title, text, html: renderMarkdown(text), lines: text.split('\n').length })
  }
  return panels
})

function togglePanel(id: string) {
  if (openPanels.has(id)) openPanels.delete(id)
  else openPanels.add(id)
}
</script>

<template>
  <section class="detail">
    <header class="d-head">
      <div class="d-main">
        <span class="d-name">{{ phase.name }}</span>
        <StatusChip :status="phase.status ?? 'queued'" />
        <StatChip
          v-if="Number.isFinite(phaseDurationMs)"
          kind="runtime"
          :value="phaseDurationMs"
        />
      </div>
      <div class="d-tags">
        <span class="tag">
          <span class="tag-k">owner</span>
          <span class="tag-v">{{ phase.owner ?? '—' }}</span>
        </span>
        <span class="tag">
          <span class="tag-k">kind</span>
          <span class="tag-v">{{ phase.kind ?? '—' }}</span>
        </span>
        <span class="tag">
          <span class="tag-k">attempt</span>
          <span class="tag-v">{{ phase.attempt ?? 0 }}/{{ phase.retries ?? 0 }}</span>
        </span>
      </div>
      <button class="close" title="close" @click="$emit('close')">✕</button>
    </header>

    <div v-if="phase.error" class="error-bar d-error">{{ phase.error }}</div>

    <div class="d-grid">
      <div class="d-col">
        <DetailSection
          v-if="requestText"
          title="request"
          :icon="Inbox"
          :open="openSections.has('request')"
          @toggle="toggleSection('request')"
        >
          <p class="d-request">{{ requestText }}</p>
        </DetailSection>

        <DetailSection
          v-if="agentConfig"
          title="agent config"
          :icon="SlidersHorizontal"
          :open="openSections.has('config')"
          @toggle="toggleSection('config')"
        >
          <div class="cfg">
            <div v-if="agentConfig.coding_agent" class="cfg-row">
              <span class="cfg-k">coding agent</span>
              <span class="cfg-chip">
                <SquareTerminal class="cfg-icon" :size="18" :stroke-width="2" />
                {{ agentConfig.coding_agent }}
              </span>
            </div>
            <div v-if="agentConfig.model" class="cfg-row">
              <span class="cfg-k">model</span>
              <span class="cfg-chip" :title="agentConfig.model">
                <img v-if="modelIcon(agentConfig.model)" class="cfg-model-icon" :src="modelIcon(agentConfig.model)!" alt="" />
                {{ modelName(agentConfig.model) }}
              </span>
            </div>
            <div v-if="agentConfig.thinking" class="cfg-row">
              <span class="cfg-k">thinking</span>
              <span class="cfg-chip">
                <Brain class="cfg-icon" :size="18" :stroke-width="2" />
                {{ agentConfig.thinking }}
              </span>
            </div>
            <div v-if="agentConfig.tools !== undefined" class="cfg-row">
              <span class="cfg-k">tools</span>
              <span v-if="agentConfig.tools === null" class="cfg-v">all tools</span>
              <span v-else class="cfg-chips">
                <span v-for="t in agentConfig.tools" :key="t" class="cfg-chip">{{ t }}</span>
              </span>
            </div>
            <div v-if="agentConfig.harness_engineering !== undefined" class="cfg-row">
              <span class="cfg-k">harness</span>
              <span v-if="!agentConfig.harness_engineering?.length" class="cfg-v dim">none</span>
              <span v-else class="cfg-chips">
                <span v-for="h in agentConfig.harness_engineering" :key="h" class="cfg-chip">{{ h }}</span>
              </span>
            </div>
            <div v-if="agentConfig.purpose" class="cfg-row">
              <span class="cfg-k">purpose</span>
              <span class="cfg-v">{{ agentConfig.purpose }}</span>
            </div>
            <div v-if="agentConfig.session_id" class="cfg-row">
              <span class="cfg-k">session</span>
              <span class="cfg-chip">
                <Fingerprint class="cfg-icon" :size="18" :stroke-width="2" />
                {{ agentConfig.session_id }}
              </span>
            </div>
          </div>
        </DetailSection>

        <DetailSection
          v-if="phase.description"
          title="description"
          :icon="AlignLeft"
          :open="openSections.has('description')"
          @toggle="toggleSection('description')"
        >
          <p class="d-desc">{{ phase.description }}</p>
        </DetailSection>

        <DetailSection
          v-if="phase.kind === 'agent'"
          title="compiled prompts"
          :icon="MessagesSquare"
          :count="promptsState === 'ready' ? promptPanels.length : null"
          :open="openSections.has('prompts')"
          @toggle="toggleSection('prompts')"
        >
          <div v-if="promptsState === 'loading'" class="faint">loading prompts…</div>
          <div v-else-if="promptsState === 'error'" class="faint">prompts unavailable</div>
          <template v-else-if="promptsState === 'ready'">
            <div v-if="!promptPanels.length" class="faint">no compiled prompts recorded</div>
            <div v-for="panel in promptPanels" :key="panel.id" class="prompt-panel">
              <button class="prompt-head" @click="togglePanel(panel.id)">
                <span class="chev">{{ openPanels.has(panel.id) ? '▾' : '▸' }}</span>
                <span class="prompt-title">{{ panel.title }}</span>
                <span class="dim">{{ panel.lines }} lines</span>
              </button>
              <div v-if="openPanels.has(panel.id)" class="prompt-body">
                <div class="prompt-tools">
                  <button
                    :class="{ active: !rawView.has(panel.id) }"
                    @click="rawView.delete(panel.id)"
                  >
                    rendered
                  </button>
                  <button :class="{ active: rawView.has(panel.id) }" @click="rawView.add(panel.id)">
                    raw
                  </button>
                </div>
                <pre v-if="rawView.has(panel.id)" class="prompt-raw">{{ panel.text }}</pre>
                <!-- Safe: renderMarkdown escapes ALL input before emitting its own tags. -->
                <div v-else class="md" v-html="panel.html" />
              </div>
            </div>
          </template>
        </DetailSection>

        <DetailSection
          title="gates"
          :icon="ShieldCheck"
          :count="phaseGates.length"
          :open="openSections.has('gates')"
          @toggle="toggleSection('gates')"
        >
          <div v-if="!phaseGates.length" class="faint">no gate results</div>
          <div v-for="g in phaseGates" :key="g.id" class="gate" :class="g.passed ? 'pass' : 'fail'">
            <template v-if="gateChecks(g)">
              <button class="gate-line gate-toggle" @click="toggleGate(g.id)">
                <span class="chev">{{ openGates.has(g.id) ? '▾' : '▸' }}</span>
                <span class="gate-mark">{{ g.passed ? '✓' : '✗' }}</span>
                <span class="gate-name">{{ g.gate }}</span>
                <span class="tag" :class="{ 'tag-fail': !g.passed }">
                  <span class="tag-k">checks</span>
                  <span class="tag-v">{{ checksLabel(gateChecks(g) ?? []) }}</span>
                </span>
                <span class="tag">
                  <span class="tag-k">attempt</span>
                  <span class="tag-v">{{ g.attempt ?? 0 }}</span>
                </span>
                <span class="dim gate-time">{{ fmtClock(g.created_at) }}</span>
              </button>
              <div v-if="openGates.has(g.id)" class="gate-checks">
                <div v-if="!gateChecks(g)?.length" class="faint">
                  nothing to check — the gate inspected no items
                </div>
                <div
                  v-for="(c, i) in gateChecks(g)"
                  :key="i"
                  class="gate-check"
                  :class="c.ok ? 'pass' : 'fail'"
                >
                  <span class="check-mark">{{ c.ok ? '✓' : '✗' }}</span>
                  <span class="check-item">{{ c.item }}</span>
                  <!-- A failed tests_pass note is "exit N" + a command-output tail:
                       multi-line evidence gets a block, one-liners stay inline. -->
                  <span v-if="c.note && !c.note.includes('\n')" class="check-note dim">{{
                    c.note
                  }}</span>
                  <pre v-else-if="c.note" class="check-note-block">{{ c.note }}</pre>
                </div>
                <ul v-if="!g.passed && violations(g).length" class="violations">
                  <li v-for="(v, i) in violations(g)" :key="i">{{ v }}</li>
                </ul>
              </div>
            </template>
            <template v-else>
              <div class="gate-line">
                <span class="gate-mark">{{ g.passed ? '✓' : '✗' }}</span>
                <span class="gate-name">{{ g.gate }}</span>
                <span class="tag">
                  <span class="tag-k">attempt</span>
                  <span class="tag-v">{{ g.attempt ?? 0 }}</span>
                </span>
                <span class="dim gate-time">{{ fmtClock(g.created_at) }}</span>
              </div>
              <ul v-if="violations(g).length" class="violations">
                <li v-for="(v, i) in violations(g)" :key="i">{{ v }}</li>
              </ul>
            </template>
          </div>
        </DetailSection>

        <DetailSection
          v-if="phaseUsage"
          title="cost"
          :icon="Receipt"
          :open="openSections.has('cost')"
          @toggle="toggleSection('cost')"
        >
          <table class="usage">
            <thead>
              <tr>
                <th class="u-k"></th>
                <th class="u-n">tokens</th>
                <th class="u-c">cost</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in phaseUsage.rows"
                :key="r.label"
                :class="r.kind ? `u-${r.kind}` : undefined"
                :title="r.title"
              >
                <td class="u-k">{{ r.label }}</td>
                <td class="u-n">{{ NUM.format(r.tokens) }}</td>
                <td class="u-c">{{ money(r.cost) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="phaseUsage.partial" class="faint u-note">
            this run predates the per-component breakdown — only the total was recorded
          </p>
        </DetailSection>

        <DetailSection
          title="outputs"
          :icon="Package"
          :count="phaseOutputs.length"
          :open="openSections.has('outputs')"
          @toggle="toggleSection('outputs')"
        >
          <div v-if="!phaseOutputs.length" class="faint">no outputs</div>
          <div v-for="env in phaseOutputs" :key="env.envelope_id" class="output">
            <div class="output-line">
              <span class="output-type">{{ env.output_type }}</span>
              <span class="tag">
                <span class="tag-k">agent</span>
                <span class="tag-v">{{ env.agent ?? '—' }}</span>
              </span>
              <span class="tag">
                <span class="tag-k">attempt</span>
                <span class="tag-v">{{ env.attempt ?? 0 }}</span>
              </span>
              <span class="output-valid" :class="env.valid ? 'pass' : 'fail'">
                {{ env.valid ? 'valid' : 'invalid' }}
              </span>
            </div>
            <!-- Safe: highlightJson escapes ALL input before emitting its own spans. -->
            <pre v-html="highlightJson(env.payload_json)" />
          </div>
        </DetailSection>
      </div>

      <div class="d-col">
        <h3><Activity class="h3-icon" :size="19" :stroke-width="2" /> events ({{ phaseEvents.length }})</h3>
        <div v-if="!phaseEvents.length" class="faint">no events</div>
        <div v-for="e in phaseEvents" :key="e.event_id" class="event">
          <button class="event-row" :class="{ open: expanded.has(e.event_id) }" @click="toggle(e)">
            <span class="e-time dim">{{ fmtClock(e.started_at) }}</span>
            <span class="e-type" :class="typeClass[e.type ?? '']">{{ e.type }}</span>
            <span
              class="e-name"
              :class="{ 't-red': e.type === 'tool_call' && !payloadOk(e.payload_json) }"
              :title="eventLabel(e)"
              >{{ eventLabel(e) }}</span
            >
            <span class="e-extra">
              <StatChip
                v-if="Number.isFinite(eventDurationMs(e))"
                kind="runtime"
                compact
                :value="eventDurationMs(e)"
              />
              <StatChip v-if="e.tokens" kind="tokens" compact :value="e.tokens" />
            </span>
          </button>

          <div v-if="expanded.has(e.event_id)" class="payload-panel">
            <template v-if="richCall(e)">
              <div class="p-meta">
                <span class="p-tool">{{ richCall(e)?.tool }}</span>
                <span v-if="richCall(e)?.ok === false" class="t-red">failed</span>
                <StatChip
                  v-if="richCall(e)?.duration_ms != null"
                  kind="runtime"
                  compact
                  :value="richCall(e)?.duration_ms"
                />
              </div>
              <h4>args</h4>
              <pre class="p-pre" v-html="argsHtml(richCall(e))" />
              <template v-if="richCall(e)?.result_snippet">
                <h4>result</h4>
                <pre class="p-pre">{{ richCall(e)?.result_snippet }}</pre>
              </template>
            </template>
            <template v-else-if="e.type === 'tool_call' && e.payload_json">
              <div class="faint">no detail available — legacy event payload</div>
              <pre class="p-pre" v-html="highlightJson(e.payload_json)" />
            </template>
            <template v-else-if="e.payload_json">
              <h4>payload</h4>
              <pre class="p-pre" v-html="highlightJson(e.payload_json)" />
            </template>
            <div v-else class="faint">no payload</div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.detail {
  margin: 0 28px 28px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
}

.d-head {
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--panel-2);
  border-radius: 10px 10px 0 0;
}

.d-main {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.d-name {
  font-size: 20px;
  font-weight: 700;
}

.d-tags {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-left: auto;
}

.tag {
  display: inline-flex;
  align-items: baseline;
  gap: 7px;
  padding: 2px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--panel-3);
  font-size: 16px;
  white-space: nowrap;
}

.tag-k {
  color: var(--faint);
}

.tag-v {
  color: var(--text);
}

.close {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--dim);
  font-family: var(--mono);
  font-size: 16px;
  cursor: pointer;
  padding: 3px 10px;
}

.close:hover {
  color: var(--text);
  border-color: var(--dim);
}

.d-desc {
  margin: 0;
  color: var(--dim);
}

.d-request {
  margin: 0;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* ── agent config ── */

.cfg {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.cfg-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.cfg-k {
  flex: none;
  width: 118px;
  color: var(--faint);
}

.cfg-v {
  color: var(--text);
  min-width: 0;
  overflow-wrap: anywhere;
}

.cfg-model-icon {
  width: 17px;
  height: 17px;
  flex: none;
  object-fit: contain;
}

.cfg-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
}

.cfg-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 2px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--panel-2);
  font-family: var(--mono);
  font-size: 16px;
  overflow-wrap: anywhere;
}

.cfg-icon {
  flex: none;
  color: var(--faint);
}

.d-error {
  margin: 14px 18px 0;
}

.d-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 3fr);
  gap: 28px;
  padding: 16px 18px 20px;
}

@media (max-width: 1100px) {
  .d-grid {
    grid-template-columns: 1fr;
  }
}

h3 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 18px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border-soft);
  font-size: 16px;
  font-weight: 700;
  color: var(--dim);
  text-transform: lowercase;
  letter-spacing: 0.05em;
}

.h3-icon {
  flex: none;
  color: var(--faint);
}

h3:first-child {
  margin-top: 0;
}

/* ── compiled prompts ── */

.prompt-panel {
  margin-bottom: 10px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--panel-3);
  overflow: hidden;
}

.prompt-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  width: 100%;
  padding: 9px 14px;
  background: none;
  border: none;
  color: var(--text);
  font-size: 16px;
  cursor: pointer;
  text-align: left;
}

.prompt-head:hover {
  background: var(--panel-2);
}

.chev {
  color: var(--faint);
  flex: none;
}

.prompt-title {
  font-weight: 700;
}

.prompt-head .dim {
  margin-left: auto;
}

.prompt-body {
  padding: 12px 14px 14px;
  border-top: 1px solid var(--border-soft);
  max-height: 60vh;
  overflow: auto;
}

.prompt-tools {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.prompt-tools button {
  padding: 2px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  background: none;
  color: var(--dim);
  font-family: var(--mono);
  font-size: 16px;
  cursor: pointer;
}

.prompt-tools button.active {
  color: var(--text);
  border-color: var(--border);
  background: var(--panel-2);
}

.prompt-raw {
  border: none;
  padding: 0;
  background: transparent;
  max-height: none;
}

/* ── gates ── */

.gate {
  margin-bottom: 10px;
  padding: 10px 14px;
  border: 1px solid var(--border-soft);
  border-left-width: 3px;
  border-radius: 8px;
  background: var(--panel-3);
}

.gate.pass {
  border-left-color: var(--green);
}

.gate.fail {
  border-left-color: var(--red);
}

.gate-line {
  display: flex;
  gap: 12px;
  align-items: baseline;
  flex-wrap: wrap;
}

.gate-toggle {
  width: 100%;
  padding: 0;
  background: none;
  border: none;
  color: var(--text);
  font-size: 16px;
  cursor: pointer;
  text-align: left;
}

.gate-checks .check-item {
  font-family: var(--mono);
}

.gate-toggle .chev {
  color: var(--faint);
  flex: none;
}

.gate-checks {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}

.gate-check {
  display: flex;
  gap: 10px;
  align-items: baseline;
  flex-wrap: wrap;
  padding: 3px 0;
}

.gate-check.pass .check-mark {
  color: var(--green);
}

.gate-check.fail .check-mark {
  color: var(--red);
}

.check-item {
  overflow-wrap: anywhere;
  min-width: 0;
}

.check-note {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.check-note-block {
  flex-basis: 100%;
  margin: 4px 0 6px 26px;
  max-height: 30vh;
  overflow: auto;
}

.tag-fail {
  border-color: var(--red);
}

.tag-fail .tag-v {
  color: var(--red);
}

.gate.pass .gate-mark {
  color: var(--green);
}

.gate.fail .gate-mark {
  color: var(--red);
}

.gate-name {
  color: var(--text);
  font-weight: 700;
}

.gate-time {
  margin-left: auto;
}

.violations {
  margin: 8px 0 2px;
  padding-left: 24px;
  color: var(--red);
}

/* ── cost ── */

.usage {
  width: 100%;
  max-width: 420px;
  border-collapse: collapse;
  font-size: 16px;
}

.usage th {
  padding: 0 0 6px;
  font-size: 14px;
  font-weight: 400;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--faint);
  border-bottom: 1px solid var(--border-soft);
}

.usage td {
  padding: 5px 0;
}

.usage .u-k {
  text-align: left;
  color: var(--dim);
}

.usage .u-n,
.usage .u-c {
  text-align: right;
  font-family: var(--mono);
  color: var(--text);
}

.usage .u-c {
  color: var(--green);
}

/* Thinking sits inside output — indent it and mute it so the column still reads
   as a sum of the un-indented rows. */
.u-nested .u-k {
  padding-left: 18px;
  color: var(--faint);
}

.u-nested .u-n,
.u-nested .u-c {
  color: var(--faint);
}

.u-total td {
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
  font-weight: 700;
}

.u-total .u-k {
  color: var(--text);
}

.u-note {
  margin: 10px 0 0;
  font-size: 15px;
}

/* ── outputs ── */

.output {
  margin-bottom: 14px;
}

.output-line {
  display: flex;
  gap: 12px;
  align-items: baseline;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.output-type {
  color: var(--purple);
  font-weight: 700;
}

.output-valid.pass {
  color: var(--green);
}

.output-valid.fail {
  color: var(--red);
}

.output pre {
  max-height: 40vh;
  overflow: auto;
}

/* ── events ── */

.event {
  border-bottom: 1px solid var(--border-soft);
}

.event-row {
  display: flex;
  gap: 14px;
  align-items: baseline;
  width: 100%;
  padding: 7px 6px;
  background: none;
  border: none;
  border-radius: 6px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 16px;
  cursor: pointer;
  text-align: left;
}

.event-row:hover,
.event-row.open {
  background: var(--panel-2);
}

.e-time {
  flex: none;
  font-variant-numeric: tabular-nums;
}

.e-type {
  flex: none;
  width: 130px;
}

.e-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.e-extra {
  margin-left: auto;
  flex: none;
  display: inline-flex;
  gap: 14px;
}

.payload-panel {
  margin: 6px 0 14px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel-3);
}

.p-meta {
  display: flex;
  gap: 16px;
  align-items: baseline;
  margin-bottom: 10px;
}

.p-tool {
  color: var(--cyan);
  font-weight: 700;
  font-size: 17px;
}

.payload-panel h4 {
  margin: 14px 0 6px;
  font-size: 16px;
  font-weight: 700;
  color: var(--dim);
  text-transform: lowercase;
  letter-spacing: 0.06em;
}

.payload-panel h4:first-of-type {
  margin-top: 0;
}

/* args and result each get their own delineated block that scrolls in place —
   the full payload is always reachable, never visually cut off. */
.p-pre {
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--panel-3);
  max-height: 42vh;
  overflow: auto;
}

.t-red {
  color: var(--red);
}

.t-green {
  color: var(--green);
}

.t-cyan {
  color: var(--cyan);
}

.t-purple {
  color: var(--purple);
}

.t-violet {
  color: var(--violet);
}
</style>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, shallowRef, watch } from 'vue'
import type { EventRow, SessionSummary } from '../lib/types'
import { archiveSession, fetchEvents } from '../lib/api'
import { axisTicks, fmtDate, fmtOffset, ts } from '../lib/format'
import { agentColor, dotColor, eventLabel } from '../lib/events'
import { hrefFor } from '../lib/router'
import StatusChip from './StatusChip.vue'
import StatChip from './StatChip.vue'
import PhaseDots from './PhaseDots.vue'

const props = defineProps<{ session: SessionSummary; nowMs: number }>()
const emit = defineEmits<{ archived: [adwId: string] }>()

// The card is an <a>; the button lives inside it, so the click must not
// navigate. Told the parent optimistically — the poll would take up to half a
// second to drop the card, and a triage click should feel instant.
async function archive(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
  emit('archived', props.session.adw_id)
  try {
    await archiveSession(props.session.adw_id)
  } catch {
    emit('archived', '')   // signals the parent to re-sync from the server
  }
}

// Each card tails its own event stream: one full fetch on mount, then the
// same rowid-cursor poll as the trace view — but only while the run is live.
const events = shallowRef<EventRow[]>([])
let cursor = 0
let inflight = false
let timer: ReturnType<typeof setInterval> | undefined

function stopPolling() {
  clearInterval(timer)
  timer = undefined
}

async function pull() {
  if (inflight) return
  inflight = true
  try {
    const fresh: EventRow[] = []
    let page
    do {
      // Cursor pagination is inherently sequential: each request needs the previous cursor.
      // oxlint-disable-next-line no-await-in-loop
      page = await fetchEvents(props.session.adw_id, cursor, 1000)
      cursor = Math.max(cursor, page.cursor)
      fresh.push(...page.events)
    } while (page.has_more)
    if (fresh.length) events.value = [...events.value, ...fresh]
    if (props.session.status !== 'running') stopPolling()
  } catch {
    /* the list view surfaces api errors; a card just retries next poll */
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void pull()
  if (props.session.status === 'running') timer = setInterval(() => void pull(), 500)
})

onUnmounted(stopPolling)

watch(
  () => props.session.status,
  (status) => {
    if (status === 'running' && !timer) timer = setInterval(() => void pull(), 500)
    // On the transition out of running, one last pull drains the tail and stops the timer.
    else if (status !== 'running') void pull()
  },
)

const running = computed(() => props.session.status === 'running')

const range = computed(() => {
  const s = props.session
  let t0 = ts(s.started_at)
  if (!Number.isFinite(t0)) {
    t0 = Math.min(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t0)) t0 = props.nowMs
  let t1 = running.value ? props.nowMs : ts(s.ended_at)
  if (!Number.isFinite(t1)) {
    t1 = Math.max(...events.value.map((e) => ts(e.started_at)).filter(Number.isFinite))
  }
  if (!Number.isFinite(t1)) t1 = t0 + 1000
  return { t0, span: Math.max(t1 - t0, 1000) }
})

const ticks = computed(() => axisTicks(range.value.span, 5))

interface TimelineDot {
  id: string
  xPct: number
  color: string
  title: string
  latest: boolean
}

interface TimelineRow {
  owner: string
  color: string
  title: string
  dots: TimelineDot[]
}

// Per-agent rows: events attribute to an agent through their phase's owner.
const rows = computed<TimelineRow[]>(() => {
  const owners: string[] = []
  const ownerByPhase = new Map<string, string>()
  for (const p of props.session.phases ?? []) {
    if (p.kind !== 'agent' || !p.owner) continue
    ownerByPhase.set(p.phase_id, p.owner)
    if (!owners.includes(p.owner)) owners.push(p.owner)
  }
  if (!owners.length) return []

  const { t0, span } = range.value
  const byOwner = new Map<string, TimelineDot[]>(owners.map((o) => [o, []]))
  let latest: TimelineDot | null = null
  let latestT = -Infinity

  for (const e of events.value) {
    const owner = e.phase_id ? ownerByPhase.get(e.phase_id) : undefined
    const color = dotColor(e.type)
    if (!owner || !color) continue
    const t = ts(e.started_at)
    if (!Number.isFinite(t)) continue
    const dot: TimelineDot = {
      id: e.event_id,
      xPct: Math.min(Math.max(((t - t0) / span) * 100, 0), 100),
      color,
      title: `${e.type} ${eventLabel(e)} at ${fmtOffset(t - t0)}`,
      latest: false,
    }
    byOwner.get(owner)?.push(dot)
    if (t >= latestT) {
      latestT = t
      latest = dot
    }
  }
  if (running.value && latest) latest.latest = true

  // /api/sessions embeds agents so the labels can use config colors with no
  // extra request; historical sessions return color null → fallback palette.
  return owners.map((owner, i) => {
    const info = (props.session.agents ?? []).find((a) => a.agent === owner)
    return {
      owner,
      color: agentColor(info?.color, null, i),
      title: info?.model ? `${owner} ${info.model}` : owner,
      dots: byOwner.get(owner) ?? [],
    }
  })
})

const durationMs = computed(() => {
  const s = props.session
  const start = ts(s.started_at)
  if (!Number.isFinite(start)) return NaN
  const end = running.value ? props.nowMs : ts(s.ended_at)
  return (Number.isFinite(end) ? end : props.nowMs) - start
})

// Cards are a fixed size, so the timeline region fits exactly MAX_VISIBLE_ROWS
// row slots. A roster that overflows spends one slot on the "+N more" line and
// shows MIN_VISIBLE_ROWS agents in the rest — never fewer than three, so a
// five-agent chain still reads as a chain rather than as a pair and a count.
const MAX_VISIBLE_ROWS = 4
const MIN_VISIBLE_ROWS = 3

const overflowing = computed(() => rows.value.length > MAX_VISIBLE_ROWS)

const visibleRows = computed(() =>
  overflowing.value ? rows.value.slice(0, MIN_VISIBLE_ROWS) : rows.value,
)

const hiddenRowCount = computed(() =>
  overflowing.value ? rows.value.length - MIN_VISIBLE_ROWS : 0,
)
</script>

<template>
  <a class="card" :class="session.status" :href="hrefFor(session.adw_id)">
    <button
      class="card-archive"
      type="button"
      title="Archive — remove this run from review"
      aria-label="Archive run"
      @click="archive"
    >
      ×
    </button>
    <span class="card-id">{{ session.adw_id }}</span>
    <span class="card-adw" :title="session.adw_name ?? ''">{{ session.adw_name ?? '—' }}</span>
    <span class="card-req" :title="session.request ?? ''">{{ session.request }}</span>

    <div v-if="rows.length" class="tl">
      <div class="tl-axis">
        <span class="tl-gutter" />
        <span class="tl-scale">
          <span
            v-for="(t, i) in ticks"
            :key="i"
            class="tl-tick"
            :class="{ edge: t.pct === 0 }"
            :style="{ left: `${t.pct}%` }"
            >{{ t.label }}</span
          >
        </span>
      </div>
      <div v-for="row in visibleRows" :key="row.owner" class="tl-row">
        <span class="tl-agent" :style="{ color: row.color }" :title="row.title">{{
          row.owner
        }}</span>
        <span class="tl-track">
          <span
            v-for="dot in row.dots"
            :key="dot.id"
            class="tl-dot"
            :class="{ latest: dot.latest }"
            :style="{ left: `${dot.xPct}%`, background: dot.color }"
            :title="dot.title"
          />
        </span>
      </div>
      <div v-if="hiddenRowCount" class="tl-more dim">+{{ hiddenRowCount }} more agents</div>
    </div>
    <div v-else class="tl tl-empty faint">no agent activity yet</div>

    <div class="card-foot">
      <span class="foot-status">
        <StatusChip :status="session.status ?? 'fail'" />
        <PhaseDots :phases="session.phases ?? []" />
      </span>
      <span class="dim">{{ fmtDate(session.started_at) }}</span>
    </div>
    <div class="card-stats">
      <StatChip kind="cost" :value="session.total_cost" />
      <StatChip v-if="session.total_credits" kind="credits" :value="session.total_credits" />
      <StatChip kind="runtime" :value="durationMs" />
      <StatChip kind="tokens" :value="session.total_tokens" />
    </div>
  </a>
</template>

<style scoped>
.card {
  /* Uniform size: the grid fixes the width, this fixes the height — content
     clamps and truncates rather than resizing the card. Grew by one 40px row
     slot when the timeline went from three to four. */
  height: 420px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 20px 22px;
  position: relative;          /* anchors the archive button */
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: var(--shadow);
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  transition:
    border-color 0.18s ease,
    box-shadow 0.18s ease,
    transform 0.18s ease;
}

.card-archive {
  /* Top-right of the card, out of the text flow so nothing reflows around it. */
  position: absolute;
  top: 10px;
  right: 12px;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--dim);
  font-family: inherit;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition:
    opacity 0.15s ease,
    background 0.15s ease,
    color 0.15s ease;
}

/* Hidden until the card is hovered — 50 cards should read as runs, not as a
   wall of close buttons. Focus reveals it too, so keyboards are not excluded. */
.card:hover .card-archive,
.card-archive:focus-visible {
  opacity: 1;
}

.card-archive:hover {
  background: color-mix(in srgb, var(--red) 16%, transparent);
  color: var(--red);
}

.card:hover {
  border-color: var(--blue);
  box-shadow: var(--shadow-hover);
  transform: translateY(-1px);
}

.card.running {
  border-color: var(--blue);
}

.card.fail {
  border-color: var(--red);
}

/* Text rows must never absorb flex shrink — the fixed-height card squeezes
   overflow into .tl (which clips), not into the text. */
.card-id {
  flex: none;
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--blue);
}

.card-adw {
  flex: none;
  font-family: var(--mono);
  font-size: 16px;
  color: var(--dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-req {
  flex: none;
  font-size: 16px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tl {
  display: flex;
  flex-direction: column;
  margin-top: 4px;
  /* Fixed region: axis (28 + 6) + four 40px row slots, roster size or not.
     Four slots is what lets three agents show alongside a "+N more" line. */
  height: 194px;
  flex: none;
  overflow: hidden;
}

.tl-more {
  display: flex;
  align-items: center;
  height: 40px;
  padding-left: 96px;
  font-size: 16px;
}

.tl-axis {
  display: flex;
  align-items: flex-end;
  height: 28px;
  margin-bottom: 6px;
}

.tl-gutter,
.tl-agent {
  flex: none;
  /* Wide enough for full agent names (planner, builder, documenter). */
  width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-right: 8px;
}

.tl-scale {
  position: relative;
  flex: 1;
  height: 100%;
  border-bottom: 1px solid var(--border);
}

.tl-tick {
  position: absolute;
  bottom: 4px;
  transform: translateX(-50%);
  font-family: var(--mono);
  font-size: 16px;
  color: var(--faint);
  white-space: nowrap;
}

.tl-tick.edge {
  transform: none;
}

.tl-row {
  display: flex;
  align-items: center;
  height: 40px;
}

.tl-agent {
  font-size: 16px;
  color: var(--dim);
}

.tl-track {
  position: relative;
  flex: 1;
  height: 100%;
  border-bottom: 1px solid var(--border-soft);
}

.tl-dot {
  position: absolute;
  top: 50%;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  transform: translate(-50%, -50%);
}

.tl-dot.latest {
  width: 13px;
  height: 13px;
  box-shadow: 0 0 10px currentColor;
  animation: pulse 1.4s ease-in-out infinite;
}

.tl-empty {
  align-items: center;
  justify-content: center;
  font-size: 16px;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: auto;
  font-size: 16px;
}

.foot-status {
  display: inline-flex;
  align-items: center;
  gap: 14px;
}

.card-stats {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>

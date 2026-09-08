<script setup lang="ts">
import { computed } from 'vue'
import { BookOpen, CircleDollarSign, Coins, PenLine, Sparkles, Timer } from 'lucide-vue-next'
import { fmtCost, fmtCredits, fmtDuration, fmtTokens } from '../lib/format'

const props = defineProps<{
  kind: 'cost' | 'credits' | 'tokens' | 'runtime' | 'read' | 'written'
  /** Raw value — dollars, credits, tokens, or runtime in milliseconds. */
  value: number | null | undefined
  /** Bare value, no pill chrome — for tight spots like waterfall blocks. */
  compact?: boolean
}>()

const ICONS = {
  cost: CircleDollarSign,
  credits: Sparkles,
  tokens: Coins,
  runtime: Timer,
  read: BookOpen,
  written: PenLine,
}

// Every chip explains itself on hover. The token numbers in particular are read
// wrong without one — the headline is billed volume, not distinct tokens.
const TITLES = {
  cost: 'Cost — dollars billed for this run, all agents combined.',
  credits: 'Credits billed for this run, all agents combined.',
  tokens:
    'Tokens exchanged (billed) — everything sent or generated, counted once per turn. ' +
    'Each turn re-sends the whole conversation, so this is far larger than the ' +
    'conversation itself: it is spend, not size. The gap between it and read + ' +
    'written is cached context re-read on later turns.',
  runtime: 'Duration — wall-clock from the first phase starting to the last one ending.',
  read:
    'Read — raw tokens the models took in: prompts, file contents and tool results, ' +
    'counted the first time they enter the context. Excludes cached re-reads of ' +
    'material already counted here.',
  written:
    'Written — tokens the models actually generated. Each one produced exactly ' +
    'once, so this is a true count of output.',
}

const text = computed(() => {
  if (props.kind === 'cost') return fmtCost(props.value)
  if (props.kind === 'credits') return fmtCredits(props.value)
  if (props.kind === 'runtime') return fmtDuration(props.value ?? NaN)
  return fmtTokens(props.value)
})
</script>

<template>
  <span class="stat" :class="{ compact }" :title="TITLES[kind]">
    <component :is="ICONS[kind]" class="stat-icon" :size="compact ? 17 : 19" :stroke-width="2" />
    <span class="stat-value">{{ text }}</span>
  </span>
</template>

<style scoped>
.stat {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 3px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--panel-2);
  font-size: 16px;
  white-space: nowrap;
}

.stat-icon {
  color: var(--faint);
  flex: none;
}

.stat-value {
  color: var(--text);
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}

.stat.compact {
  padding: 0;
  border: none;
  background: transparent;
}

.stat.compact .stat-value {
  color: var(--dim);
}
</style>

<script setup lang="ts">
import { useRoute, hrefFor, phaseCrumb } from './lib/router'
import SessionsList from './components/SessionsList.vue'
import SessionTrace from './components/SessionTrace.vue'

const route = useRoute()
</script>

<template>
  <div class="app">
    <header class="topbar">
      <nav class="crumbs">
        <!-- Inline copy of public/logo.svg's glyph (the favicon adds the
             background square around this same shape) — keep the two in sync. -->
        <span class="mark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path
              d="M19.4 7.5 L17.4 9.5 L19.4 11.5"
              stroke="#fff"
              stroke-width="1.6"
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-opacity="0.85"
              fill="none"
            />
            <path
              d="M24 7.5 L26 9.5 L24 11.5"
              stroke="#fff"
              stroke-width="1.6"
              stroke-linecap="round"
              stroke-linejoin="round"
              stroke-opacity="0.85"
              fill="none"
            />
            <rect x="20" y="12" width="3.2" height="5.5" fill="#fff" fill-opacity="0.9" />
            <path
              d="M5 20 L5 17.5 L8.5 14 L8.5 17.5 L12 14 L12 17.5 L15.5 14 L15.5 17.5 L27 17.5 L27 20 Z"
              fill="#fff"
              fill-opacity="0.9"
            />
            <rect x="5" y="20" width="22" height="8" rx="1" fill="#fff" fill-opacity="0.9" />
            <rect x="8" y="22.5" width="3" height="3" fill="#1b1e24" fill-opacity="0.4" />
            <rect x="13.5" y="22.5" width="3" height="3" fill="#1b1e24" fill-opacity="0.3" />
            <rect x="19" y="22.5" width="3" height="3" fill="#1b1e24" fill-opacity="0.2" />
          </svg>
        </span>
        <span class="brand">Super Simple Software Factory</span>
        <span class="sep">›</span>
        <a :href="hrefFor()" :class="{ current: !route.adwId }">sessions</a>
        <template v-if="route.adwId">
          <span class="sep">›</span>
          <a :href="hrefFor(route.adwId)" :class="{ current: !route.phaseId }">{{
            route.adwId
          }}</a>
        </template>
        <template v-if="route.adwId && route.phaseId">
          <span class="sep">›</span>
          <span class="current">{{ phaseCrumb ?? route.phaseId }}</span>
        </template>
      </nav>
      <span class="live-hint"><span class="live-dot" /> live</span>
    </header>
    <main>
      <SessionsList v-if="!route.adwId" />
      <SessionTrace v-else :key="route.adwId" :adw-id="route.adwId" :phase-id="route.phaseId" />
    </main>
  </div>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 15px 28px;
  background: color-mix(in srgb, var(--panel) 85%, transparent);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 17px;
  min-width: 0;
}

.mark {
  width: 26px;
  height: 26px;
  flex: none;
  border-radius: 6px;
  background: var(--blue);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.mark svg {
  width: 17px;
  height: 17px;
}

.brand {
  color: var(--text);
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.sep {
  color: var(--faint);
}

.crumbs a {
  color: var(--dim);
}

.crumbs a:hover {
  color: var(--text);
}

.crumbs .current {
  color: var(--text);
}

.live-hint {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--dim);
  font-size: 16px;
  white-space: nowrap;
}

.live-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px color-mix(in srgb, var(--green) 55%, transparent);
  animation: pulse 1.6s ease-in-out infinite;
}
</style>

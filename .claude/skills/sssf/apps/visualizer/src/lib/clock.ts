/**
 * Server-anchored clock.
 *
 * Every timestamp in the trace is written by the machine the ADW ran on, and the
 * visualizer is explicitly built to be pointed at a repo somewhere else — a WSL2
 * distro, a container, another host. Those clocks do not have to agree, and when
 * they do not, an elapsed time computed as `Date.now() - started_at` is wrong by
 * the whole skew.
 *
 * That is not theoretical: a WSL2 distro measured 3m09s ahead of its Windows
 * host made every RUNNING phase report a negative duration, which fmtDuration
 * renders as "—". The panel therefore showed no time at all while a phase was in
 * flight and the correct one the instant it closed, because a finished phase is
 * `ended_at - started_at` — both written by the same clock, so the skew cancels.
 *
 * The API echoes its own clock on every response (`x-sssf-now`), and the server
 * runs beside the db it reads, so its frame is the writer's frame. The offset is
 * kept here and applied by `serverNow()`.
 */

let offsetMs = 0

/** Record the API's clock from a response header. Ignores a missing/bad value. */
export function observeServerClock(header: string | null | undefined): void {
  if (!header) return
  const server = new Date(header).getTime()
  if (Number.isFinite(server)) offsetMs = server - Date.now()
}

/** `Date.now()` translated into the frame the trace's timestamps were written in. */
export function serverNow(): number {
  return Date.now() + offsetMs
}

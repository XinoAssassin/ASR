import { onBeforeUnmount, onMounted, ref } from 'vue'

/** A reactive `Date.now()` that ticks every `intervalMs` while the component is mounted. */
export function useNow(intervalMs = 1000) {
  const now = ref(Date.now())
  let timer: ReturnType<typeof setInterval> | null = null
  onMounted(() => {
    timer = setInterval(() => (now.value = Date.now()), intervalMs)
  })
  onBeforeUnmount(() => {
    if (timer) clearInterval(timer)
  })
  return now
}

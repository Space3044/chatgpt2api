<template>
  <button
    type="button"
    class="asr-mic-button"
    :class="{
      'asr-mic-button--recording': asr.state.value === 'recording',
      'asr-mic-button--busy': asr.state.value === 'transcribing',
    }"
    :disabled="disabled || asr.state.value === 'transcribing'"
    :aria-label="ariaLabel"
    :aria-pressed="asr.state.value === 'recording'"
    :title="ariaLabel"
    @click.stop.prevent="handleClick"
  >
    <Icon
      :icon="asr.state.value === 'transcribing' ? 'lucide:loader-circle' : 'lucide:mic'"
      class="h-3.5 w-3.5"
      :class="{ 'animate-spin': asr.state.value === 'transcribing' }"
    />
  </button>
</template>

<script setup lang="ts">
import { Icon } from '@iconify/vue'
import { computed, watch } from 'vue'
import { useAsrInput } from '@/composables/useAsrInput'

const props = defineProps<{
  disabled?: boolean
}>()

const emit = defineEmits<{
  transcript: [text: string]
  error: [message: string]
}>()

const asr = useAsrInput()

const ariaLabel = computed(() => {
  if (asr.state.value === 'recording') return '点击结束语音输入'
  if (asr.state.value === 'transcribing') return '正在识别语音'
  return '点击开始语音输入'
})

// Click to toggle: click once to start recording, click again to stop
// and transcribe. Desktop browsers use this; there is no hold-to-talk.
async function handleClick() {
  if (props.disabled) return
  if (asr.state.value === 'recording') {
    const text = await asr.stop()
    if (text) emit('transcript', text)
    return
  }
  if (asr.state.value === 'idle') {
    await asr.start()
  }
}

watch(
  () => asr.error.value,
  (message) => {
    if (message) emit('error', message)
  },
)
</script>

<style scoped>
.asr-mic-button {
  display: inline-flex;
  width: 1.75rem;
  height: 1.75rem;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border: 1px solid hsl(var(--border) / 0.72);
  border-radius: 999px;
  background: hsl(var(--secondary) / 0.78);
  color: hsl(var(--muted-foreground));
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}

.asr-mic-button:hover:not(:disabled),
.asr-mic-button:focus-visible {
  background: hsl(var(--secondary));
  color: hsl(var(--foreground));
}

.asr-mic-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.asr-mic-button--recording {
  border-color: rgb(220 38 38 / 0.55);
  background: rgb(254 242 242);
  color: rgb(220 38 38);
}

.asr-mic-button--busy {
  cursor: wait;
}
</style>

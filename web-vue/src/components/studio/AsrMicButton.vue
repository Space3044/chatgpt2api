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
    :title="ariaLabel"
    @pointerdown.prevent="handlePointerDown"
    @pointerup.prevent="handlePointerUp"
    @pointerleave="handlePointerUp"
    @pointercancel="handlePointerUp"
    @click.stop.prevent
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
  if (asr.state.value === 'recording') return '松开发送语音'
  if (asr.state.value === 'transcribing') return '正在识别语音'
  return '按住说话'
})

async function handlePointerDown() {
  if (props.disabled) return
  await asr.start()
}

async function handlePointerUp() {
  if (asr.state.value !== 'recording') return
  const text = await asr.stop()
  if (text) emit('transcript', text)
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

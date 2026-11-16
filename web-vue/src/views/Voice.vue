<template>
  <div class="voice-workspace">
    <aside class="voice-history" aria-label="语音历史">
      <div class="voice-history-head">
        <p class="voice-history-title">语音对话</p>
        <Button
          size="sm"
          variant="outline"
          :disabled="call.isBusy.value"
          @click="createConversation"
        >
          <Icon icon="lucide:plus" class="h-3.5 w-3.5" />
          新通话
        </Button>
      </div>
      <div class="voice-history-list">
        <button
          v-for="item in transcript.conversations.value"
          :key="item.id"
          type="button"
          class="voice-history-item"
          :class="{ 'voice-history-item--active': item.id === transcript.activeConversationId.value }"
          @click="transcript.selectConversation(item.id)"
        >
          <span class="voice-history-item-title">{{ item.title }}</span>
          <span class="voice-history-item-meta">
            {{ formatTime(item.updatedAt) }} · {{ item.messages.length }} 条
          </span>
        </button>
        <p v-if="!transcript.conversations.value.length" class="voice-history-empty">
          暂无语音对话
        </p>
      </div>
      <div v-if="transcript.conversations.value.length" class="voice-history-foot">
        <Button size="sm" variant="ghost" @click="confirmClearHistory">
          <Icon icon="lucide:trash-2" class="h-3.5 w-3.5" />
          清空历史
        </Button>
      </div>
    </aside>

    <main class="voice-main">
      <div class="voice-header">
        <div class="min-w-0">
          <div class="voice-header-name">{{ activeTitle }}</div>
          <div class="voice-header-subtitle">
            {{ statusText }}<span v-if="call.isLive.value"> · {{ formatDuration(call.callSeconds.value) }}</span>
          </div>
        </div>
        <div class="voice-header-actions">
          <Button
            v-if="call.phase.value === 'live' || call.phase.value === 'connecting'"
            size="sm"
            variant="outline"
            :disabled="call.phase.value === 'ending'"
            @click="hangup"
          >
            <Icon icon="lucide:phone-off" class="h-3.5 w-3.5" />
            挂断
          </Button>
          <Button
            v-else
            size="sm"
            variant="primary"
            :disabled="call.isBusy.value || call.configLoading.value || !call.config.value"
            @click="startCall"
          >
            <Icon icon="lucide:phone" class="h-3.5 w-3.5" />
            开始通话
          </Button>
        </div>
      </div>

      <div class="voice-controls">
        <label class="voice-control">
          <span>音色</span>
          <select
            v-model="call.selectedVoice.value"
            :disabled="!isIdle || !call.config.value"
            aria-label="音色"
          >
            <option
              v-for="voice in call.config.value?.voices || []"
              :key="voice.id"
              :value="voice.id"
            >
              {{ voice.name }}（{{ voice.description }}）
            </option>
          </select>
        </label>
        <label class="voice-control">
          <span>语言</span>
          <select
            v-model="call.selectedLanguage.value"
            :disabled="!isIdle || !call.config.value"
            aria-label="语言"
          >
            <option
              v-for="language in call.config.value?.languages || []"
              :key="language.id"
              :value="language.id"
            >
              {{ language.name_zh }}（{{ language.name_en }}）
            </option>
          </select>
        </label>
        <label class="voice-control">
          <span>麦克风</span>
          <select
            v-model="call.selectedMicId.value"
            :disabled="!isIdle"
            aria-label="麦克风"
          >
            <option value="">默认麦克风</option>
            <option
              v-for="device in call.micDevices.value"
              :key="device.deviceId"
              :value="device.deviceId"
            >
              {{ device.label }}
            </option>
          </select>
        </label>
        <div class="voice-meter" aria-label="麦克风音量">
          <span>麦克风</span>
          <div class="voice-meter-bar">
            <div class="voice-meter-fill" :style="{ width: `${Math.round(call.micLevel.value * 100)}%` }"></div>
          </div>
        </div>
        <div class="voice-meter" aria-label="对方音量">
          <span>对方</span>
          <div class="voice-meter-bar">
            <div class="voice-meter-fill" :style="{ width: `${Math.round(call.remoteLevel.value * 100)}%` }"></div>
          </div>
        </div>
      </div>

      <PageLoadingState
        v-if="call.configLoading.value"
        title="正在加载语音配置"
        description="正在读取音色与语言列表..."
      />
      <StateBlock
        v-else-if="call.configError.value"
        title="语音配置加载失败"
        :description="call.configError.value"
      >
        <Button size="sm" variant="primary" @click="reloadConfig">重试</Button>
      </StateBlock>
      <div v-else-if="call.phase.value === 'error'" class="voice-error">
        <StateBlock title="通话失败" :description="call.callError.value || '通话出现问题。'">
          <Button size="sm" variant="primary" @click="retryCall">重新开始</Button>
        </StateBlock>
      </div>
      <div v-else class="voice-content">
        <div ref="transcriptRef" class="voice-transcript">
          <StateBlock
            v-if="!activeConversation?.messages.length && isIdle"
            title="还没有对话内容"
            description="选择音色和语言，点击「开始通话」，对方的回复会实时显示为字幕。"
            plain
          />
          <div
            v-for="message in activeConversation?.messages || []"
            :key="message.id"
            class="voice-message"
            :class="`voice-message--${message.role}`"
          >
            <div class="voice-message-head">
              <span>{{ message.role === 'user' ? '我' : '对方' }}</span>
              <span>{{ formatTime(message.ts) }}</span>
            </div>
            <p class="voice-message-text">{{ message.content }}</p>
          </div>
          <div v-if="call.liveCaption.value.text && call.assistantState.value === 'speaking'" class="voice-message voice-message--live">
            <div class="voice-message-head">
              <span>{{ call.liveCaption.value.role === 'user' ? '我' : '对方' }}（实时字幕）</span>
              <span>{{ call.assistantState.value === 'speaking' ? '说话中' : '聆听中' }}</span>
            </div>
            <p class="voice-message-text">{{ call.liveCaption.value.text }}</p>
          </div>
        </div>

        <div v-if="call.remoteAudioBlocked.value" class="voice-audio-blocked">
          <span>浏览器阻止了自动播放，请点击播放对方声音。</span>
          <Button size="sm" variant="outline" @click="call.resumeRemoteAudio()">
            播放对方声音
          </Button>
        </div>

        <div class="voice-call-actions">
          <Button
            size="sm"
            variant="outline"
            :disabled="!call.isLive.value"
            :aria-pressed="call.isMuted.value"
            @click="call.toggleMute()"
          >
            <Icon :icon="call.isMuted.value ? 'lucide:mic-off' : 'lucide:mic'" class="h-3.5 w-3.5" />
            {{ call.isMuted.value ? '取消静音' : '静音' }}
          </Button>
          <Button
            size="sm"
            variant="outline"
            :disabled="!call.isLive.value || call.assistantState.value !== 'speaking'"
            @click="call.interruptAssistant()"
          >
            <Icon icon="lucide:octagon-x" class="h-3.5 w-3.5" />
            打断对方
          </Button>
        </div>

        <form class="voice-composer" @submit.prevent="sendText">
          <input
            v-model="composerText"
            type="text"
            class="voice-composer-input"
            placeholder="通话中输入文字发给对方..."
            :disabled="!call.isLive.value"
            aria-label="发送文字消息"
          />
          <Button
            size="sm"
            variant="primary"
            type="submit"
            :disabled="!call.isLive.value || !composerText.trim()"
          >
            发送
          </Button>
        </form>
      </div>
    </main>
    <audio ref="remoteAudioRef" autoplay playsinline class="hidden"></audio>
  </div>
</template>

<script setup lang="ts">
import { Icon } from '@iconify/vue'
import { Button } from 'nanocat-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import PageLoadingState from '@/components/ai/PageLoadingState.vue'
import StateBlock from '@/components/ai/StateBlock.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { useVoiceCallRuntime } from '@/views/voice/voiceCallRuntime'
import { useVoiceTranscriptRuntime } from '@/views/voice/voiceTranscriptRuntime'

defineOptions({ name: 'Voice' })

const toast = useToast()
const confirmDialog = useConfirmDialog()
const transcript = useVoiceTranscriptRuntime()
const call = useVoiceCallRuntime(transcript)

const remoteAudioRef = ref<HTMLAudioElement | null>(null)
const transcriptRef = ref<HTMLElement | null>(null)
const composerText = ref('')

const activeConversation = computed(() => transcript.activeConversation.value)
const isIdle = computed(() => call.phase.value === 'idle')
const activeTitle = computed(() => activeConversation.value?.title || '新语音对话')

const statusText = computed(() => {
  switch (call.phase.value) {
    case 'connecting':
      return '正在连接...'
    case 'live':
      return call.assistantState.value === 'speaking' ? '对方正在说话' : '正在聆听'
    case 'ending':
      return '正在挂断...'
    case 'error':
      return call.callError.value || '通话失败'
    default:
      return call.config.value ? '就绪' : '正在加载语音配置'
  }
})

function formatTime(value: number): string {
  const date = new Date(value)
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')
  return `${hours}:${minutes}`
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function scrollTranscriptToBottom() {
  void nextTick(() => {
    const element = transcriptRef.value
    if (element) element.scrollTop = element.scrollHeight
  })
}

watch(
  () => activeConversation.value?.messages.length || 0,
  scrollTranscriptToBottom,
)
watch(
  () => call.liveCaption.value.text,
  scrollTranscriptToBottom,
)

function createConversation() {
  if (!isIdle.value) return
  transcript.createConversation()
}

async function confirmClearHistory() {
  if (!transcript.conversations.value.length) return
  const ok = await confirmDialog.ask({
    title: '清空历史',
    message: '确定清空本地语音对话历史吗？正在通话的记录不会受影响。',
    confirmText: '清空',
    cancelText: '取消',
  })
  if (ok) transcript.clearHistory()
}

async function reloadConfig() {
  await call.loadConfig()
}

async function startCall() {
  await call.start()
  if (call.phase.value === 'error' && call.callError.value) {
    toast.error(call.callError.value)
  }
}

async function retryCall() {
  call.resetError()
  await startCall()
}

async function hangup() {
  await call.hangup()
}

function sendText() {
  const text = composerText.value.trim()
  if (!text) return
  const ok = call.sendText(text)
  if (ok) {
    composerText.value = ''
  } else {
    toast.error('文字发送失败，语音通道尚未建立')
  }
}

function handlePageExit() {
  call.releaseOnPageExit()
}

onMounted(() => {
  call.setRemoteAudioElement(remoteAudioRef.value)
  void call.loadConfig()
  void call.listMicDevices()
  window.addEventListener('beforeunload', handlePageExit)
  scrollTranscriptToBottom()
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', handlePageExit)
  call.dispose()
})
</script>

<style scoped>
.voice-workspace {
  display: grid;
  min-height: 0;
  flex: 1 1 0%;
  grid-template-columns: 16rem minmax(0, 1fr);
  gap: 0.75rem;
  overflow: hidden;
}

.voice-history {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid hsl(var(--border));
  border-radius: 1.25rem;
  background: hsl(var(--card) / 0.88);
}

.voice-history-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  border-bottom: 1px solid hsl(var(--border));
  padding: 0.75rem 0.875rem;
}

.voice-history-title {
  font-size: 0.875rem;
  font-weight: 700;
  color: hsl(var(--foreground));
}

.voice-history-list {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 0.5rem;
}

.voice-history-item {
  display: block;
  width: 100%;
  border: 1px solid transparent;
  border-radius: 0.75rem;
  padding: 0.5rem 0.625rem;
  text-align: left;
}

.voice-history-item:hover {
  background: hsl(var(--secondary) / 0.55);
}

.voice-history-item--active {
  border-color: hsl(var(--primary) / 0.28);
  background: hsl(var(--primary) / 0.08);
}

.voice-history-item-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.875rem;
  font-weight: 600;
  color: hsl(var(--foreground));
}

.voice-history-item-meta {
  display: block;
  margin-top: 0.125rem;
  font-size: 0.75rem;
  color: hsl(var(--muted-foreground));
}

.voice-history-empty {
  padding: 1rem 0.5rem;
  text-align: center;
  font-size: 0.875rem;
  color: hsl(var(--muted-foreground));
}

.voice-history-foot {
  flex: 0 0 auto;
  border-top: 1px solid hsl(var(--border));
  padding: 0.5rem;
}

.voice-main {
  position: relative;
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid hsl(var(--border));
  border-radius: 1.25rem;
  background: hsl(var(--card) / 0.88);
  box-shadow: 0 16px 44px -36px rgba(15, 23, 42, 0.45);
}

.voice-header {
  display: flex;
  min-height: 3.5rem;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  border-bottom: 1px solid hsl(var(--border));
  background: hsl(var(--card) / 0.84);
  padding: 0.625rem 0.875rem;
}

.voice-header-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.875rem;
  font-weight: 700;
  color: hsl(var(--foreground));
}

.voice-header-subtitle {
  margin-top: 0.125rem;
  font-size: 0.75rem;
  line-height: 1rem;
  color: hsl(var(--muted-foreground));
}

.voice-header-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 0.375rem;
}

.voice-controls {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  border-bottom: 1px solid hsl(var(--border));
  padding: 0.625rem 0.875rem;
}

.voice-control {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.875rem;
  color: hsl(var(--muted-foreground));
}

.voice-control select {
  max-width: 12rem;
  border: 1px solid hsl(var(--border));
  border-radius: 0.625rem;
  background: hsl(var(--background));
  padding: 0.375rem 0.5rem;
  font-size: 0.875rem;
  color: hsl(var(--foreground));
}

.voice-control select:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.voice-meter {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.875rem;
  color: hsl(var(--muted-foreground));
}

.voice-meter-bar {
  width: 5rem;
  height: 0.375rem;
  overflow: hidden;
  border-radius: 999px;
  background: hsl(var(--secondary));
}

.voice-meter-fill {
  height: 100%;
  border-radius: 999px;
  background: hsl(var(--primary));
  transition: width 120ms linear;
}

.voice-content {
  display: flex;
  min-height: 0;
  flex: 1 1 auto;
  flex-direction: column;
  overflow: hidden;
}

.voice-error {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 1rem;
}

.voice-transcript {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 1rem 0.875rem;
}

.voice-message {
  max-width: 42rem;
  margin-bottom: 0.75rem;
  border: 1px solid hsl(var(--border));
  border-radius: 1rem;
  background: hsl(var(--background));
  padding: 0.625rem 0.875rem;
}

.voice-message--user {
  margin-left: auto;
  border-color: hsl(var(--primary) / 0.28);
  background: hsl(var(--primary) / 0.07);
}

.voice-message--live {
  border-style: dashed;
  opacity: 0.9;
}

.voice-message-head {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.75rem;
  color: hsl(var(--muted-foreground));
}

.voice-message-text {
  margin-top: 0.25rem;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.875rem;
  line-height: 1.6;
  color: hsl(var(--foreground));
}

.voice-audio-blocked {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  border-top: 1px solid hsl(var(--border));
  background: hsl(var(--secondary) / 0.5);
  padding: 0.5rem 0.875rem;
  font-size: 0.875rem;
  color: hsl(var(--foreground));
}

.voice-call-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: 0.5rem;
  border-top: 1px solid hsl(var(--border));
  padding: 0.625rem 0.875rem;
}

.voice-composer {
  display: flex;
  flex: 0 0 auto;
  gap: 0.5rem;
  border-top: 1px solid hsl(var(--border));
  padding: 0.625rem 0.875rem;
}

.voice-composer-input {
  min-width: 0;
  flex: 1 1 auto;
  border: 1px solid hsl(var(--border));
  border-radius: 0.75rem;
  background: hsl(var(--background));
  padding: 0.5rem 0.75rem;
  font-size: 0.875rem;
  color: hsl(var(--foreground));
}

.voice-composer-input:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

@media (max-width: 1023px) {
  .voice-workspace {
    grid-template-columns: minmax(0, 1fr);
  }

  .voice-history {
    display: none;
  }
}
</style>

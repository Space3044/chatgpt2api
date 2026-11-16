import { computed, ref } from 'vue'
import { getJsonPreference, preferenceKeys, setJsonPreference } from '@/lib/preferences'
import type { VoiceCaptionRole } from './voiceDcEvents'

/**
 * Local voice conversation history.
 *
 * The backend owns durable subtitle persistence (`VoiceService.record_message`
 * — currently unreachable from the console, see the coordinator report);
 * until that route exists this runtime is the only place subtitles live, so
 * it keeps them in localStorage like Studio keeps its conversations.
 * Merging mirrors the reference `rememberLocalMessage` /
 * `rememberAssistantText`: a caption delta updates the last message of the
 * same role when it arrives within the merge window and the texts overlap
 * by prefix; otherwise it starts a new message.
 */

export type VoiceTranscriptRole = 'user' | 'assistant'

export interface VoiceTranscriptMessage {
  id: string
  role: VoiceTranscriptRole
  content: string
  ts: number
}

export interface VoiceConversation {
  id: string
  title: string
  voiceSessionId: string
  upstreamConversationId: string
  messages: VoiceTranscriptMessage[]
  createdAt: number
  updatedAt: number
}

const MAX_CONVERSATIONS = 50
const MAX_MESSAGES_PER_CONVERSATION = 400
const USER_MERGE_WINDOW_MS = 45_000
const ASSISTANT_MERGE_WINDOW_MS = 90_000
const TITLE_MAX_CHARS = 42

export function createVoiceId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `${prefix}-${crypto.randomUUID()}`
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function buildVoiceTitle(content: string): string {
  const title = content.trim().replace(/\s+/g, ' ')
  if (!title) return '新语音对话'
  return title.length > TITLE_MAX_CHARS
    ? `${title.slice(0, TITLE_MAX_CHARS)}…`
    : title
}

function textsOverlap(a: string, b: string): boolean {
  if (!a || !b) return false
  return a === b || b.startsWith(a) || a.startsWith(b)
}

function normalizeMessage(value: unknown): VoiceTranscriptMessage | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  const role = item.role === 'user' || item.role === 'assistant' ? item.role : null
  const content = typeof item.content === 'string' ? item.content : ''
  if (!role || !content.trim()) return null
  return {
    id: typeof item.id === 'string' && item.id ? item.id : createVoiceId('msg'),
    role,
    content,
    ts: typeof item.ts === 'number' && Number.isFinite(item.ts) ? item.ts : Date.now(),
  }
}

function normalizeConversation(value: unknown): VoiceConversation | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string' || !item.id) return null
  const messages = Array.isArray(item.messages)
    ? item.messages.map(normalizeMessage).filter((m): m is VoiceTranscriptMessage => Boolean(m))
    : []
  return {
    id: item.id,
    title: typeof item.title === 'string' && item.title.trim() ? item.title : '新语音对话',
    voiceSessionId: typeof item.voiceSessionId === 'string' ? item.voiceSessionId : '',
    upstreamConversationId: typeof item.upstreamConversationId === 'string' ? item.upstreamConversationId : '',
    messages: messages.slice(-MAX_MESSAGES_PER_CONVERSATION),
    createdAt: typeof item.createdAt === 'number' ? item.createdAt : Date.now(),
    updatedAt: typeof item.updatedAt === 'number' ? item.updatedAt : Date.now(),
  }
}

export function loadVoiceConversations(): VoiceConversation[] {
  const raw = getJsonPreference<unknown[]>(preferenceKeys.voiceConversations, [])
  if (!Array.isArray(raw)) return []
  return raw
    .map(normalizeConversation)
    .filter((item): item is VoiceConversation => Boolean(item))
    .slice(0, MAX_CONVERSATIONS)
}

export function useVoiceTranscriptRuntime() {
  const conversations = ref<VoiceConversation[]>(loadVoiceConversations())
  const activeConversationId = ref(
    getJsonPreference<string>(preferenceKeys.voiceActiveConversationId, ''),
  )

  const activeConversation = computed(
    () =>
      conversations.value.find((item) => item.id === activeConversationId.value)
      || conversations.value[0]
      || null,
  )

  function persist() {
    setJsonPreference(preferenceKeys.voiceConversations, conversations.value)
    setJsonPreference(preferenceKeys.voiceActiveConversationId, activeConversationId.value)
  }

  function touch(conversation: VoiceConversation) {
    conversation.updatedAt = Date.now()
    conversations.value.sort((a, b) => b.updatedAt - a.updatedAt)
    persist()
  }

  function createConversation(): VoiceConversation {
    const conversation: VoiceConversation = {
      id: createVoiceId('voice'),
      title: '新语音对话',
      voiceSessionId: '',
      upstreamConversationId: '',
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }
    conversations.value.unshift(conversation)
    activeConversationId.value = conversation.id
    persist()
    return conversation
  }

  function ensureConversation(): VoiceConversation {
    return activeConversation.value || createConversation()
  }

  function selectConversation(id: string) {
    if (!conversations.value.some((item) => item.id === id)) return
    activeConversationId.value = id
    persist()
  }

  function deleteConversation(id: string) {
    conversations.value = conversations.value.filter((item) => item.id !== id)
    if (activeConversationId.value === id) {
      activeConversationId.value = conversations.value[0]?.id || ''
    }
    persist()
  }

  function clearHistory() {
    conversations.value = []
    activeConversationId.value = ''
    persist()
  }

  function setVoiceSession(conversation: VoiceConversation, voiceSessionId: string) {
    conversation.voiceSessionId = voiceSessionId
    touch(conversation)
  }

  function setUpstreamConversationId(conversation: VoiceConversation, upstreamConversationId: string) {
    if (!upstreamConversationId || conversation.upstreamConversationId === upstreamConversationId) return
    conversation.upstreamConversationId = upstreamConversationId
    touch(conversation)
  }

  function applyTitle(conversation: VoiceConversation, title: string, isUpstream: boolean) {
    const clean = title.trim()
    if (!clean) return
    if (isUpstream) {
      conversation.title = buildVoiceTitle(clean)
      touch(conversation)
      return
    }
    // Temporary first-utterance title only replaces a placeholder title.
    if (conversation.title !== '新语音对话') return
    conversation.title = buildVoiceTitle(clean)
    touch(conversation)
  }

  /**
   * Merge one finalized caption text into the conversation. A caption with
   * a stable `messageId` updates the message carrying that id; otherwise
   * the last same-role message inside the merge window absorbs the delta
   * when the texts overlap by prefix (streaming STT grows/shrinks).
   */
  function applyCaption(
    conversation: VoiceConversation,
    role: VoiceCaptionRole,
    messageId: string,
    text: string,
  ): VoiceTranscriptMessage | null {
    const clean = text.trim()
    if (role !== 'user' && role !== 'assistant') return null
    if (!clean) return null
    const now = Date.now()
    const windowMs = role === 'user' ? USER_MERGE_WINDOW_MS : ASSISTANT_MERGE_WINDOW_MS

    if (messageId) {
      const byId = conversation.messages.find((item) => item.id === messageId)
      if (byId && byId.role === role) {
        byId.content = clean
        byId.ts = now
        touch(conversation)
        return byId
      }
    }

    const last = conversation.messages[conversation.messages.length - 1]
    if (
      last
      && last.role === role
      && now - last.ts < windowMs
      && textsOverlap(last.content, clean)
    ) {
      last.content = clean.length >= last.content.length ? clean : last.content
      last.ts = now
      touch(conversation)
      return last
    }

    const message: VoiceTranscriptMessage = {
      id: messageId || createVoiceId('msg'),
      role,
      content: clean,
      ts: now,
    }
    conversation.messages.push(message)
    if (conversation.messages.length > MAX_MESSAGES_PER_CONVERSATION) {
      conversation.messages.splice(
        0,
        conversation.messages.length - MAX_MESSAGES_PER_CONVERSATION,
      )
    }
    if (role === 'user') applyTitle(conversation, clean, false)
    touch(conversation)
    return message
  }

  return {
    conversations,
    activeConversationId,
    activeConversation,
    ensureConversation,
    createConversation,
    selectConversation,
    deleteConversation,
    clearHistory,
    setVoiceSession,
    setUpstreamConversationId,
    applyTitle,
    applyCaption,
    persist,
  }
}

export type VoiceTranscriptRuntime = ReturnType<typeof useVoiceTranscriptRuntime>

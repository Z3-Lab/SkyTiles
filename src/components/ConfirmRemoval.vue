<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

const props = defineProps<{
  title: string
  description: string
  confirmLabel: string
  confirmationText?: string
  busy: boolean
  error: string
}>()
const emit = defineEmits<{ cancel: []; confirm: [] }>()
const typed = ref('')
const dialog = ref<HTMLElement>()
const input = ref<HTMLInputElement>()
const cancelButton = ref<HTMLButtonElement>()
const ready = computed(
  () => !props.busy && (!props.confirmationText || typed.value === props.confirmationText),
)
let previousFocus: HTMLElement | null = null

function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    if (!props.busy) emit('cancel')
  }
  if (event.key !== 'Tab') return
  const controls = dialog.value?.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled)',
  )
  if (!controls?.length) {
    event.preventDefault()
    return
  }
  const first = controls[0]
  const last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}

onMounted(() => {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
  if (props.confirmationText) input.value?.focus()
  else cancelButton.value?.focus()
})
onUnmounted(() => {
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>

<template>
  <div class="dialog-backdrop confirmation-backdrop" @click.self="!busy && emit('cancel')">
    <section
      ref="dialog"
      class="dialog confirmation-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="removal-title"
      aria-describedby="removal-description"
      @keydown="keydown"
    >
      <header class="dialog-header">
        <h2 id="removal-title">{{ title }}</h2>
      </header>
      <div class="dialog-body">
        <p id="removal-description" class="removal-description">{{ description }}</p>
        <label v-if="confirmationText" class="field-label removal-input">
          输入“{{ confirmationText }}”以确认
          <input
            ref="input"
            v-model="typed"
            :disabled="busy"
            autocomplete="off"
            @keydown.enter.prevent="ready && emit('confirm')"
          />
        </label>
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      </div>
      <footer class="dialog-footer">
        <button ref="cancelButton" class="quiet" :disabled="busy" @click="emit('cancel')">
          取消
        </button>
        <button class="danger" :disabled="!ready" @click="emit('confirm')">
          {{ busy ? '正在移除…' : confirmLabel }}
        </button>
      </footer>
    </section>
  </div>
</template>

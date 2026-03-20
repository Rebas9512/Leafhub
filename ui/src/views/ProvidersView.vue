<template>
  <div>
    <div class="page-actions">
      <div>
        <h1>Providers</h1>
        <p class="page-subtitle">Manage API keys and provider connection settings.</p>
      </div>
      <button class="btn-primary" @click="openCreate">+ Add Provider</button>
    </div>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="loading" class="empty-state"><span class="spinner"></span></div>

    <div v-else-if="!items.length" class="empty-state">
      <div style="font-size:32px">🔌</div>
      <p>No providers yet. Add your first provider to get started.</p>
    </div>

    <div v-else class="grid">
      <div v-for="p in items" :key="p.id" class="card">
        <div class="card-header">
          <div style="flex:1">
            <div class="card-title">{{ p.label }}</div>
            <div class="card-meta">{{ p.provider_type }} · {{ p.api_format }}</div>
            <div class="card-meta mono" style="margin-top:4px">{{ p.base_url }}</div>
          </div>
          <span :class="authBadge(p.auth_mode)">{{ p.auth_mode }}</span>
        </div>

        <div class="card-meta" style="margin-bottom:8px">
          Default model: <strong>{{ p.default_model }}</strong>
        </div>

        <div v-if="p.auth_header" class="card-meta" style="margin-bottom:4px">
          Auth header: <span class="mono">{{ p.auth_header }}</span>
        </div>

        <div v-if="p.extra_headers && Object.keys(p.extra_headers).length"
             class="card-meta" style="margin-bottom:8px">
          Extra headers:
          <span v-for="(v, k) in p.extra_headers" :key="k" class="badge badge-neutral"
                style="margin-left:4px; font-size:10px">{{ k }}</span>
        </div>

        <div class="card-body">
          <button class="btn-secondary btn-sm" @click="openEdit(p)">Edit</button>
          <button class="btn-danger btn-sm" style="margin-left:auto" @click="doDelete(p)">Delete</button>
        </div>
      </div>
    </div>

    <!-- Create / Edit modal -->
    <div v-if="modal.open" class="modal-backdrop" @click.self="closeModal">
      <div class="modal">
        <h2>{{ modal.id ? 'Edit Provider' : 'Add Provider' }}</h2>

        <!-- Preset selector — only shown when creating -->
        <div v-if="!modal.id" class="field">
          <label class="field-label">Preset <small>(pre-fills fields below)</small></label>
          <select class="input select" :value="selectedPreset"
                  @change="applyPreset(PROVIDER_PRESETS.find(p => p.id === $event.target.value))">
            <option v-for="p in PROVIDER_PRESETS" :key="p.id" :value="p.id">
              {{ p.icon }} {{ p.label }}
            </option>
          </select>
        </div>

        <div class="field">
          <label class="field-label">Label</label>
          <input v-model="form.label" class="input" placeholder="My OpenAI Account" />
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
          <div class="field">
            <label class="field-label">Provider Type</label>
            <select v-model="form.provider_type" class="input select" :disabled="!!modal.id">
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
              <option value="ollama">ollama</option>
              <option value="custom">custom</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label">API Format</label>
            <select v-model="form.api_format" class="input select" :disabled="!!modal.id" @change="onFormatChange">
              <option value="openai-completions">openai-completions</option>
              <option value="anthropic-messages">anthropic-messages</option>
              <option value="ollama">ollama</option>
            </select>
          </div>
        </div>

        <div class="field">
          <label class="field-label">Base URL</label>
          <input v-model="form.base_url" class="input" placeholder="https://api.openai.com/v1" />
        </div>

        <div class="field">
          <label class="field-label">Default Model</label>
          <input v-model="form.default_model" class="input" placeholder="gpt-4o-mini" />
        </div>

        <div class="field">
          <label class="field-label">API Key
            <small v-if="modal.id">(leave blank to keep existing)</small>
            <small v-else-if="form.auth_mode !== 'none'">(required)</small>
            <small v-else>(not required for this auth mode)</small>
          </label>
          <input v-model="form.api_key" class="input" type="password"
                 :placeholder="modal.id ? '(unchanged)' : 'sk-...'"
                 :disabled="form.auth_mode === 'none'" />
        </div>

        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
          <div class="field">
            <label class="field-label">Auth Mode</label>
            <select v-model="form.auth_mode" class="input select">
              <option value="bearer">bearer</option>
              <option value="x-api-key">x-api-key</option>
              <option value="none">none (no auth)</option>
            </select>
          </div>
          <div class="field">
            <label class="field-label">Auth Header Override <small>(optional)</small></label>
            <input v-model="form.auth_header" class="input" placeholder="e.g. api-key"
                   :disabled="form.auth_mode === 'none'" />
          </div>
        </div>

        <div class="field">
          <label class="field-label">Extra Headers
            <small>(one per line: Key: Value)</small>
          </label>
          <textarea v-model="form.extra_headers_raw" class="input" rows="3"
                    placeholder="anthropic-version: 2023-06-01&#10;anthropic-beta: prompt-caching-2024-07-31"></textarea>
        </div>

        <div v-if="modal.probing" class="info-banner">
          Testing connectivity… this may take a few seconds.
        </div>
        <div v-if="modal.error" class="error-banner">{{ modal.error }}</div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="closeModal">Cancel</button>
          <button class="btn-primary" :disabled="modal.saving" @click="doSave">
            <span v-if="modal.saving" class="spinner"></span>
            <span v-else>{{ modal.id ? 'Save Changes' : 'Create & Test Connection' }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { providers } from '../api.js'
import { PROVIDER_PRESETS } from '../presets.js'

const items   = ref([])
const loading = ref(true)
const error   = ref('')

const modal          = reactive({ open: false, id: null, error: '', saving: false, probing: false })
const selectedPreset = ref('custom')
const form           = reactive({
  label: '', provider_type: 'custom', api_format: 'openai-completions',
  base_url: '', default_model: '', api_key: '',
  auth_mode: 'bearer', auth_header: '', extra_headers_raw: '',
})

// Infer default auth_mode when user manually changes api_format (not via preset)
const FORMAT_AUTH = {
  'openai-completions': 'bearer',
  'anthropic-messages': 'x-api-key',
  'ollama':             'none',
}

async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await providers.list()).data }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}

function authBadge(mode) {
  return {
    badge: true,
    'badge-blue':    mode === 'bearer',
    'badge-neutral': mode === 'x-api-key',
    'badge-warn':    mode === 'none',
  }
}

function headersToRaw(obj) {
  if (!obj || !Object.keys(obj).length) return ''
  return Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n')
}

function parseHeaders(raw) {
  const result = {}
  for (const line of raw.split('\n')) {
    const colon = line.indexOf(':')
    if (colon < 1) continue
    const key = line.slice(0, colon).trim()
    const val = line.slice(colon + 1).trim()
    if (key && val) result[key] = val
  }
  return result
}

function applyPreset(preset) {
  selectedPreset.value = preset.id
  // Don't overwrite a label the user already typed
  if (!form.label || form.label === prevPresetLabel.value) {
    form.label = preset.id === 'custom' ? '' : preset.label
    prevPresetLabel.value = form.label
  }
  form.provider_type      = preset.provider_type
  form.api_format         = preset.api_format
  form.base_url           = preset.base_url
  form.default_model      = preset.default_model
  form.auth_mode          = preset.auth_mode
  form.auth_header        = preset.auth_header
  form.extra_headers_raw  = headersToRaw(preset.extra_headers)
  // Never clear an api_key the user already typed
}

// Tracks the label last set by a preset so we know if user has customised it
const prevPresetLabel = ref('')

function onFormatChange() {
  // Only infer auth_mode if user manually changes the format (no preset active),
  // or if they're on the generic 'custom' preset
  if (selectedPreset.value === 'custom' || !selectedPreset.value) {
    form.auth_mode = FORMAT_AUTH[form.api_format] || 'bearer'
  }
}

function openCreate() {
  selectedPreset.value  = 'custom'
  prevPresetLabel.value = ''
  Object.assign(form, {
    label: '', provider_type: 'custom', api_format: 'openai-completions',
    base_url: '', default_model: '', api_key: '',
    auth_mode: 'bearer', auth_header: '', extra_headers_raw: '',
  })
  Object.assign(modal, { open: true, id: null, error: '', saving: false })
}

function openEdit(p) {
  Object.assign(form, {
    label: p.label, provider_type: p.provider_type,
    api_format: p.api_format, base_url: p.base_url,
    default_model: p.default_model, api_key: '',
    auth_mode: p.auth_mode || 'bearer',
    auth_header: p.auth_header || '',
    extra_headers_raw: headersToRaw(p.extra_headers),
  })
  Object.assign(modal, { open: true, id: p.id, error: '', saving: false })
}

function closeModal() { modal.open = false }

async function doSave() {
  modal.saving = true; modal.error = ''; modal.probing = false
  const extra_headers = parseHeaders(form.extra_headers_raw)
  const body = {
    label:         form.label,
    provider_type: form.provider_type,
    api_format:    form.api_format,
    base_url:      form.base_url,
    default_model: form.default_model,
    auth_mode:     form.auth_mode,
    auth_header:   form.auth_header || null,
    extra_headers,
    ...(form.api_key ? { api_key: form.api_key } : {}),
  }
  try {
    if (modal.id) {
      await providers.update(modal.id, body)
    } else {
      modal.probing = true
      await providers.create(body)
    }
    closeModal(); await load()
  } catch (e) { modal.error = e.message }
  finally { modal.saving = false; modal.probing = false }
}

async function doDelete(p) {
  if (!confirm(`Delete provider "${p.label}"? This cannot be undone.`)) return
  try { await providers.delete(p.id); await load() }
  catch (e) { error.value = e.message }
}

onMounted(load)
</script>

<style scoped>
.info-banner {
  background: var(--color-blue-50, #eff6ff);
  border: 1px solid var(--color-blue-200, #bfdbfe);
  color: var(--color-blue-700, #1d4ed8);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  margin-bottom: 8px;
}
</style>

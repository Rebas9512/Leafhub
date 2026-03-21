<template>
  <div>
    <div class="page-actions">
      <div>
        <h1>Projects</h1>
        <p class="page-subtitle">Manage project tokens and provider alias bindings.</p>
      </div>
      <button class="btn-primary" @click="openCreate">+ New Project</button>
    </div>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="deleteResult" class="success-banner" style="margin-bottom:12px">
      <strong>Project deleted.</strong>
      <ul v-if="deleteResult.length" style="margin:4px 0 0 16px; padding:0; font-size:12px">
        <li v-for="item in deleteResult" :key="item">{{ item }}</li>
      </ul>
    </div>
    <div v-if="loading" class="empty-state"><span class="spinner"></span></div>

    <div v-else-if="!items.length" class="empty-state">
      <div style="font-size:32px">📁</div>
      <p>No projects yet. Create a project to get a token for your application.</p>
    </div>

    <div v-else class="grid">
      <div v-for="p in items" :key="p.id" class="card">
        <div class="card-header">
          <div style="flex:1">
            <div class="card-title">
              {{ p.name }}
              <span v-if="duplicateNames.has(p.name)" class="badge badge-neutral"
                    style="font-size:10px; vertical-align:middle; margin-left:6px"
                    title="Multiple projects share this name — token prefix distinguishes them">
                duplicate
              </span>
            </div>
            <div class="card-meta mono"
                 :class="{ 'prefix-highlight': duplicateNames.has(p.name) }">
              token: {{ p.token_prefix }}…
            </div>
          </div>
          <span :class="p.is_active ? 'badge badge-ok' : 'badge badge-err'">
            {{ p.is_active ? 'active' : 'disabled' }}
          </span>
        </div>

        <!-- Linked directory -->
        <div v-if="p.path" class="linked-path">
          <span class="link-icon">⛓</span>
          <span class="mono" :title="p.path">{{ p.path }}</span>
        </div>

        <!-- Bindings -->
        <div v-if="p.bindings?.length" style="margin-bottom:12px">
          <div v-for="b in p.bindings" :key="b.id" class="binding-row">
            <span class="mono" style="font-size:12px; color:var(--primary)">"{{ b.alias }}"</span>
            <span style="color:var(--muted)">→</span>
            <span class="mono" style="font-size:12px">{{ provLabel(b.provider_id) }}</span>
            <span v-if="b.model_override" class="badge badge-neutral"
                  style="margin-left:auto">{{ b.model_override }}</span>
          </div>
        </div>
        <div v-else style="font-size:12px; color:var(--muted); margin-bottom:12px">
          No alias bindings
        </div>

        <div class="card-body">
          <button class="btn-secondary btn-sm" @click="openEdit(p)">Edit</button>
          <button class="btn-secondary btn-sm" @click="doRotate(p)">↺ Rotate Token</button>
          <button class="btn-secondary btn-sm" @click="openLink(p)" title="Link to a local project directory">
            {{ p.path ? '⛓ Relink' : '⛓ Link Dir' }}
          </button>
          <button class="btn-secondary btn-sm" @click="toggleActive(p)">
            {{ p.is_active ? 'Disable' : 'Enable' }}
          </button>
          <button class="btn-danger btn-sm" style="margin-left:auto" @click="doDelete(p)">Delete</button>
        </div>
      </div>
    </div>

    <!-- Create / Edit modal -->
    <div v-if="modal.open" class="modal-backdrop" @click.self="closeModal">
      <div class="modal">
        <h2>{{ modal.id ? 'Edit Project' : 'New Project' }}</h2>

        <div class="field">
          <label class="field-label">Project Name</label>
          <input v-model="form.name" class="input" placeholder="my-app" />
        </div>

        <div class="field">
          <label class="field-label">Alias Bindings
            <small>(alias → provider, with optional model override)</small>
          </label>
          <div v-for="(b, i) in form.bindings" :key="i" class="binding-editor">
            <input v-model="b.alias" class="input" style="flex:0 0 120px"
                   placeholder='alias, e.g. "chat"' />
            <select v-model="b.provider_id" class="input select" style="flex:1">
              <option value="">— select provider —</option>
              <option v-for="pv in providersList" :key="pv.id" :value="pv.id">{{ pv.label }}</option>
            </select>
            <input v-model="b.model_override" class="input" style="flex:0 0 140px"
                   placeholder="model override" />
            <button class="btn-icon" @click="removeBinding(i)" title="Remove">✕</button>
          </div>
          <button class="btn-link" style="margin-top:6px" @click="addBinding">+ Add binding</button>
        </div>

        <div v-if="!modal.id" class="field">
          <label class="field-label">Link Directory <small>(optional — auto-configures .leafhub)</small></label>
          <input v-model="form.path" class="input" placeholder="/absolute/path/to/project" />
          <p class="field-hint">
            If set, LeafHub writes a <code>.leafhub</code> file here so the project
            auto-detects its token on startup — no manual copy-paste needed.
          </p>
        </div>

        <div v-if="modal.error" class="error-banner">{{ modal.error }}</div>
        <div class="modal-actions">
          <button class="btn-secondary" @click="closeModal">Cancel</button>
          <button class="btn-primary" :disabled="modal.saving" @click="doSave">
            <span v-if="modal.saving" class="spinner"></span>
            <span v-else>{{ modal.id ? 'Save Changes' : 'Create Project' }}</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Link Directory modal -->
    <div v-if="linkModal.open" class="modal-backdrop" @click.self="closeLinkModal">
      <div class="modal">
        <h2>⛓ Link Directory</h2>
        <p style="color:var(--muted); font-size:13px; margin-bottom:16px">
          LeafHub will write a <code>.leafhub</code> file into the directory you specify.
          Any project running there will auto-detect its credentials on next startup.
          The current token will be rotated and written only to that file — it will
          <strong>not</strong> be shown here.
        </p>

        <div class="field">
          <label class="field-label">Project Directory (absolute path)</label>
          <input v-model="linkModal.path" class="input"
                 :placeholder="`/home/user/${linkModal.projectName}`"
                 @keyup.enter="doLink" />
        </div>

        <div v-if="linkModal.error" class="error-banner">{{ linkModal.error }}</div>
        <div v-if="linkModal.success" class="success-banner">{{ linkModal.success }}</div>

        <div class="modal-actions">
          <button class="btn-secondary" @click="closeLinkModal">{{ linkModal.success ? 'Close' : 'Cancel' }}</button>
          <button v-if="!linkModal.success" class="btn-primary" :disabled="linkModal.saving" @click="doLink">
            <span v-if="linkModal.saving" class="spinner"></span>
            <span v-else>Link &amp; Write .leafhub</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Token-once modal (shown after create or rotate without linked path) -->
    <div v-if="tokenModal.open" class="modal-backdrop">
      <div class="modal">
        <div style="text-align:center; margin-bottom:16px">
          <div style="font-size:36px">🔑</div>
          <h2 style="margin-top:8px">{{ tokenModal.rotated ? 'Token Rotated' : 'Project Created!' }}</h2>
          <p style="color:var(--muted); font-size:13px; margin-top:4px">
            Save this token now — it will <strong>never be shown again</strong>.
          </p>
        </div>

        <div class="field">
          <label class="field-label">Add to your project <code>.env</code></label>
          <div class="token-box">
            <pre class="input input-code" style="white-space:pre-wrap; user-select:all">LEAFHUB_TOKEN={{ tokenModal.token }}</pre>
            <button class="copy-btn" style="position:absolute; top:6px; right:6px"
                    @click="copyToken" :title="copied ? 'Copied!' : 'Copy'">
              {{ copied ? '✓' : '⎘' }}
            </button>
          </div>
        </div>

        <div class="field">
          <label class="field-label">Use with LeafHub SDK</label>
          <pre class="input input-code" style="white-space:pre-wrap">from leafhub import LeafHub

hub = LeafHub(token="{{ tokenModal.token }}")
key = hub.get_key("your-alias")         # raw API key string
client = hub.openai("your-alias")       # openai.OpenAI instance
client = hub.anthropic("your-alias")    # anthropic.Anthropic instance</pre>
        </div>

        <div style="text-align:center; margin-top:16px">
          <button class="btn-primary" @click="tokenModal.open = false">Done — I've saved the token</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { projects, providers as provApi } from '../api.js'

const items         = ref([])
const providersList = ref([])
const loading       = ref(true)
const error         = ref('')

// Names that appear more than once — used to highlight token_prefix as the disambiguator.
const duplicateNames = computed(() => {
  const counts = {}
  for (const p of items.value) counts[p.name] = (counts[p.name] || 0) + 1
  return new Set(Object.entries(counts).filter(([, n]) => n > 1).map(([k]) => k))
})

const modal = reactive({ open: false, id: null, error: '', saving: false })
const form  = reactive({ name: '', bindings: [], path: '' })

const tokenModal   = reactive({ open: false, token: '', rotated: false })
const copied       = ref(false)
const deleteResult = ref(null)   // null = hidden; array = cleanup summary lines

const linkModal = reactive({
  open: false, projectId: null, projectName: '', path: '',
  saving: false, error: '', success: '',
})

async function load() {
  loading.value = true; error.value = ''
  try {
    const [proj, prov] = await Promise.all([projects.list(), provApi.list()])
    items.value         = proj.data
    providersList.value = prov.data
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}

function provLabel(id) {
  return providersList.value.find(p => p.id === id)?.label || id.slice(0, 8)
}

function openCreate() {
  Object.assign(form, { name: '', bindings: [], path: '' })
  Object.assign(modal, { open: true, id: null, error: '', saving: false })
}

function openEdit(p) {
  Object.assign(form, {
    name: p.name,
    path: '',   // path not editable via PUT; use Link Dir button
    bindings: (p.bindings || []).map(b => ({
      alias: b.alias, provider_id: b.provider_id, model_override: b.model_override || '',
    })),
  })
  Object.assign(modal, { open: true, id: p.id, error: '', saving: false })
}

function closeModal() { modal.open = false }
function addBinding()  { form.bindings.push({ alias: '', provider_id: '', model_override: '' }) }
function removeBinding(i) { form.bindings.splice(i, 1) }

async function doSave() {
  modal.saving = true; modal.error = ''
  const bindings = form.bindings
    .filter(b => b.alias && b.provider_id)
    .map(b => ({ alias: b.alias, provider_id: b.provider_id,
                 model_override: b.model_override || null }))
  const body = { name: form.name, bindings }
  if (!modal.id && form.path.trim()) {
    body.path = form.path.trim()
  }
  try {
    if (modal.id) {
      await projects.update(modal.id, body)
    } else {
      const res = await projects.create(body)
      // If a path was provided, token was written to .leafhub — don't show it.
      if (!body.path) {
        tokenModal.token   = res.token
        tokenModal.rotated = false
        tokenModal.open    = true
      }
    }
    closeModal(); await load()
  } catch (e) { modal.error = e.message }
  finally { modal.saving = false }
}

async function doDelete(p) {
  if (!confirm(`Delete project "${p.name}"? Its token will stop working immediately.`)) return
  deleteResult.value = null
  try {
    const res = await projects.delete(p.id)
    const cleaned = [
      ...(res?.files_removed        || []).map(f => `removed file: ${f}`),
      ...(res?.registration_removed || []).map(r => `removed registration: ${r}`),
    ]
    deleteResult.value = cleaned
    setTimeout(() => { deleteResult.value = null }, 8000)
    await load()
  } catch (e) { error.value = e.message }
}

async function doRotate(p) {
  if (!confirm(`Rotate token for "${p.name}"? The current token will stop working immediately.`)) return
  try {
    const res = await projects.rotate(p.id)
    // If the project has a linked directory the server already rewrote .leafhub.
    if (res.dotfile_updated) {
      error.value = ''
      await load()
      return
    }
    tokenModal.token   = res.token
    tokenModal.rotated = true
    tokenModal.open    = true
    await load()
  } catch (e) { error.value = e.message }
}

function openLink(p) {
  Object.assign(linkModal, {
    open: true, projectId: p.id, projectName: p.name,
    path: p.path || '',
    saving: false, error: '', success: '',
  })
}

function closeLinkModal() { linkModal.open = false }

async function doLink() {
  linkModal.saving = true; linkModal.error = ''; linkModal.success = ''
  try {
    const res = await projects.link(
      linkModal.projectId, linkModal.path.trim()
    )
    linkModal.success = res.message || `Linked to ${linkModal.path}`
    await load()
  } catch (e) { linkModal.error = e.message }
  finally { linkModal.saving = false }
}

async function toggleActive(p) {
  try {
    if (p.is_active) await projects.deactivate(p.id)
    else             await projects.activate(p.id)
    await load()
  } catch (e) { error.value = e.message }
}

async function copyToken() {
  await navigator.clipboard.writeText(tokenModal.token)
  copied.value = true
  setTimeout(() => { copied.value = false }, 2000)
}

onMounted(load)
</script>

<style scoped>
.binding-row    { display: flex; align-items: center; gap: 8px; padding: 4px 0;
                  font-size: 13px; border-bottom: 1px solid var(--border) }
.binding-row:last-child { border-bottom: none }
.binding-editor { display: flex; gap: 6px; align-items: center; margin-bottom: 6px }
.token-box      { position: relative }

.linked-path {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--muted);
  margin-bottom: 10px; padding: 4px 8px;
  background: var(--surface-alt, rgba(0,0,0,.04));
  border-radius: 4px; overflow: hidden;
}
.linked-path .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.link-icon { flex-shrink: 0 }

.field-hint       { font-size: 12px; color: var(--muted); margin-top: 4px }
.prefix-highlight { color: var(--primary); font-weight: 600 }
.checkbox-label   { display: flex; align-items: center; gap: 8px;
                    font-size: 13px; cursor: pointer; user-select: none }
.checkbox-label input[type="checkbox"] { width: 15px; height: 15px;
                                         flex-shrink: 0; cursor: pointer }

.success-banner {
  background: rgba(34,197,94,.12); border: 1px solid rgba(34,197,94,.3);
  color: #166534; border-radius: 6px; padding: 10px 14px;
  font-size: 13px; margin-bottom: 12px;
}
</style>

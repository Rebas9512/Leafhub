<template>
  <div class="layout">
    <nav class="sidebar">
      <div class="logo">
        <span class="logo-icon">🔐</span>
        <span class="logo-text">LeafHub</span>
      </div>

      <RouterLink to="/providers" class="nav-link">
        <span class="nav-icon">🔌</span> Providers
      </RouterLink>
      <RouterLink to="/projects" class="nav-link">
        <span class="nav-icon">📁</span> Projects
      </RouterLink>

      <div class="sidebar-bottom">
        <div :class="['status-dot', healthy ? 'ok' : 'err']"></div>
        <span class="status-label">{{ healthy ? 'Online' : 'Offline' }}</span>
        <button class="btn-icon" title="Settings" @click="showSettings = true">⚙</button>
      </div>
    </nav>

    <main class="content">
      <RouterView />
    </main>

    <!-- Admin token settings modal -->
    <div v-if="showSettings" class="modal-backdrop" @click.self="showSettings = false">
      <div class="modal">
        <h2>Settings</h2>
        <label class="field-label">Admin Token
          <small>(set LEAFHUB_ADMIN_TOKEN server-side; leave blank for dev mode)</small>
        </label>
        <input v-model="tokenInput" type="password" class="input" placeholder="lh-admin-..." />
        <div class="modal-actions">
          <button class="btn-secondary" @click="showSettings = false">Cancel</button>
          <button class="btn-primary" @click="saveToken">Save</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { system, getAdminToken, setAdminToken } from './api.js'

const healthy      = ref(false)
const showSettings = ref(false)
const tokenInput   = ref(getAdminToken())

async function checkHealth() {
  try { await system.health(); healthy.value = true }
  catch { healthy.value = false }
}

function saveToken() {
  setAdminToken(tokenInput.value)
  showSettings.value = false
  checkHealth()
}

onMounted(() => {
  checkHealth()
  setInterval(checkHealth, 30_000)
})
</script>

<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }

:root {
  --bg:       #0d0f1a;
  --surface:  #151824;
  --border:   #252840;
  --text:     #dde2f0;
  --muted:    #6872a0;
  --primary:  #5b8df5;
  --primary-h:#4070e8;
  --danger:   #f06060;
  --success:  #3dd68c;
  --warning:  #f5a623;
  --radius:   8px;
  --shadow:   0 1px 4px rgba(0,0,0,.4), 0 2px 8px rgba(0,0,0,.25);
}

body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5 }

.layout  { display: flex; height: 100vh; overflow: hidden }
.sidebar { width: 200px; background: var(--surface); border-right: 1px solid var(--border);
           display: flex; flex-direction: column; padding: 16px 12px; gap: 4px; flex-shrink: 0 }
.content { flex: 1; overflow-y: auto; padding: 24px }

.logo       { display: flex; align-items: center; gap: 8px; padding: 8px 4px 20px;
              font-weight: 700; font-size: 16px }
.logo-icon  { font-size: 22px }

.nav-link   { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
              border-radius: var(--radius); color: var(--muted); text-decoration: none;
              font-weight: 500; transition: all .15s }
.nav-link:hover      { background: var(--bg); color: var(--text) }
.nav-link.router-link-active { background: rgba(91,141,245,.12); color: var(--primary) }
.nav-icon  { font-size: 16px }

.sidebar-bottom { margin-top: auto; display: flex; align-items: center; gap: 6px;
                  padding: 8px 4px; font-size: 12px; color: var(--muted) }
.status-dot  { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0 }
.status-dot.ok  { background: var(--success) }
.status-dot.err { background: var(--danger) }
.status-label   { flex: 1 }
.btn-icon       { border: none; background: none; cursor: pointer; font-size: 16px;
                  color: var(--muted); padding: 2px; border-radius: 4px }
.btn-icon:hover { color: var(--text); background: var(--bg) }

/* ── Shared component styles ─────────────────────────── */
h1 { font-size: 20px; font-weight: 700; margin-bottom: 4px }
.page-subtitle { color: var(--muted); margin-bottom: 24px }

.card { background: var(--surface); border: 1px solid var(--border);
        border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow) }
.card-header { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px }
.card-body   { display: flex; gap: 8px; flex-wrap: wrap; align-items: center }
.card-title  { font-weight: 600; font-size: 15px }
.card-meta   { font-size: 12px; color: var(--muted) }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px }

.badge { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 12px;
         font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .4px;
         white-space: nowrap; flex-shrink: 0 }
.badge-ok      { background: rgba(61,214,140,.15); color: #3dd68c }
.badge-err     { background: rgba(240,96,96,.15);  color: #f06060 }
.badge-warn    { background: rgba(245,166,35,.15); color: #f5a623 }
.badge-neutral { background: rgba(104,114,160,.15);color: #8892b8 }
.badge-blue    { background: rgba(91,141,245,.15); color: #7aa8f8 }

.btn-primary   { padding: 7px 14px; background: var(--primary); color: #fff;
                 border: none; border-radius: var(--radius); cursor: pointer;
                 font-size: 13px; font-weight: 500; transition: background .15s }
.btn-primary:hover   { background: var(--primary-h) }
.btn-secondary { padding: 7px 14px; background: transparent; color: var(--text);
                 border: 1px solid var(--border); border-radius: var(--radius);
                 cursor: pointer; font-size: 13px; font-weight: 500 }
.btn-secondary:hover { background: var(--bg) }
.btn-danger    { padding: 7px 14px; background: var(--danger); color: #fff;
                 border: none; border-radius: var(--radius); cursor: pointer;
                 font-size: 13px; font-weight: 500 }
.btn-danger:hover { background: #dc2626 }
.btn-sm  { padding: 4px 10px; font-size: 12px }
.btn-link { background: none; border: none; color: var(--primary); cursor: pointer;
            font-size: 13px; padding: 0; text-decoration: underline }

.input, .select, textarea.input {
  width: 100%; padding: 8px 10px; border: 1px solid var(--border);
  border-radius: var(--radius); font-size: 13px; font-family: inherit;
  background: var(--surface); color: var(--text); outline: none;
  transition: border-color .15s }
.input:focus, .select:focus { border-color: var(--primary) }
.select { appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24'%3E%3Cpath d='M7 10l5 5 5-5z' fill='%236872a0'/%3E%3C/svg%3E");
          background-repeat: no-repeat; background-position: right 8px center; padding-right: 28px }
.input-code { font-family: monospace; font-size: 12px; background: #0a0c15 }

.field       { margin-bottom: 14px }
.field-label { display: block; font-size: 12px; font-weight: 600;
               color: var(--muted); margin-bottom: 5px; text-transform: uppercase; letter-spacing: .4px }
.field-label small { font-weight: 400; text-transform: none; letter-spacing: 0; color: var(--muted) }

.page-actions { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px }

/* ── Modal ───────────────────────────────────────────── */
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,.4);
                  display: flex; align-items: center; justify-content: center; z-index: 100 }
.modal         { background: var(--surface); border-radius: 10px; padding: 24px;
                 width: 520px; max-width: 95vw; max-height: 90vh; overflow-y: auto;
                 box-shadow: 0 20px 60px rgba(0,0,0,.6) }
.modal h2      { font-size: 17px; font-weight: 700; margin-bottom: 20px }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px }

/* ── Table ───────────────────────────────────────────── */
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius) }
table  { width: 100%; border-collapse: collapse; font-size: 13px }
thead  { background: var(--bg) }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border) }
th     { font-weight: 600; font-size: 11px; text-transform: uppercase;
         letter-spacing: .4px; color: var(--muted) }
tbody tr:last-child td { border-bottom: none }
tbody tr:hover         { background: #1e2133 }

/* ── Misc ────────────────────────────────────────────── */
.error-banner { background: rgba(240,96,96,.12); border: 1px solid rgba(240,96,96,.3);
                border-radius: var(--radius); padding: 10px 14px; color: #f07878;
                font-size: 13px; margin-bottom: 16px }
.empty-state  { text-align: center; padding: 48px; color: var(--muted) }
.empty-state p { margin-top: 8px; font-size: 14px }
.spinner      { display: inline-block; width: 16px; height: 16px;
                border: 2px solid var(--border); border-top-color: var(--primary);
                border-radius: 50%; animation: spin .6s linear infinite }
@keyframes spin { to { transform: rotate(360deg) } }
.mono { font-family: monospace; font-size: 12px }
.copy-btn { background: none; border: none; cursor: pointer; font-size: 14px;
            padding: 2px 4px; border-radius: 4px; color: var(--muted) }
.copy-btn:hover { color: var(--text); background: var(--bg) }
</style>

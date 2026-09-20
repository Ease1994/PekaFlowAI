/**
 * release Atom SDK for Node.js（无第三方依赖）。
 * Agent 注入环境变量；插件写 stdout 与 .release_atom_output.json。
 */
'use strict'

const fs = require('fs')
const path = require('path')

const status = { SUCCESS: 'success', FAILURE: 'failure', ERROR: 'error' }

function env(k, d) {
  const v = process.env[k]
  return v == null || v === '' ? (d || '') : v
}

function getWorkspace() {
  return env('RELEASE_WORKSPACE') || env('BK_CI_WORKSPACE') || process.cwd()
}

function getSrc() {
  return env('RELEASE_SRC') || path.join(getWorkspace(), 'src')
}

function getInput() {
  const raw = env('RELEASE_ATOM_INPUT_JSON')
  if (!raw) return {}
  try {
    const o = JSON.parse(raw)
    return o && typeof o === 'object' ? o : {}
  } catch (e) {
    return {}
  }
}

function getPipelineId() {
  return env('RELEASE_PIPELINE_ID') || env('BK_CI_PIPELINE_ID')
}

function getBuildId() {
  return env('RELEASE_BUILD_ID') || env('BK_CI_BUILD_ID')
}

function getJobName() {
  return env('RELEASE_JOB_NAME')
}

function getSensitiveConf(key) {
  return env('RELEASE_SENSITIVE_' + String(key).toUpperCase())
}

const log = {
  info: (m) => { console.log('[INFO]: ' + m) },
  warning: (m) => { console.log('[WARNING]: ' + m) },
  error: (m) => { console.error('[ERROR]: ' + m) },
}

function setOutput(output) {
  const text = JSON.stringify(output, null, 2)
  const files = [
    path.join(getWorkspace(), '.release_atom_output.json'),
    path.join(process.cwd(), '.release_atom_output.json'),
  ]
  for (const f of files) {
    try { fs.writeFileSync(f, text, 'utf8') } catch (e) { /* ignore */ }
  }
  const data = (output && output.data) || {}
  for (const [k, meta] of Object.entries(data)) {
    if (meta && typeof meta === 'object' && meta.value != null) {
      console.log('##[set-output]' + k + '=' + meta.value)
    }
  }
}

module.exports = {
  status,
  getWorkspace,
  getSrc,
  getInput,
  getPipelineId,
  getBuildId,
  getJobName,
  getSensitiveConf,
  log,
  setOutput,
}

import {
  Component,
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { applyTheme, currentTheme, type Theme } from './theme'

// ---------------------------------------------------------------------------
// Contract types - mirror src/shelfwise_contracts serialized shapes.
// ---------------------------------------------------------------------------
type RecommendedAction = { type?: string; params?: Record<string, unknown>; risk_tier?: string }
type SourceRef = { kind?: string; ref?: string; locator?: string | null }
type SupportingFact = { fact?: string; value?: unknown; source?: string; method?: string }
type EvidenceObject = {
  agent?: string
  conclusion?: string
  supporting_data?: SupportingFact[]
  confidence?: string | number
  recommended_action?: RecommendedAction
  sources?: SourceRef[]
  requires_human_review?: boolean
}
type Decision = {
  id?: string
  status?: string
  action?: RecommendedAction
  caused_by?: string[]
  summary?: string
  role?: string
  created_at?: string
  updated_at?: string
  review?: { reviewer?: string; status?: string; reviewed_at?: string } | null
  outcome?: { units_cleared?: number; waste_units?: number; rand_recovered?: unknown; success_score?: string }
}
type LearningEvent = {
  id?: string
  decision_id?: string
  message?: string
  outcome?: Decision['outcome']
}
type TraceSpan = { name?: string; status?: string; ms?: number; detail?: Record<string, unknown> }
type InferenceConfig = {
  provider?: string
  base_url_configured?: boolean
  base_url_host?: string
  accelerator?: string
  routine_model?: string
  strong_model?: string
  api_key_present?: boolean
  routing?: { routine_agents?: string[]; strong_agents?: string[] }
}
type FefoBatch = {
  lot?: string
  units?: number
  expiry_date?: string
  days_to_expiry?: number
  location?: string
  stock_status?: string
}
type StoreIntelligence = {
  batch_split?: {
    sku?: string
    total_units?: number
    priority_sell_units?: number
    normal_units?: number
    blocked_units?: number
    conclusion?: string
    fefo_batches?: FefoBatch[]
  }
  delivery_reconciliation?: {
    sku?: string
    ordered_units?: number
    asn_units?: number
    received_units?: number
    accepted_units?: number
    rejected_units?: number
    missing_units?: number
    over_units?: number
    short_dated_units?: number
    supplier_fill_rate?: string
    status?: string
    conclusion?: string
  }
  supplier_cover?: {
    sku?: string
    units_on_hand?: number
    forecast_daily_units?: string
    supplier_lead_time_days?: string
    days_of_supply?: string
    units_needed_until_delivery?: number
    gap_before_delivery_units?: number
    transfer_available_units?: number
    transfer_units_recommended?: number
    recommended_action?: string
    conclusion?: string
  }
  learning_summary?: { sku?: string; score?: string; lesson?: string }
}
/** /data/seed/summary - the one-product stock card the store is currently working. */
type SeedSummary = {
  sku?: string
  product_name?: string
  category?: string
  supplier?: string
  location?: string
  units_on_hand?: number
  reorder_point?: number
  days_to_expiry?: number
  supplier_lead_time_days?: string
  supplier_recent_delay?: boolean
}
type ProductCatalogItem = {
  sku?: string
  product_id?: string
  barcode?: string | null
  plu?: string | null
  name?: string
  receipt_name?: string
  brand?: string
  generic_name?: string
  department?: string
  category?: string
  subcategory?: string
  supplier?: string
  source?: string
  synthetic?: boolean
  price?: unknown
  on_hand?: number
  reorder_point?: number
  expiry_date?: string
  days_to_expiry?: number
  requires_attention?: boolean
  attention_reasons?: string[]
  attention_summary?: string
  sell_first_units?: number
  normal_units?: number
  blocked_units?: number
  total_units?: number
  lot_count?: number
  fefo_batches?: FefoBatch[]
  shelf_location?: string
  storage_requirements?: string
}
type ProductAttentionPayload = {
  limit?: number
  truncated?: boolean
  totals?: JsonObject
  items?: ProductCatalogItem[]
  sell_first?: ProductCatalogItem[]
  to_order?: ProductCatalogItem[]
  expiring?: ProductCatalogItem[]
}
type ProductSearchPayload = {
  query?: string
  limit?: number
  truncated?: boolean
  products?: ProductCatalogItem[]
  source_counts?: JsonObject
}
type GoldenDemo = {
  correlation_id?: string
  scenario?: string
  evidence?: EvidenceObject[]
  decision?: Decision
  trace?: TraceSpan[]
  inference?: InferenceConfig
  learning?: { status?: string; message?: string }
  store_intelligence?: StoreIntelligence
}
type JsonObject = Record<string, unknown>
type ConnectorSystemRow = {
  system?: string
  label?: string
  transport?: string
  priority?: number
  read_supported?: boolean
  webhook_supported?: boolean
  mapper_registered?: boolean
  write_back_mode?: string
  enabled_for_tenant?: boolean
  status?: string
}
type InboundRecordRow = {
  id?: string
  source_system?: string
  source_object_type?: string
  source_object_id?: string
  canonical_type?: string
  event_id?: string | null
  validation?: { ok?: boolean; errors?: string[] }
  created_at?: string
  raw_payload_hash?: string
}
type EventRow = {
  id?: string
  type?: string
  ts?: string
  tenant_id?: string
  correlation_id?: string
  payload?: JsonObject
}
type BusMessageRow = {
  id?: string
  message_id?: string
  stream?: string
  event?: EventRow
}
type WritebackTaskRow = {
  id?: string
  title?: string
  status?: string
  assignee_role?: string
  action?: RecommendedAction
  created_at?: string
}
type OperationalSnapshot = {
  apiPaths: string[]
  health: JsonObject
  readiness: JsonObject
  inferenceConfig: JsonObject
  connectorSystems: ConnectorSystemRow[]
  tenantConnectors: ConnectorSystemRow[]
  inboundRecords: InboundRecordRow[]
  events: EventRow[]
  busMessages: BusMessageRow[]
  coldChainStatus: JsonObject
  coldChainEvents: JsonObject[]
  traces: JsonObject[]
  platformTools: JsonObject[]
  platformToolAudit: JsonObject[]
  writebackTasks: WritebackTaskRow[]
  learningThresholds: JsonObject
  learningEvents: JsonObject[]
  productAttention: ProductAttentionPayload
  modelRuns: JsonObject[]
  promptVersions: JsonObject[]
  accountability: JsonObject
  tenantFacts: JsonObject[]
  tenantProfile: JsonObject
  workerRuns: JsonObject[]
  worldgenRuns: JsonObject[]
  detectiveSql: string
  observability: JsonObject
  worker: JsonObject
}
type DecisionLogResponse = { decisions?: Decision[] }
type TransitionResult = { decision: Decision; learning_event?: LearningEvent | null }
type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type Tone = 'ok' | 'warn' | 'risk' | 'info' | 'mute' | 'accent'
type WorkspaceOpenOptions = { query?: string }

// Chat is Q&A only now - decisions live in the persistent status bar + slide-down panel, never
// embedded as an interactive card inside a message (that was duplicate UI: the same pending
// decision rendered twice, in two different places, with two different ways to act on it).
type ChatMessage = { id: string; role: 'user' | 'assistant'; text: string }
type UiIconName = 'close' | 'menu' | 'mic' | 'moon' | 'send' | 'stop' | 'sun'

declare global {
  interface Window {
    SHELFWISE_CONFIG?: {
      apiBase?: string
      apiKey?: string
    }
  }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
const DEFAULT_API_BASE = 'http://localhost:8000'
const DEMO_PATH = '/demo/golden'

// Runtime config (public/shelfwise-config.js) lets a deployed bundle point at any backend
// without a rebuild; build-time VITE_* vars stay as the fallback.
function runtimeConfig(): Window['SHELFWISE_CONFIG'] {
  return typeof window === 'undefined' ? undefined : window.SHELFWISE_CONFIG
}
function configuredBase(): string {
  const env = import.meta.env as Record<string, string | undefined>
  return (runtimeConfig()?.apiBase ?? env.VITE_API_BASE ?? env.VITE_API_BASE_URL ?? '').trim()
}
function apiKey(): string {
  return (runtimeConfig()?.apiKey ?? (import.meta.env as Record<string, string | undefined>).VITE_API_KEY ?? '').trim()
}
function authHeaders(): Record<string, string> {
  const key = apiKey()
  return key ? { 'x-api-key': key } : {}
}
function joinUrl(base: string, path: string): string {
  return base ? `${base.replace(/\/+$/, '')}${path}` : path
}
function requestUrls(path: string): string[] {
  const configured = configuredBase()
  const urls: string[] = []
  if (!configured && import.meta.env.DEV) urls.push(path)
  urls.push(joinUrl(configured || DEFAULT_API_BASE, path))
  return Array.from(new Set(urls))
}
function backendRequestUrls(path: string): string[] {
  return [joinUrl(configuredBase() || DEFAULT_API_BASE, path)]
}
async function fetchJson<T>(path: string, init: RequestInit, signal: AbortSignal): Promise<T> {
  return fetchFromUrls<T>(requestUrls(path), path, init, signal)
}
async function fetchBackendJson<T>(path: string, init: RequestInit, signal: AbortSignal): Promise<T> {
  return fetchFromUrls<T>(backendRequestUrls(path), path, init, signal)
}
async function fetchFromUrls<T>(urls: string[], path: string, init: RequestInit, signal: AbortSignal): Promise<T> {
  let lastError = 'Unknown error'
  for (const url of urls) {
    try {
      const res = await fetch(url, {
        ...init,
        headers: { Accept: 'application/json', ...authHeaders(), ...(init.headers ?? {}) },
        signal,
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText || 'HTTP error'}`.trim())
      return (await res.json()) as T
    } catch (error) {
      if (signal.aborted) throw error
      lastError = error instanceof Error ? error.message : String(error)
    }
  }
  throw new Error(`Could not reach ${path}. ${lastError}`)
}
const fetchDemo = (path: string, signal: AbortSignal) => fetchJson<GoldenDemo>(path, { method: 'GET' }, signal)
async function fetchOptional<T>(path: string, signal: AbortSignal): Promise<T | null> {
  try {
    return await fetchBackendJson<T>(path, { method: 'GET' }, signal)
  } catch {
    return null
  }
}
async function postTransition(id: string, transition: 'approve' | 'reject', signal: AbortSignal): Promise<TransitionResult> {
  const payload = await fetchJson<TransitionResult>(
    `/decisions/${encodeURIComponent(id)}/${transition}`,
    { method: 'POST' },
    signal,
  )
  if (!payload.decision) throw new Error('Transition response did not include a decision.')
  return { decision: payload.decision, learning_event: payload.learning_event ?? null }
}
async function postChat(question: string, signal: AbortSignal): Promise<string> {
  let lastError = 'Unknown error'
  for (const url of requestUrls('/chat')) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          Accept: 'text/plain',
          'Content-Type': 'application/json',
          ...authHeaders(),
        },
        body: JSON.stringify({ question }),
        signal,
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText || 'HTTP error'}`.trim())
      return (await res.text()).trim()
    } catch (error) {
      if (signal.aborted) throw error
      lastError = error instanceof Error ? error.message : String(error)
    }
  }
  throw new Error(`Could not reach /chat. ${lastError}`)
}

// ---------------------------------------------------------------------------
// Formatting + derived helpers
// ---------------------------------------------------------------------------
function formatLabel(value: unknown): string {
  return String(value ?? 'unknown').replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
const SKU_LABELS: Record<string, string> = {
  milk_2l: '2L milk',
  yoghurt_1l: '1L yoghurt',
}
const SOURCE_LABELS: Record<string, string> = {
  seed_stock: 'Stock ledger',
  seed_sales: 'Sales history',
  load_shedding: 'Load-shedding schedule',
  simulate_markdown: 'Markdown simulation',
  critic_gate: 'Critic gate',
  executive_policy: 'Executive policy',
  seed_suppliers: 'Supplier file',
}
function humanizeOperationalText(value: unknown): string {
  return String(value ?? '').replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, (match) => {
    return SKU_LABELS[match] ?? match.replace(/_/g, ' ')
  })
}
function formatMoneyish(value: unknown): string | null {
  if (value && typeof value === 'object') {
    const m = value as { minor_units?: number }
    if (typeof m.minor_units === 'number') return `R${Math.round(m.minor_units / 100).toLocaleString('en-ZA')}`
  }
  if (typeof value === 'string') {
    const m = value.match(/^\s*(?:ZAR|R)\s*([\d,]+(?:\.\d+)?)\s*$/i)
    if (m) return `R${Math.round(Number(m[1].replace(/,/g, ''))).toLocaleString('en-ZA')}`
  }
  return null
}
function moneyMinorUnits(value: unknown): number | null {
  if (value && typeof value === 'object') {
    const m = value as { minor_units?: number }
    if (typeof m.minor_units === 'number') return m.minor_units
  }
  if (typeof value === 'string') {
    const m = value.match(/^\s*(?:ZAR|R)\s*([\d,]+(?:\.\d+)?)\s*$/i)
    if (m) return Math.round(Number(m[1].replace(/,/g, '')) * 100)
  }
  return null
}
function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  const money = formatMoneyish(value)
  if (money) return money
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '-'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(formatValue).join(', ')
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}
function emptyOps(): OperationalSnapshot {
  return {
    apiPaths: [],
    health: {},
    readiness: {},
    inferenceConfig: {},
    connectorSystems: [],
    tenantConnectors: [],
    inboundRecords: [],
    events: [],
    busMessages: [],
    coldChainStatus: {},
    coldChainEvents: [],
    traces: [],
    platformTools: [],
    platformToolAudit: [],
    writebackTasks: [],
    learningThresholds: {},
    learningEvents: [],
    productAttention: {},
    modelRuns: [],
    promptVersions: [],
    accountability: {},
    tenantFacts: [],
    tenantProfile: {},
    workerRuns: [],
    worldgenRuns: [],
    detectiveSql: '',
    observability: {},
    worker: {},
  }
}
function asObject(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonObject) : {}
}
function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}
function fieldText(row: JsonObject | undefined, key: string, fallback = '-'): string {
  const value = row?.[key]
  if (value === null || value === undefined || value === '') return fallback
  return humanizeOperationalText(formatValue(value))
}
function fieldNumber(row: JsonObject | undefined, key: string): number {
  const n = Number(row?.[key])
  return Number.isFinite(n) ? n : 0
}
function optionalStatusTone(value: unknown): Tone | undefined {
  const status = String(value ?? '').toLowerCase()
  if (['enabled', 'live', 'ok', 'ready', 'running', 'accepted', 'approved', 'done'].includes(status)) return 'ok'
  if (['review', 'pending', 'available', 'loading', 'idle'].includes(status)) return 'warn'
  if (['error', 'failed', 'invalid', 'blocked', 'rejected', 'offline'].includes(status)) return 'risk'
  return undefined
}
function confidencePct(value: string | number | undefined): number {
  const n = Number(value)
  if (!Number.isFinite(n)) return 0
  return Math.max(0, Math.min(100, Math.round((n > 1 ? n / 100 : n) * 100)))
}
function formatSource(source: SourceRef): string {
  const ref = source.ref ?? 'unknown'
  return SOURCE_LABELS[ref] ?? formatLabel(ref)
}
function formatFactSource(value: unknown): string {
  if (!value) return '-'
  const ref = String(value).split('#')[0]
  return SOURCE_LABELS[ref] ?? formatLabel(ref)
}
function riskTone(tier?: string): Tone {
  const t = (tier ?? '').toLowerCase()
  return t === 'high' ? 'risk' : t === 'medium' ? 'warn' : 'ok'
}
function statusTone(status?: string): Tone {
  const s = (status ?? '').toLowerCase()
  if (s === 'ok' || s === 'approved') return 'ok'
  if (s === 'rejected' || s === 'error' || s === 'timeout') return 'risk'
  if (s === 'pending' || s === 'degraded') return 'warn'
  return 'mute'
}
function decisionTime(decision?: Decision): string {
  const raw = decision?.updated_at ?? decision?.created_at
  if (!raw) return '-'
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function sortByTimeDesc(decisions: Decision[]): Decision[] {
  const key = (d: Decision) => {
    const v = Date.parse(d.updated_at ?? d.created_at ?? '')
    return Number.isNaN(v) ? 0 : v
  }
  return [...(Array.isArray(decisions) ? decisions : [])].sort((a, b) => key(b) - key(a))
}

const toneVar: Record<Tone, string> = {
  ok: 'var(--ok)', warn: 'var(--warn)', risk: 'var(--risk)', info: 'var(--info)', mute: 'var(--mute)', accent: 'var(--accent)',
}

/** Which agent raised this decision - used to group/rank the approval stack. */
function agencyForAction(action?: RecommendedAction): string {
  const t = (action?.type ?? '').toLowerCase()
  if (t.includes('markdown') || t.includes('bundle')) return 'Expiry'
  if (t.includes('reorder')) return 'Inventory'
  if (t.includes('supplier')) return 'Procurement'
  if (t.includes('move') || t.includes('transfer')) return 'Cold chain'
  if (t.includes('monitor')) return 'Executive'
  return 'Operations'
}
function riskRank(tier?: string): number {
  const t = (tier ?? 'low').toLowerCase()
  return t === 'high' ? 0 : t === 'medium' ? 1 : 2
}
/** The day's approval queue: pending decisions, most urgent (by risk) first. */
function pendingQueue(decisions: Decision[], current?: Decision): Decision[] {
  const byId = new Map<string, Decision>()
  for (const d of decisions) if (d.id) byId.set(d.id, d)
  if (current?.id) byId.set(current.id, current)
  return Array.from(byId.values())
    .filter((d) => (d.status ?? 'pending').toLowerCase() === 'pending')
    .sort((a, b) => riskRank(a.action?.risk_tier) - riskRank(b.action?.risk_tier))
}
function describeAction(action?: RecommendedAction): string {
  if (!action) return 'No action'
  const discount = action.params?.discount_pct
  const pct =
    typeof discount === 'string' || typeof discount === 'number'
      ? `${Math.round((Number(discount) > 1 ? Number(discount) / 100 : Number(discount)) * 100)}%`
      : ''
  if (action.type === 'apply_markdown') return pct ? `Apply ${pct} markdown` : 'Apply markdown'
  if (action.type === 'monitor') return 'Monitor only'
  if (action.type === 'supplier_switch') return 'Switch supplier'
  return formatLabel(action.type)
}
function firstActionEvidence(evidence?: EvidenceObject[], actionType?: string): EvidenceObject | undefined {
  const items = evidence ?? []
  return (
    items.find((i) => i.recommended_action?.type === actionType && i.requires_human_review) ??
    items.find((i) => i.recommended_action?.type === actionType) ??
    items.find((i) => i.requires_human_review) ??
    items.find((i) => i.recommended_action) ??
    items[0]
  )
}
function moneyAtRisk(evidence?: EvidenceObject[]): string | null {
  for (const e of evidence ?? [])
    for (const f of e.supporting_data ?? [])
      if ((f.fact ?? '').toLowerCase().includes('at_risk')) {
        const m = formatMoneyish(f.value)
        if (m) return m
      }
  return null
}
function whyLine(decision: Decision, evidence?: EvidenceObject[]): string {
  const ev = firstActionEvidence(evidence, decision.action?.type)
  return humanizeOperationalText(ev?.conclusion ?? decision.summary ?? 'Recommended by the ShelfWise cascade.')
}

/** Demo intent-matching. Real streaming /chat is the follow-up; this never fabricates numbers.
 *  Chat only ever points at the status bar now - it never renders the decisions itself. */
function replyFor(text: string, data: GoldenDemo | null, pending: Decision[]): string {
  const q = text.toLowerCase()
  // Risk intent is checked before the approvals intent: "what's at risk today?" is a risk
  // question even though it also mentions the day.
  if (/risk|waste|expir|cold|fridge|warm|spoil/.test(q)) {
    const ev = firstActionEvidence(data?.evidence)
    const atRisk = moneyAtRisk(data?.evidence)
    const base = humanizeOperationalText(ev?.conclusion ?? 'Nothing is flagged at risk right now.')
    return atRisk ? `${base} About ${atRisk} of stock is exposed.` : base
  }
  if (/approv|decision|queue|pending|what.*do/.test(q)) {
    return pending.length
      ? `${pending.length} approval${pending.length > 1 ? 's' : ''} waiting. Open the status bar to review the evidence.`
      : 'Queue clear. No approvals are waiting.'
  }
  if (/why|reason|explain|how/.test(q)) {
    return 'Open the status bar, then choose Why? on a decision to see the agent chain and numbers.'
  }
  return 'Ask about approvals, stock risk, deliveries, or evidence.'
}

/** Local calendar-day label for grouping the receipt timeline ("Today", "Yesterday", or a date). */
function dayLabel(iso: string | undefined): string {
  if (!iso) return 'Earlier'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Earlier'
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diffDays = Math.round((startOf(new Date()) - startOf(d)) / 86_400_000)
  if (diffDays === 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })
}

function receiptDetail(decision: Decision): string {
  const outcome = decision.outcome
  if (outcome) {
    const parts: string[] = []
    if (outcome.units_cleared != null) parts.push(`${outcome.units_cleared} units cleared`)
    const recovered = formatMoneyish(outcome.rand_recovered)
    if (recovered) parts.push(`${recovered} recovered`)
    if (outcome.success_score) parts.push(`score ${outcome.success_score}`)
    if (parts.length) return parts.join(', ')
  }
  return humanizeOperationalText(decision.summary ?? 'No further detail recorded.')
}

let msgSeq = 0
const newMsgId = () => `m${++msgSeq}`

// ---------------------------------------------------------------------------
// Voice input (browser SpeechRecognition; degrades silently)
// ---------------------------------------------------------------------------
function useVoiceInput(onText: (text: string) => void) {
  const [supported] = useState(
    () => typeof window !== 'undefined' && Boolean((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition),
  )
  const [listening, setListening] = useState(false)
  const recRef = useRef<any>(null)

  const stop = useCallback(() => {
    try {
      recRef.current?.stop?.()
    } catch {
      /* ignore */
    }
    setListening(false)
  }, [])

  const start = useCallback(() => {
    if (!supported || listening) return
    const Ctor = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    const rec = new Ctor()
    rec.lang = 'en-ZA'
    rec.interimResults = false
    rec.maxAlternatives = 1
    rec.onresult = (e: any) => {
      const t = e?.results?.[0]?.[0]?.transcript
      if (t) onText(String(t))
    }
    rec.onend = () => setListening(false)
    rec.onerror = () => setListening(false)
    recRef.current = rec
    try {
      rec.start()
      setListening(true)
    } catch {
      setListening(false)
    }
  }, [supported, listening, onText])

  useEffect(
    () => () => {
      try {
        recRef.current?.abort?.()
      } catch {
        /* ignore */
      }
    },
    [],
  )

  return { supported, listening, start, stop }
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------
function StatusGlyph({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`glyph tone-${tone}`}>
      <span className={`glyph-shape tone-${tone}`} aria-hidden />
      <span>{label}</span>
    </span>
  )
}
function UiIcon({ name }: { name: UiIconName }) {
  return <span className={`ui-icon ui-icon-${name}`} aria-hidden />
}
function Confidence({ value }: { value: string | number | undefined }) {
  const pct = confidencePct(value)
  return (
    <span className="conf" role="img" aria-label={`confidence ${pct}%`}>
      <span className="conf-track">
        <span className="conf-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="conf-pct tnum">{pct}%</span>
    </span>
  )
}
class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  componentDidCatch(err: unknown) {
    console.error('[app]', err)
  }
  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="bubble assistant">
        <p>Something failed to render.</p>
        <button className="btn btn-secondary" type="button" onClick={() => this.setState({ failed: false })}>
          Retry
        </button>
      </div>
    )
  }
}

// ---------------------------------------------------------------------------
// Reasoning (on-demand, INSIDE a decision) - compact chain + swap-in detail
// ---------------------------------------------------------------------------
function Reasoning({ evidence }: { evidence: EvidenceObject[] }) {
  const [active, setActive] = useState(0)
  const selected = evidence[active]
  if (evidence.length === 0) return <p className="why-empty">No agent chain is attached to this decision.</p>
  return (
    <div className="why-body">
      <ol className="why-chain">
        {evidence.map((item, index) => {
          const tone = riskTone(item.recommended_action?.risk_tier)
          const detailFacts = selected?.supporting_data ?? []
          const factSourceLabels = new Set(detailFacts.map((fact) => formatFactSource(fact.source ?? fact.method)).filter((label) => label !== '-'))
          const sourceLabels = Array.from(new Set((selected?.sources ?? []).map(formatSource).filter((label) => !factSourceLabels.has(label))))
          return (
            <li key={`${item.agent ?? 'a'}-${index}`}>
              <button
                type="button"
                className={`why-step ${index === active ? 'is-active' : ''}`}
                aria-expanded={index === active}
                onClick={() => setActive(index)}
              >
                <span className={`glyph-shape tone-${tone}`} aria-hidden />
                <span className="why-step-text">
                  <span>{formatLabel(item.agent)}</span>
                  <small>{humanizeOperationalText(item.conclusion ?? 'No conclusion.')}</small>
                </span>
                <Confidence value={item.confidence} />
              </button>
              {index === active ? (
                <div className="why-detail" style={{ '--rail': toneVar[tone] } as CSSProperties}>
                  {detailFacts.length > 0 ? (
                    <dl className="facts">
                      {detailFacts.map((f, i) => (
                        <div className="fact-row" key={`${f.fact ?? 'f'}-${i}`}>
                          <dt>{formatLabel(f.fact ?? f.method ?? 'Fact')}</dt>
                          <dd className="fact-value tnum">{formatValue(f.value)}</dd>
                          <dd className="fact-source">{formatFactSource(f.source ?? f.method)}</dd>
                        </div>
                      ))}
                    </dl>
                  ) : (
                    <p className="why-empty">No supporting facts for this step.</p>
                  )}
                  {sourceLabels.length > 0 ? (
                    <p className="source-line">
                      {sourceLabels.map((label, i) => (
                        <span key={`${label}-${i}`}>
                          {label}
                        </span>
                      ))}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

// ---------------------------------------------------------------------------
// A single decision card (inside a message / the stack)
// ---------------------------------------------------------------------------
function DecisionCard({
  decision,
  evidence,
  busyId,
  onApprove,
  onReject,
}: {
  decision: Decision
  evidence?: EvidenceObject[]
  busyId: string | null
  onApprove: (id: string) => void
  onReject: (id: string) => void
}) {
  const [why, setWhy] = useState(false)
  // Confirm-before-acting gate: clicking Approve/Reject never fires the API call directly. It swaps
  // the buttons for a plain-text warning with explicit Yes/Cancel first -
  // irreversible actions on a real ops tool need a deliberate second step, not a single fat-finger tap.
  const [pendingChoice, setPendingChoice] = useState<'approve' | 'reject' | null>(null)
  const action = decision.action
  const tone = riskTone(action?.risk_tier)
  const status = (decision.status ?? 'pending').toLowerCase()
  const pending = status === 'pending'
  const busy = busyId === decision.id
  const atRisk = moneyAtRisk(evidence)
  const actionLabel = describeAction(action)

  const confirmText =
    pendingChoice === 'approve'
      ? `This action cannot be undone. "${actionLabel}" will be applied now.`
      : pendingChoice === 'reject'
        ? 'This action cannot be undone. This recommendation will leave the queue.'
        : null

  return (
    <article className="decision-card" style={{ '--rail': toneVar[tone] } as CSSProperties}>
      <header className="dc-head">
        <div>
          <div className="dc-agency">{agencyForAction(action)}</div>
          <h3>{actionLabel}</h3>
        </div>
        <StatusGlyph tone={pending ? tone : statusTone(status)} label={pending ? `${action?.risk_tier ?? 'low'} risk` : formatLabel(status)} />
      </header>
      <p className="dc-why">{whyLine(decision, evidence)}</p>
      {atRisk ? (
        <p className="dc-risk">
          <span className="tnum">{atRisk}</span> at risk
        </p>
      ) : null}

      {pending ? (
        <>
          {pendingChoice && confirmText ? (
            <div className="dc-confirm show">
              <p className="dc-confirm-msg">{confirmText}</p>
              <div className="dc-confirm-actions">
                <button
                  className={`confirm-yes ${pendingChoice === 'reject' ? 'reject-tone' : ''}`}
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (pendingChoice === 'approve') onApprove(decision.id!)
                    else if (pendingChoice === 'reject') onReject(decision.id!)
                  }}
                >
                  {busy ? 'Working...' : pendingChoice === 'approve' ? 'Yes, apply it' : 'Yes, reject it'}
                </button>
                <button className="confirm-cancel" type="button" onClick={() => setPendingChoice(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="dc-actions">
              <button className="btn btn-primary" type="button" disabled={busy} onClick={() => setPendingChoice('approve')}>
                Approve
              </button>
              <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => setPendingChoice('reject')}>
                Reject
              </button>
              {evidence && evidence.length > 0 ? (
                <button className="btn btn-ghost why-toggle" type="button" aria-expanded={why} onClick={() => setWhy((v) => !v)}>
                  <span>Why?</span>
                  <span className={`why-caret ${why ? 'is-open' : ''}`} aria-hidden />
                </button>
              ) : null}
            </div>
          )}
        </>
      ) : (
        <div className="dc-actions">
          <span className={`dc-resolved tone-${statusTone(status)}`}>{formatLabel(status)}</span>
          {evidence && evidence.length > 0 ? (
            <button className="btn btn-ghost why-toggle" type="button" aria-expanded={why} onClick={() => setWhy((v) => !v)}>
              <span>Why?</span>
              <span className={`why-caret ${why ? 'is-open' : ''}`} aria-hidden />
            </button>
          ) : null}
        </div>
      )}
      {why && evidence ? <Reasoning evidence={evidence} /> : null}
    </article>
  )
}

// ---------------------------------------------------------------------------
// The persistent approval queue: a status bar (always visible) + a slide-down panel with the
// flat pending list and the day-grouped receipt timeline.
// ---------------------------------------------------------------------------
function StatusBar({ queue, open, onToggle }: { queue: Decision[]; open: boolean; onToggle: () => void }) {
  const top = queue[0]
  const tone: Tone = top ? riskTone(top.action?.risk_tier) : 'ok'
  const more = queue.length - 1
  const statusLabel = top ? describeAction(top.action) : 'Queue clear'
  const statusMeta = top ? (top.action?.risk_tier ?? 'low') : 'clear'
  const a11yLabel = top
    ? `Approval queue: ${statusLabel}, ${statusMeta} risk${more > 0 ? `, ${more} more waiting` : ''}.`
    : 'Approval queue: clear.'
  return (
    <button
      className={`statusbar ${open ? 'is-open' : ''}`}
      type="button"
      aria-controls="approval-panel"
      aria-expanded={open}
      aria-label={a11yLabel}
      onClick={onToggle}
    >
      <span className="statusbar-accent" style={{ background: toneVar[tone] }} aria-hidden />
      <span className="statusbar-main">
        <span className="statusbar-label">{statusLabel}</span>
        <span className="statusbar-sub">
          <span className={`tag tone-${tone}`}>{statusMeta}</span>
          {more > 0 ? <span>{more} more waiting</span> : null}
        </span>
      </span>
      <svg className="chevron" viewBox="0 0 10 6" fill="none" aria-hidden>
        <path d="M1 1L5 5L9 1" stroke="currentColor" strokeWidth="1.4" />
      </svg>
    </button>
  )
}

function ReceiptRow({ decision }: { decision: Decision }) {
  const [open, setOpen] = useState(false)
  const tone = statusTone(decision.status)
  const label = `${formatLabel(decision.status)} - ${describeAction(decision.action)}`
  return (
    <li className={`receipt ${open ? 'is-open' : ''}`}>
      <button className="receipt-toggle" type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className={`receipt-dot tone-${tone}`} aria-hidden />
        <span className="receipt-txt">{label}</span>
        <span className="receipt-time tnum">{decisionTime(decision)}</span>
      </button>
      {open ? <div className="receipt-detail">{receiptDetail(decision)}</div> : null}
    </li>
  )
}

function ApprovalPanel({
  queue,
  resolved,
  currentId,
  evidence,
  busyId,
  onApprove,
  onReject,
}: {
  queue: Decision[]
  resolved: Decision[]
  currentId?: string
  evidence?: EvidenceObject[]
  busyId: string | null
  onApprove: (id: string) => void
  onReject: (id: string) => void
}) {
  const evidenceFor = (d: Decision) => (d.id && d.id === currentId ? evidence : undefined)

  const days = new Map<string, Decision[]>()
  for (const d of sortByTimeDesc(resolved)) {
    const key = dayLabel(d.updated_at ?? d.created_at)
    days.set(key, [...(days.get(key) ?? []), d])
  }

  return (
    <div className="approval-panel-scroll">
      <div className="section-heading">Needs your approval</div>
      {queue.length === 0 ? (
        <p className="stack-clear">Queue clear. Nothing waiting.</p>
      ) : (
        queue.map((d) => (
          <DecisionCard key={d.id} decision={d} evidence={evidenceFor(d)} busyId={busyId} onApprove={onApprove} onReject={onReject} />
        ))
      )}

      {Array.from(days.entries()).map(([day, items]) => (
        <div key={day}>
          <div className="day-marker">{day}</div>
          <ol className="receipt-list">
            {items.map((d) => (
              <ReceiptRow key={d.id ?? d.summary} decision={d} />
            ))}
          </ol>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Chat message bubbles: text only. Decisions live in the status bar/panel, never here.
// ---------------------------------------------------------------------------
function AssistantBubble({ text }: { text: string }) {
  return (
    <div className="row assistant-row">
      <div className="avatar" aria-hidden>
        <span className="avatar-mark" />
      </div>
      <div className="bubble assistant">
        <p>{text}</p>
      </div>
    </div>
  )
}
function UserBubble({ text }: { text: string }) {
  return (
    <div className="row user-row">
      <div className="bubble user">{text}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sidebar - the chat-first access surface. Modeled on ChatGPT/Claude/Codex:
// three zones (create+find · continuity+surfaces · identity). Nothing here is a
// data display; every row is an ENTRY POINT that never blocks the chat. Persistent
// on desktop, an overlay on mobile. High-scale product/lot surfaces open in the
// main workspace; only small utility pages stay in the sidebar stack.
// ---------------------------------------------------------------------------
type SidebarPage = 'settings'
type WorkspaceSurface =
  | 'products'
  | 'to-order'
  | 'sell-first'
  | 'deliveries'
  | 'cold-chain'
  | 'connections'
  | 'operations'
  | 'results'
type Recent = { id: string; title: string; active?: boolean }

const PAGE_TITLE: Record<SidebarPage, string> = {
  settings: 'Settings',
}

const CONNECTOR_SYSTEMS = [
  { label: 'Odoo', transport: 'poll' },
  { label: 'SAP', transport: 'poll' },
  { label: 'SYSPRO', transport: 'poll' },
  { label: 'Square', transport: 'webhook' },
  { label: 'Shopify', transport: 'webhook' },
  { label: 'Lightspeed', transport: 'webhook/poll' },
  { label: 'CSV', transport: 'file' },
]

const OPERATION_READ_ENDPOINTS = [
  { label: 'Health', method: 'GET', path: '/health', detail: 'Liveness and public inference config.' },
  { label: 'Readiness', method: 'GET', path: '/readiness', detail: 'Backend checks, stores, worker, auth mode, and seed data.' },
  { label: 'Inference config', method: 'GET', path: '/inference/config', detail: 'Provider, routine model, strong model, and credential presence flags.' },
  { label: 'Inference readiness', method: 'GET', path: '/inference/readiness', detail: 'Live AMD MI300X/vLLM or Fireworks readiness checks and next step.' },
  { label: 'Submission readiness', method: 'GET', path: '/submission/readiness', detail: 'Hackathon submission prescreen checks and AMD compute proof.' },
  { label: 'Seed summary', method: 'GET', path: '/data/seed/summary', detail: 'Current seeded store scenario and focus product.' },
  { label: 'Product attention', method: 'GET', path: '/products/attention', detail: 'Bounded product groups that need action.' },
  { label: 'Product search', method: 'GET', path: '/products/search', detail: 'Bounded search-first catalogue lookup.' },
  { label: 'Decisions', method: 'GET', path: '/decisions', detail: 'Decision ledger for approvals, history, and outcomes.' },
  { label: 'Decision detail', method: 'GET', path: '/decisions/{decision_id}', detail: 'Parameterized decision record behind approval rows.' },
  { label: 'Learning', method: 'GET', path: '/learning', detail: 'Outcome learning thresholds and learning events.' },
  { label: 'Write-back tasks', method: 'GET', path: '/writeback/tasks', detail: 'Task-only write-back queue.' },
  { label: 'Events', method: 'GET', path: '/events', detail: 'Persisted canonical event log.' },
  { label: 'Event bus', method: 'GET', path: '/events/bus', detail: 'Buffered event bus messages.' },
  { label: 'Trace detail', method: 'GET', path: '/trace/{correlation_id}', detail: 'Parameterized cascade trace detail.' },
  { label: 'Traces', method: 'GET', path: '/traces', detail: 'Recorded cascade traces and evidence agents.' },
  { label: 'Root-cause analysis', method: 'GET', path: '/detective/root-cause/{target_id}', detail: 'Parameterized root-cause traversal.' },
  { label: 'Root-cause SQL', method: 'GET', path: '/detective/root-cause-sql', detail: 'Root-cause SQL template used by the detective surface.' },
  { label: 'Platform tools', method: 'GET', path: '/tools/platform', detail: 'Read-only tool catalogue exposed to agents.' },
  { label: 'Platform audit', method: 'GET', path: '/tools/platform/audit', detail: 'Tool-call audit events.' },
  { label: 'Cold-chain feed', method: 'GET', path: '/cold-chain/feed', detail: 'Cold-chain status and buffered feed events.' },
  { label: 'Worldgen runs', method: 'GET', path: '/demo/worldgen-runs', detail: 'Synthetic drill run history.' },
  { label: 'Worldgen run detail', method: 'GET', path: '/demo/worldgen-runs/{run_id}', detail: 'Parameterized synthetic drill run detail.' },
  { label: 'Tenant profile', method: 'GET', path: '/tenants/me', detail: 'Current tenant/store profile and connector policy.' },
  { label: 'Connector catalogue', method: 'GET', path: '/connectors/systems', detail: 'Supported source-system connector capabilities.' },
  { label: 'Tenant connectors', method: 'GET', path: '/connectors/me', detail: 'Connector status for the current tenant.' },
  { label: 'Inbound records', method: 'GET', path: '/connectors/inbound-records', detail: 'Connector intake and validation records.' },
  { label: 'Model runs', method: 'GET', path: '/mlops/model-runs', detail: 'Inference run ledger.' },
  { label: 'Prompt versions', method: 'GET', path: '/mlops/prompts', detail: 'Prompt registry without raw prompt leakage.' },
  { label: 'Accountability', method: 'GET', path: '/mlops/accountability', detail: 'Recovered value, cost, and model accountability report.' },
  { label: 'Observability', method: 'GET', path: '/mlops/observability', detail: 'Tenant decision, connector, model, worker, and learning snapshot.' },
  { label: 'Tenant facts', method: 'GET', path: '/mlops/tenant-facts', detail: 'Governed learning facts.' },
  { label: 'Worker status', method: 'GET', path: '/worker/status', detail: 'Background worker runtime state.' },
  { label: 'Worker runs', method: 'GET', path: '/worker/runs', detail: 'Background cascade processing journal.' },
  { label: 'Catalog products', method: 'GET', path: '/catalog/products', detail: 'Product-identity master list for the tenant.' },
  { label: 'Catalog product detail', method: 'GET', path: '/catalog/products/{product_id}', detail: 'Parameterized product-identity record.' },
  { label: 'Catalog variants', method: 'GET', path: '/catalog/products/{product_id}/variants', detail: 'Sellable variants (pack size, unit) for a product.' },
  { label: 'Catalog resolve', method: 'GET', path: '/catalog/resolve', detail: 'Resolve a GTIN/barcode/SKU/PLU/source id to its variant.' },
]

const GATED_ENDPOINTS = [
  { label: 'Chat stream', method: 'POST', path: '/chat', group: 'operations', detail: 'Composer-backed ShelfWise chat; API-key gated when configured.' },
  { label: 'Connector intake', method: 'POST', path: '/connectors/{system}/intake', group: 'connections', detail: 'Webhook/poll payload intake; API-key and role gated.' },
  { label: 'Event ingest', method: 'POST', path: '/ingest', group: 'operations', detail: 'Canonical event ingest; validates tenant and source payloads.' },
  { label: 'Barcode scan', method: 'POST', path: '/scan/barcode', group: 'connections', detail: 'Multimodal SKU lookup from barcode input.' },
  { label: 'Image scan', method: 'POST', path: '/scan/image', group: 'connections', detail: 'Shelf/image extraction path for SKU and expiry candidates.' },
  { label: 'Receipt scan', method: 'POST', path: '/scan/receipt', group: 'connections', detail: 'Receipt line intake for POS evidence.' },
  { label: 'Voice in', method: 'POST', path: '/voice/in', group: 'connections', detail: 'Speech-to-command intake.' },
  { label: 'Voice out', method: 'POST', path: '/voice/out', group: 'connections', detail: 'Text-to-speech response output.' },
  { label: 'FEFO split', method: 'POST', path: '/intelligence/stock/fefo-split', group: 'intelligence', detail: 'Lot-level sell-first calculation.' },
  { label: 'Delivery reconcile', method: 'POST', path: '/intelligence/deliveries/reconcile', group: 'intelligence', detail: 'ASN, receipt, rejection, and short-date reconciliation.' },
  { label: 'Supplier cover', method: 'POST', path: '/intelligence/suppliers/cover-plan', group: 'intelligence', detail: 'Stock cover and transfer/order recommendation.' },
  { label: 'Outcome summary', method: 'POST', path: '/intelligence/outcomes/summarize', group: 'intelligence', detail: 'Post-decision learning summary.' },
  { label: 'Approval approve', method: 'POST', path: '/decisions/{decision_id}/approve', group: 'operations', detail: 'Human approval transition and task-only write-back.' },
  { label: 'Approval reject', method: 'POST', path: '/decisions/{decision_id}/reject', group: 'operations', detail: 'Human rejection transition.' },
  { label: 'Tenant profile write', method: 'POST', path: '/tenants/me', group: 'operations', detail: 'Owner-only profile and connector policy update.' },
  { label: 'Worker process one', method: 'POST', path: '/worker/process-one', group: 'operations', detail: 'Manual worker execution; role and API-key gated.' },
  { label: 'Memory consolidation', method: 'POST', path: '/mlops/consolidate-memory', group: 'operations', detail: 'Governed learning fact consolidation.' },
  { label: 'Inference smoke', method: 'GET', path: '/inference/smoke', group: 'operations', detail: 'Manual inference smoke test; records a model run.' },
  { label: 'Trace detail', method: 'GET', path: '/trace/{correlation_id}', group: 'operations', detail: 'Parameterized trace detail from the trace registry.' },
  { label: 'Root-cause analysis', method: 'GET', path: '/detective/root-cause/{target_id}', group: 'operations', detail: 'Parameterized root-cause traversal for decisions/events.' },
  { label: 'Golden demo', method: 'GET/POST', path: '/demo/golden', group: 'operations', detail: 'Demo cascade endpoint used by smoke and runbook flows.' },
  { label: 'Procurement demo', method: 'GET/POST', path: '/demo/procurement', group: 'operations', detail: 'Scenario endpoint that persists a procurement decision.' },
  { label: 'Sales demo', method: 'GET/POST', path: '/demo/sales', group: 'operations', detail: 'Scenario endpoint that persists a POS decision.' },
  { label: 'Cold-chain demo', method: 'GET/POST', path: '/demo/cold-chain', group: 'operations', detail: 'Scenario endpoint that persists a facilities decision.' },
  { label: 'Critic rejection demo', method: 'GET', path: '/demo/critic-rejection', group: 'operations', detail: 'Scenario endpoint for the critic-rejection path.' },
  { label: 'Worldgen run detail', method: 'GET', path: '/demo/worldgen-runs/{run_id}', group: 'operations', detail: 'Parameterized synthetic drill run detail.' },
  { label: 'Worldgen drill', method: 'GET', path: '/demo/worldgen/{scenario_id}', group: 'operations', detail: 'Synthetic scenario execution; not auto-run from the sidebar.' },
  { label: 'Catalog product upsert', method: 'POST', path: '/catalog/products', group: 'intelligence', detail: 'Create or update a product-identity record.' },
  { label: 'Catalog variant upsert', method: 'POST', path: '/catalog/products/{product_id}/variants', group: 'intelligence', detail: 'Create or update a sellable variant of a product.' },
  { label: 'Catalog identifier upsert', method: 'POST', path: '/catalog/identifiers', group: 'intelligence', detail: 'Map a GTIN/barcode/SKU/PLU/source id to a variant; rejects conflicting remaps.' },
]

function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path d="M8 3v10M3 8h10" strokeLinecap="round" />
    </svg>
  )
}
function SearchIcon() {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <circle cx="7" cy="7" r="4.3" />
      <path d="M10.4 10.4L14 14" strokeLinecap="round" />
    </svg>
  )
}

function NavRow({
  label,
  sku,
  value,
  tone,
  active,
  onOpen,
}: {
  label: string
  sku?: string
  value?: string
  tone?: Tone
  active?: boolean
  onOpen: () => void
}) {
  return (
    <button className={`nav-row${active ? ' is-active' : ''}`} type="button" onClick={onOpen}>
      <span className="nav-label">
        {label}
        {sku ? <small>{humanizeOperationalText(sku)}</small> : null}
      </span>
      {value ? <span className={`nav-value tnum${tone ? ` tone-${tone}` : ''}`}>{value}</span> : null}
      <span className="nav-chevron" aria-hidden />
    </button>
  )
}

/** "1.2 days" from the backend's decimal-string days-of-supply; null when the data is absent. */
function daysNumber(value: unknown): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? Math.round(n * 10) / 10 : null
}

/** Theme control lives in Settings now (moved off the top bar), like every reference product. */
function ThemeRow() {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  return (
    <button
      className="set-row"
      type="button"
      onClick={() => {
        const next: Theme = theme === 'dark' ? 'light' : 'dark'
        applyTheme(next)
        setTheme(next)
      }}
    >
      <span className="set-label">Appearance</span>
      <span className="set-value">{theme === 'dark' ? 'Dark' : 'Light'}</span>
    </button>
  )
}

function inferenceProviderLabel(config?: InferenceConfig | null): string {
  if (!config?.provider) return 'Unknown'
  if (config.provider === 'vllm_mi300x') return 'AMD vLLM'
  if (config.provider === 'fireworks') return 'Fireworks'
  return 'Offline'
}

function inferenceTone(config?: InferenceConfig | null): Tone {
  if (config?.provider === 'vllm_mi300x') return 'ok'
  if (config?.provider === 'fireworks') return 'info'
  return 'mute'
}

/** Top-bar badge: which inference backend answered this snapshot (AMD vLLM, Fireworks, offline). */
function InferencePill({ config }: { config?: InferenceConfig | null }) {
  const label = inferenceProviderLabel(config)
  const tone = inferenceTone(config)
  const host = config?.base_url_host
  const title = config?.provider === 'vllm_mi300x'
    ? `AMD Developer Cloud / vLLM${host ? ` via ${host}` : ''}`
    : config?.provider === 'fireworks'
      ? 'Fireworks fallback configured'
      : 'Deterministic offline mode; set LLM_BASE_URL before the MI300X demo'
  return (
    <span className={`inference-pill tone-${tone}`} title={title}>
      <span className="inference-pill-label">{label}</span>
      {config?.accelerator ? <span className="inference-pill-detail">{config.accelerator}</span> : null}
    </span>
  )
}

function Sidebar({
  open,
  onClose,
  onSelectRecent,
  onNewChat,
  onOpenApprovals,
  onOpenWorkspace,
  activeWorkspace,
  recents,
  queue,
  data,
  seed,
  ops,
  recoveredToday,
}: {
  open: boolean
  onClose: () => void
  onSelectRecent: () => void
  onNewChat: () => void
  onOpenApprovals: () => void
  onOpenWorkspace: (surface: WorkspaceSurface, options?: WorkspaceOpenOptions) => void
  activeWorkspace: WorkspaceSurface | null
  recents: Recent[]
  queue: Decision[]
  data: GoldenDemo | null
  seed: SeedSummary | null
  ops: OperationalSnapshot
  recoveredToday: string | null
}) {
  const intel = data?.store_intelligence
  const cover = intel?.supplier_cover

  // Identity zone - real fields, no fabrication: role routed by the backend, store from the seed.
  // A person's name fills in when company-account login lands (see roadmap).
  const role = data?.decision?.role ? formatLabel(data.decision.role) : 'Store manager'
  const store = seed?.location ? formatLabel(seed.location) : 'Store'
  const monogram = role.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()
  const tenantProfile = ops.tenantProfile
  const connectorPolicy = asObject(tenantProfile.connector_policy)
  const allowedSystems = asArray<string>(connectorPolicy.allowed_systems)
  const policyMode = fieldText(connectorPolicy, 'mode', 'read only')
  const writeBackMode = fieldText(connectorPolicy, 'write_back', 'HITL required')
  const allowedSystemText = allowedSystems.length ? allowedSystems.map(formatLabel).join(', ') : 'CSV'

  // This list is attention-only. It supports sidebar search without turning the rail into inventory.
  const coverDays = daysNumber(cover?.days_of_supply)
  const apiAttentionProducts = asArray<ProductCatalogItem>(ops.productAttention.items)
  const fallbackAttentionProducts: ProductCatalogItem[] = []
  if (seed) {
    const low = seed.units_on_hand != null && seed.reorder_point != null && seed.units_on_hand <= seed.reorder_point
    const expiring = seed.days_to_expiry != null && seed.days_to_expiry <= 7
    fallbackAttentionProducts.push({
      sku: seed.sku,
      name: seed.product_name ?? humanizeOperationalText(seed.sku ?? 'Product'),
      category: seed.category,
      supplier: seed.supplier,
      on_hand: seed.units_on_hand,
      reorder_point: seed.reorder_point,
      days_to_expiry: seed.days_to_expiry,
      requires_attention: low || expiring,
      attention_reasons: [expiring ? 'expiring' : null, low ? 'low_stock' : null].filter(Boolean) as string[],
      attention_summary: [
        seed.days_to_expiry != null ? `${seed.days_to_expiry} days to expiry` : null,
        seed.units_on_hand != null ? `${seed.units_on_hand} on hand` : null,
      ].filter(Boolean).join(' · '),
    })
  }
  if (cover?.sku) {
    fallbackAttentionProducts.push({
      sku: cover.sku,
      name: humanizeOperationalText(cover.sku),
      category: 'Replenishment risk',
      on_hand: cover.units_on_hand,
      requires_attention: true,
      attention_reasons: ['low_stock'],
      attention_summary: cover.conclusion ?? (coverDays != null ? `${coverDays} days left` : undefined),
    })
  }
  const products = apiAttentionProducts.length ? apiAttentionProducts : fallbackAttentionProducts.filter((item) => item.requires_attention)
  const productTotals = asObject(ops.productAttention.totals)
  const apiToOrderCount = fieldNumber(productTotals, 'to_order_products')
  const apiSellFirstCount = fieldNumber(productTotals, 'sell_first_products')
  // The shopping list: products that will run out before a normal order can arrive.
  const orderCount = apiToOrderCount || (cover?.transfer_units_recommended ? 1 : 0)
  const sellFirstUnits = Number(intel?.batch_split?.priority_sell_units ?? 0)
  const sellFirstProducts = apiSellFirstCount || (intel?.batch_split && sellFirstUnits > 0 ? 1 : 0)
  const deliveryShortUnits = Number(intel?.delivery_reconciliation?.missing_units ?? 0)
  const deliveryIssues = deliveryShortUnits > 0 ? 1 : 0
  const coldRunning = ops.coldChainStatus.running === true
  const coldEnabled = ops.coldChainStatus.enabled === true
  const coldChainValue = data?.scenario === 'cold_chain' || ops.coldChainEvents.length ? 'review' : coldRunning ? 'live' : coldEnabled ? 'armed' : 'clear'
  const operationsValue = data ? 'live' : 'loading'
  const connectorRows = ops.tenantConnectors.length || ops.connectorSystems.length
  const connectorCount = connectorRows || CONNECTOR_SYSTEMS.length
  // Page stack is intentionally tiny: settings lives in the sidebar; operational detail opens
  // in the main workspace so large product/lot queues never fight the rail.
  const [stack, setStack] = useState<SidebarPage[]>([])
  const page = stack[stack.length - 1] ?? null
  const push = (p: SidebarPage) => setStack((s) => [...s, p])
  const back = () => setStack((s) => s.slice(0, -1))
  const openWorkspace = (surface: WorkspaceSurface, options?: WorkspaceOpenOptions) => {
    setStack([])
    setSearching(false)
    onOpenWorkspace(surface, options)
  }
  const backRef = useRef<HTMLButtonElement | null>(null)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) {
      setStack([])
      setSearching(false)
      setQuery('')
    }
  }, [open])
  useEffect(() => {
    if (page) backRef.current?.focus()
  }, [page])

  const shownRecents = recents.filter((r) => !query || r.title.toLowerCase().includes(query.toLowerCase()))
  const shownAttentionProducts = products.filter((p) => {
    const q = query.toLowerCase()
    const haystack = [p.name, p.sku, p.category, p.subcategory, p.supplier, p.attention_summary].filter(Boolean).join(' ').toLowerCase()
    return !q || haystack.includes(q)
  })
  const shownQueue = queue.filter((d) => {
    if (!query) return true
    const haystack = [describeAction(d.action), d.summary, d.role].filter(Boolean).join(' ').toLowerCase()
    return haystack.includes(query.toLowerCase())
  })

  return (
    <>
      <aside className={`sidebar ${open ? 'is-open' : 'is-collapsed'}`} aria-label="Navigation" aria-hidden={!open}>
        <div className="sidebar-inner">
          <div className="sidebar-head">
            {page ? (
              <button className="drawer-back" type="button" ref={backRef} onClick={back}>
                <span className="nav-chevron back" aria-hidden />
                {PAGE_TITLE[page]}
              </button>
            ) : (
              <span className="brand">
                <span className="brand-mark" aria-hidden />
                <span className="brand-name">ShelfWise</span>
              </span>
            )}
            <button className="icon-btn" type="button" aria-label="Close sidebar" onClick={onClose}>
              <UiIcon name="close" />
            </button>
          </div>

          <div className="sidebar-body">
            {/* ROOT - create + find, then continuity + operating surfaces. */}
            {page == null ? (
              <>
                <div className="sidebar-actions">
                  <button className="side-action" type="button" onClick={onNewChat}>
                    <PlusIcon />
                    New chat
                  </button>
                  <button className="side-action" type="button" aria-expanded={searching} onClick={() => setSearching((v) => !v)}>
                    <SearchIcon />
                    Search
                  </button>
                </div>

                <div className="sidebar-section">
                  <div className="sidebar-kicker">Recents</div>
                  {searching ? (
                    <input
                      className="side-search"
                      autoFocus
                      placeholder="Search conversations and attention queues..."
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      aria-label="Search conversations and attention queues"
                    />
                  ) : null}
                  {searching ? (
                    <div className="search-groups">
                      <div className="search-group">
                        <div className="sidebar-kicker mini">Conversations</div>
                        {shownRecents.length ? (
                          shownRecents.map((r) => (
                            <button key={r.id} className={`recent-row ${r.active ? 'is-active' : ''}`} type="button" onClick={onSelectRecent}>
                              {r.title}
                            </button>
                          ))
                        ) : (
                          <p className="side-empty">No conversations match.</p>
                        )}
                      </div>
                      <div className="search-group">
                        <div className="sidebar-kicker mini">Attention products</div>
                        {shownAttentionProducts.length ? (
                          shownAttentionProducts.map((p, index) => (
                            <NavRow
                              key={productKey(p, index)}
                              label={productTitle(p)}
                              sku={productMeta(p)}
                              value={productValue(p)}
                              tone={productTone(p)}
                              onOpen={() => openWorkspace('products', { query: query || productTitle(p) })}
                            />
                          ))
                        ) : (
                          <p className="side-empty">No attention products match.</p>
                        )}
                        <button className="recent-row" type="button" onClick={() => openWorkspace('products', { query })}>
                          {query ? `Search catalogue for "${query}"` : 'Open product catalogue'}
                        </button>
                      </div>
                      <div className="search-group">
                        <div className="sidebar-kicker mini">Decisions</div>
                        {shownQueue.length ? (
                          shownQueue.map((d) => (
                            <button key={d.id ?? describeAction(d.action)} className="recent-row" type="button" onClick={onOpenApprovals}>
                              {describeAction(d.action)}
                            </button>
                          ))
                        ) : (
                          <p className="side-empty">No decisions match.</p>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="recents-list">
                      {shownRecents.length ? (
                      shownRecents.map((r) => (
                        <button key={r.id} className={`recent-row ${r.active ? 'is-active' : ''}`} type="button" onClick={onSelectRecent}>
                          {r.title}
                        </button>
                      ))
                      ) : (
                        <p className="side-empty">No conversations match.</p>
                      )}
                    </div>
                  )}
                </div>

                <div className="sidebar-section">
                  <div className="sidebar-kicker">Needs attention</div>
                  <NavRow label="Approvals" value={queue.length ? `${queue.length} waiting` : 'clear'} tone={queue.length ? 'warn' : undefined} onOpen={onOpenApprovals} />
                  <NavRow
                    label="To order"
                    value={orderCount ? `${orderCount} product${orderCount === 1 ? '' : 's'}` : 'clear'}
                    tone={orderCount ? 'warn' : undefined}
                    active={activeWorkspace === 'to-order'}
                    onOpen={() => openWorkspace('to-order')}
                  />
                  <NavRow
                    label="Sell first"
                    value={sellFirstProducts ? `${sellFirstProducts} product${sellFirstProducts === 1 ? '' : 's'}` : 'clear'}
                    tone={sellFirstProducts ? 'warn' : undefined}
                    active={activeWorkspace === 'sell-first'}
                    onOpen={() => openWorkspace('sell-first')}
                  />
                  <NavRow
                    label="Deliveries"
                    value={deliveryIssues ? `${deliveryIssues} issue${deliveryIssues === 1 ? '' : 's'}` : 'clear'}
                    tone={deliveryIssues ? 'warn' : undefined}
                    active={activeWorkspace === 'deliveries'}
                    onOpen={() => openWorkspace('deliveries')}
                  />
                  <NavRow
                    label="Cold chain"
                    value={coldChainValue}
                    tone={coldChainValue === 'review' ? 'risk' : undefined}
                    active={activeWorkspace === 'cold-chain'}
                    onOpen={() => openWorkspace('cold-chain')}
                  />
                </div>

                <div className="sidebar-section">
                  <div className="sidebar-kicker">My store</div>
                  <NavRow
                    label="Products"
                    value="search"
                    active={activeWorkspace === 'products'}
                    onOpen={() => openWorkspace('products')}
                  />
                  <NavRow
                    label="Today's results"
                    value={recoveredToday ?? 'R0'}
                    tone={recoveredToday ? 'ok' : undefined}
                    active={activeWorkspace === 'results'}
                    onOpen={() => openWorkspace('results')}
                  />
                </div>

                <div className="sidebar-section">
                  <div className="sidebar-kicker">System</div>
                  <NavRow
                    label="Connections"
                    value={`${connectorCount} systems`}
                    active={activeWorkspace === 'connections'}
                    onOpen={() => openWorkspace('connections')}
                  />
                  <NavRow
                    label="Operations"
                    value={operationsValue}
                    tone={operationsValue === 'live' ? 'ok' : undefined}
                    active={activeWorkspace === 'operations'}
                    onOpen={() => openWorkspace('operations')}
                  />
                </div>
              </>
            ) : null}

            {/* SETTINGS - behind the profile chip: appearance + identity, nothing internal */}
            {page === 'settings' ? (
              <section className="rail-section">
                <ThemeRow />
                <dl className="kv">
                  <div>
                    <dt>Store</dt>
                    <dd className="tnum">{store}</dd>
                  </div>
                  <div>
                    <dt>Role</dt>
                    <dd className="tnum">{role}</dd>
                  </div>
                  <div>
                    <dt>Profile</dt>
                    <dd className="tnum">{fieldText(tenantProfile, 'name', store)}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd className="tnum">{fieldText(tenantProfile, 'status', 'active')}</dd>
                  </div>
                  <div>
                    <dt>Tenant</dt>
                    <dd className="tnum">{fieldText(tenantProfile, 'tenant_id', 'sa_retail_demo')}</dd>
                  </div>
                  <div>
                    <dt>Currency</dt>
                    <dd className="tnum">{fieldText(tenantProfile, 'currency', 'ZAR')}</dd>
                  </div>
                  <div>
                    <dt>Timezone</dt>
                    <dd className="tnum">{fieldText(tenantProfile, 'timezone', 'Africa/Johannesburg')}</dd>
                  </div>
                  <div>
                    <dt>Write-back</dt>
                    <dd className="tnum">{writeBackMode}</dd>
                  </div>
                </dl>
                <p className="muted">
                  Connector policy: {policyMode}; allowed systems: {allowedSystemText}.
                </p>
              </section>
            ) : null}
          </div>

          {/* IDENTITY - pinned bottom, opens Settings (the ChatGPT/Codex profile-chip pattern) */}
          {page == null ? (
            <div className="sidebar-foot">
              <button className="profile-chip" type="button" onClick={() => push('settings')}>
                <span className="chip-avatar" aria-hidden>{monogram}</span>
                <span className="chip-id">
                  <span className="chip-name">{role}</span>
                  <span className="chip-sub">{store}</span>
                </span>
                <span className="nav-chevron" aria-hidden />
              </button>
            </div>
          ) : null}
        </div>
      </aside>
      <div className={`sidebar-scrim ${open ? 'is-open' : ''}`} onClick={onClose} aria-hidden />
    </>
  )
}

type WorkspaceRowProps = {
  label: string
  meta?: string
  detail?: string
  value?: string
  tone?: Tone
  active?: boolean
  onSelect?: () => void
}

function pluralLabel(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`
}

function WorkspaceRow({ label, meta, detail, value, tone, active, onSelect }: WorkspaceRowProps) {
  const className = `workspace-row${onSelect ? ' is-action' : ''}${active ? ' is-active' : ''}`
  const content = (
    <>
      <div className="workspace-row-main">
        <span>{label}</span>
        {meta ? <small>{humanizeOperationalText(meta)}</small> : null}
        {detail ? <p>{humanizeOperationalText(detail)}</p> : null}
      </div>
      {value ? <span className={`workspace-row-value tnum${tone ? ` tone-${tone}` : ''}`}>{value}</span> : null}
    </>
  )
  if (onSelect) {
    return (
      <button className={className} type="button" onClick={onSelect}>
        {content}
      </button>
    )
  }
  return (
    <div className={className}>
      {content}
    </div>
  )
}

function WorkspaceMetric({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className="workspace-metric">
      <span>{label}</span>
      <strong className={`tnum${tone ? ` tone-${tone}` : ''}`}>{value}</strong>
    </div>
  )
}

function WorkspaceSection({
  title,
  count,
  children,
}: {
  title: string
  count?: string
  children: ReactNode
}) {
  return (
    <section className="workspace-section">
      <div className="workspace-section-head">
        <h2>{title}</h2>
        {count ? <span className="tnum">{count}</span> : null}
      </div>
      {children}
    </section>
  )
}

function WorkspaceEmpty({ children }: { children: ReactNode }) {
  return <p className="workspace-empty">{children}</p>
}

function batchExpiryText(batch: FefoBatch): string {
  const d = batch.days_to_expiry
  if (d == null) return 'No expiry date'
  if (d <= 0) return 'Expires today'
  if (d === 1) return 'Expires tomorrow'
  return `Expires in ${d} days`
}

function productKey(item: ProductCatalogItem, index: number): string {
  return String(item.sku ?? item.product_id ?? item.barcode ?? item.name ?? index)
}

function productTitle(item: ProductCatalogItem): string {
  return humanizeOperationalText(item.name ?? item.receipt_name ?? item.sku ?? item.product_id ?? 'Product')
}

function productMeta(item: ProductCatalogItem): string {
  return [item.category, item.subcategory, item.supplier].filter(Boolean).join(' · ')
}

function productDetail(item: ProductCatalogItem): string {
  const details = [
    item.attention_summary,
    item.sku ? `SKU ${item.sku}` : null,
    item.barcode ? `Barcode ${item.barcode}` : null,
    item.lot_count ? `${item.lot_count} lots` : null,
    item.shelf_location ? `Shelf ${item.shelf_location}` : null,
    item.storage_requirements,
  ].filter(Boolean)
  return details.join(' · ')
}

function productValue(item: ProductCatalogItem): string {
  if (item.sell_first_units && item.sell_first_units > 0) return `${formatValue(item.sell_first_units)} sell first`
  if (item.on_hand != null) return `${formatValue(item.on_hand)} on hand`
  const money = formatMoneyish(item.price)
  return money ?? formatLabel(item.source ?? 'catalogue')
}

function productTone(item: ProductCatalogItem): Tone | undefined {
  const reasons = item.attention_reasons ?? []
  if (reasons.includes('sell_first') || reasons.includes('expiring')) return 'warn'
  if (reasons.includes('low_stock')) return 'info'
  return item.requires_attention ? 'warn' : undefined
}

function productRows(items: ProductCatalogItem[]): WorkspaceRowProps[] {
  return items.map((item) => ({
    label: productTitle(item),
    meta: productMeta(item),
    detail: productDetail(item),
    value: productValue(item),
    tone: productTone(item),
  }))
}

function workspaceCopy(surface: WorkspaceSurface): { title: string; kicker: string; status: string; subtitle: string } {
  switch (surface) {
    case 'products':
      return {
        title: 'Products',
        kicker: 'Search-first catalogue',
        status: 'search',
        subtitle: 'Product, SKU, supplier, barcode, and lot lookup with attention items first.',
      }
    case 'to-order':
      return {
        title: 'To order',
        kicker: 'Replenishment queue',
        status: 'worst first',
        subtitle: 'Products likely to run out before the next normal supplier delivery.',
      }
    case 'sell-first':
      return {
        title: 'Sell first',
        kicker: 'FEFO exceptions',
        status: 'expiry first',
        subtitle: 'Products where short-dated lots must move before later-dated stock.',
      }
    case 'deliveries':
      return {
        title: 'Deliveries',
        kicker: 'Receiving issues',
        status: 'exceptions',
        subtitle: 'Problem purchase orders, missing units, short-dated stock, and fill-rate gaps.',
      }
    case 'cold-chain':
      return {
        title: 'Cold chain',
        kicker: 'Temperature risk',
        status: 'monitor',
        subtitle: 'Fridge and load-shedding exceptions that can spoil stock before sale.',
      }
    case 'connections':
      return {
        title: 'Connections',
        kicker: 'Data feeds',
        status: 'read first',
        subtitle: 'Systems feeding stock, POS, delivery, and catalogue facts into ShelfWise.',
      }
    case 'operations':
      return {
        title: 'Operations',
        kicker: 'Runtime',
        status: 'live check',
        subtitle: 'Backend, inference, trace, and agent execution status for this snapshot.',
      }
    case 'results':
      return {
        title: "Today's results",
        kicker: 'Outcome ledger',
        status: 'today',
        subtitle: 'Resolved actions and value recovered from decisions closed today.',
      }
  }
}

function WorkspaceScreen({
  surface,
  initialQuery,
  onBack,
  data,
  seed,
  queue,
  ops,
  recoveredToday,
}: {
  surface: WorkspaceSurface
  initialQuery?: string
  onBack: () => void
  data: GoldenDemo | null
  seed: SeedSummary | null
  queue: Decision[]
  ops: OperationalSnapshot
  recoveredToday: string | null
}) {
  const [productQuery, setProductQuery] = useState(initialQuery ?? '')
  const [catalogResults, setCatalogResults] = useState<ProductCatalogItem[]>([])
  const [catalogSearchReceipt, setCatalogSearchReceipt] = useState<ProductSearchPayload | null>(null)
  const [catalogSearchState, setCatalogSearchState] = useState<LoadState>('idle')
  const [selectedProductKey, setSelectedProductKey] = useState<string | null>(null)
  const intel = data?.store_intelligence
  const cover = intel?.supplier_cover
  const coverDays = daysNumber(cover?.days_of_supply)
  const batch = intel?.batch_split
  const batches = batch?.fefo_batches ?? []
  const delivery = intel?.delivery_reconciliation
  const apiSellFirstItems = asArray<ProductCatalogItem>(ops.productAttention.sell_first)
  const apiSellFirstUnits = apiSellFirstItems.reduce((total, item) => total + Number(item.sell_first_units ?? 0), 0)
  const apiBlockedUnits = apiSellFirstItems.reduce((total, item) => total + Number(item.blocked_units ?? 0), 0)
  const sellFirstUnits = apiSellFirstUnits || Number(batch?.priority_sell_units ?? 0)
  const sellFirstProducts = apiSellFirstItems.length || (batch && sellFirstUnits > 0 ? 1 : 0)
  const blockedUnits = apiBlockedUnits || Number(batch?.blocked_units ?? 0)
  const orderLines = cover?.transfer_units_recommended ? [cover] : []
  const deliveryIssues = Number(delivery?.missing_units ?? 0) > 0 ? 1 : 0
  const coldRunning = ops.coldChainStatus.running === true
  const coldEnabled = ops.coldChainStatus.enabled === true
  const coldChainValue = data?.scenario === 'cold_chain' || ops.coldChainEvents.length ? 'review' : coldRunning ? 'live' : coldEnabled ? 'armed' : 'clear'
  const copy = workspaceCopy(surface)

  useEffect(() => {
    if (surface === 'products') setProductQuery(initialQuery ?? '')
  }, [surface, initialQuery])

  useEffect(() => {
    if (surface === 'products') setSelectedProductKey(null)
  }, [surface, productQuery])

  useEffect(() => {
    if (surface !== 'products') {
      setCatalogResults([])
      setCatalogSearchReceipt(null)
      setCatalogSearchState('idle')
      return
    }

    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      setCatalogSearchState('loading')
      const params = new URLSearchParams()
      const cleanQuery = productQuery.trim()
      if (cleanQuery) params.set('q', cleanQuery)
      params.set('limit', '20')
      const payload = await fetchOptional<ProductSearchPayload>(`/products/search?${params.toString()}`, controller.signal)
      if (controller.signal.aborted) return
      if (payload) {
        setCatalogResults(asArray<ProductCatalogItem>(payload.products))
        setCatalogSearchReceipt(payload)
        setCatalogSearchState('ready')
      } else {
        setCatalogResults([])
        setCatalogSearchReceipt(null)
        setCatalogSearchState('error')
      }
    }, 180)

    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [surface, productQuery])

  const apiAttentionItems = asArray<ProductCatalogItem>(ops.productAttention.items)
  const attentionProductItems: ProductCatalogItem[] = [...apiAttentionItems]
  if (!apiAttentionItems.length && seed) {
    const low = seed.units_on_hand != null && seed.reorder_point != null && seed.units_on_hand <= seed.reorder_point
    const expiring = seed.days_to_expiry != null && seed.days_to_expiry <= 7
    if (low || expiring) {
      attentionProductItems.push({
        sku: seed.sku,
        name: seed.product_name ?? humanizeOperationalText(seed.sku ?? 'Product'),
        category: seed.category,
        supplier: seed.supplier,
        on_hand: seed.units_on_hand,
        reorder_point: seed.reorder_point,
        days_to_expiry: seed.days_to_expiry,
        requires_attention: true,
        attention_reasons: [expiring ? 'expiring' : null, low ? 'low_stock' : null].filter(Boolean) as string[],
        attention_summary: [
          seed.days_to_expiry != null ? `${seed.days_to_expiry} days to expiry` : null,
          seed.units_on_hand != null ? `${formatValue(seed.units_on_hand)} on hand` : null,
          seed.reorder_point != null ? `reorder at ${formatValue(seed.reorder_point)}` : null,
        ].filter(Boolean).join(' · '),
      })
    }
  }
  if (!apiAttentionItems.length && cover?.sku) {
    attentionProductItems.push({
      sku: cover.sku,
      name: humanizeOperationalText(cover.sku),
      category: 'Replenishment risk',
      on_hand: cover.units_on_hand,
      requires_attention: true,
      attention_reasons: ['low_stock'],
      attention_summary: cover.conclusion ?? (coverDays != null ? `${coverDays} days left` : undefined),
    })
  }
  if (!apiAttentionItems.length && batch?.sku && sellFirstProducts) {
    attentionProductItems.push({
      sku: batch.sku,
      name: humanizeOperationalText(batch.sku),
      category: 'FEFO exception',
      requires_attention: true,
      attention_reasons: ['sell_first'],
      attention_summary: batch.conclusion,
      sell_first_units: batch.priority_sell_units,
      normal_units: batch.normal_units,
      blocked_units: batch.blocked_units,
      total_units: batch.total_units,
      lot_count: batches.length,
      fefo_batches: batches,
    })
  }

  const q = productQuery.trim().toLowerCase()
  const attentionProducts = productRows(attentionProductItems)
  const filteredAttentionProductItems = q
    ? attentionProductItems.filter((p) => [productTitle(p), productMeta(p), productDetail(p)].filter(Boolean).join(' ').toLowerCase().includes(q))
    : attentionProductItems
  const filteredAttentionProducts = productRows(filteredAttentionProductItems)
  const catalogRows = productRows(catalogResults)
  const productResultItems = catalogSearchState === 'error' ? filteredAttentionProductItems : catalogResults
  const productResultRows = catalogSearchState === 'error' ? filteredAttentionProducts : catalogRows
  const selectedProduct = productResultItems.find((item, index) => productKey(item, index) === selectedProductKey) ?? null
  const selectedProductBatches = selectedProduct ? asArray<FefoBatch>(selectedProduct.fefo_batches) : []
  const productResultTitle = productQuery.trim() ? 'Catalogue results' : 'Attention products'
  const productResultCount = catalogSearchState === 'loading' ? 'searching' : pluralLabel(productResultRows.length, 'shown', 'shown')
  const catalogSourceCounts = asObject(catalogSearchReceipt?.source_counts)
  const catalogScanned = fieldNumber(catalogSourceCounts, 'synthetic_scanned')
  const catalogScanBudget = fieldNumber(catalogSourceCounts, 'synthetic_scan_budget')
  const catalogTotalEstimate = fieldNumber(catalogSourceCounts, 'synthetic_total_estimate')
  const catalogSeedMatches = fieldNumber(catalogSourceCounts, 'seed')
  const catalogSyntheticMatches = fieldNumber(catalogSourceCounts, 'synthetic_catalog')
  const searchReceiptVisible = Boolean(productQuery.trim() && catalogSearchReceipt)
  const connectorRows: ConnectorSystemRow[] = ops.tenantConnectors.length
    ? ops.tenantConnectors
    : ops.connectorSystems.length
      ? ops.connectorSystems
      : CONNECTOR_SYSTEMS.map((system) => ({
          system: system.label.toLowerCase(),
          label: system.label,
          transport: system.transport,
          status: 'available',
          read_supported: true,
          webhook_supported: system.transport.includes('webhook'),
          mapper_registered: true,
          enabled_for_tenant: false,
          write_back_mode: 'task_only',
        }))
  const enabledConnectors = connectorRows.filter((row) => row.enabled_for_tenant || row.status === 'enabled').length
  const observability = asObject(ops.observability.snapshot)
  const obsDecisions = asObject(observability.decisions)
  const obsInference = asObject(observability.inference)
  const obsConnectors = asObject(observability.connectors)
  const obsEvents = asObject(observability.events)
  const obsWriteback = asObject(observability.writeback)
  const obsWorker = asObject(observability.worker)
  const obsLearning = asObject(observability.learning)
  const readinessChecks = asObject(ops.readiness.checks)
  const tenantProfile = ops.tenantProfile
  const recoveredReport = formatMoneyish(ops.accountability.recovered) ?? 'R0'
  const routeAvailable = (path: string) => ops.apiPaths.includes(path)

  const renderProducts = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Search mode" value="Attention first" />
        <WorkspaceMetric label="Attention products" value={String(attentionProducts.length)} tone={attentionProducts.length ? 'warn' : 'ok'} />
        <WorkspaceMetric label="Catalogue results" value={catalogSearchState === 'loading' ? 'loading' : String(productResultRows.length)} tone={catalogSearchState === 'error' ? 'warn' : undefined} />
        <WorkspaceMetric
          label="Scan window"
          value={catalogScanBudget ? `${catalogScanned}/${catalogScanBudget}` : 'attention'}
          tone={catalogSearchReceipt?.truncated ? 'info' : undefined}
        />
      </div>
      <WorkspaceSection title="Catalogue search">
        <div className="workspace-search-row">
          <SearchIcon />
          <input
            value={productQuery}
            onChange={(e) => setProductQuery(e.target.value)}
            placeholder="Product, SKU, supplier, barcode, or lot"
            aria-label="Search products"
          />
        </div>
      </WorkspaceSection>
      <WorkspaceSection title={productResultTitle} count={productResultCount}>
        {catalogSearchState === 'loading' ? (
          <WorkspaceEmpty>Searching the bounded catalogue index...</WorkspaceEmpty>
        ) : productResultRows.length ? (
          <div className="workspace-list">
            {productResultRows.map((row, index) => {
              const item = productResultItems[index]
              const key = item ? productKey(item, index) : `${row.label}-${index}`
              return (
                <WorkspaceRow
                  key={key}
                  {...row}
                  active={selectedProductKey === key}
                  onSelect={() => item && setSelectedProductKey(key)}
                />
              )
            })}
          </div>
        ) : (
          <WorkspaceEmpty>
            {catalogSearchState === 'error'
              ? 'Catalogue search is not available from the running backend. Attention rows stay here; inventory never moves into the sidebar.'
              : productQuery
              ? 'No product matches this search in the bounded catalogue result set.'
              : 'No attention products are active.'}
          </WorkspaceEmpty>
        )}
      </WorkspaceSection>
      {searchReceiptVisible ? (
        <WorkspaceSection title="Search receipt">
          <div className="workspace-list">
            <WorkspaceRow
              label="Bounded catalogue scan"
              meta={catalogTotalEstimate ? `${catalogScanned} of ${catalogTotalEstimate} generated rows scanned` : `${catalogScanned} rows scanned`}
              detail="Attention products are ranked first, then ShelfWise caps the demo catalogue scan so product lookup stays fast at large catalogue sizes."
              value={catalogSearchReceipt?.truncated ? 'truncated' : 'complete'}
              tone={catalogSearchReceipt?.truncated ? 'info' : 'ok'}
            />
            <WorkspaceRow
              label="Source mix"
              meta={`${catalogSeedMatches} seed matches · ${catalogSyntheticMatches} catalogue matches`}
              detail={`Result limit ${formatValue(catalogSearchReceipt?.limit ?? 20)}`}
              value={formatLabel(catalogSearchReceipt?.query ?? productQuery)}
            />
          </div>
        </WorkspaceSection>
      ) : null}
      {selectedProduct ? (
        <>
          <WorkspaceSection title="Product card" count={selectedProduct.lot_count ? pluralLabel(selectedProduct.lot_count, 'lot') : undefined}>
            <div className="workspace-list">
              <WorkspaceRow
                label={productTitle(selectedProduct)}
                meta={productMeta(selectedProduct)}
                detail={productDetail(selectedProduct)}
                value={productValue(selectedProduct)}
                tone={productTone(selectedProduct)}
              />
              <WorkspaceRow
                label="Reason surfaced"
                meta={formatValue(selectedProduct.attention_reasons ?? [])}
                detail={selectedProduct.attention_summary}
                value={selectedProduct.requires_attention ? 'attention' : 'catalogue'}
                tone={selectedProduct.requires_attention ? 'warn' : 'info'}
              />
            </div>
          </WorkspaceSection>
          <WorkspaceSection title="Lot rotation" count={selectedProductBatches.length ? pluralLabel(selectedProductBatches.length, 'lot') : undefined}>
            {selectedProductBatches.length ? (
              <div className="workspace-table" role="table" aria-label="Selected product lots">
                <div className="workspace-table-head" role="row">
                  <span role="columnheader">Lot</span>
                  <span role="columnheader">Expiry</span>
                  <span role="columnheader">Location</span>
                  <span role="columnheader">Units</span>
                  <span role="columnheader">Status</span>
                </div>
                {selectedProductBatches.map((lot, index) => (
                  <div className="workspace-table-row" role="row" key={lot.lot ?? index}>
                    <span role="cell" className="tnum">{lot.lot ?? '-'}</span>
                    <span role="cell">{batchExpiryText(lot)}</span>
                    <span role="cell">{formatLabel(lot.location ?? 'store')}</span>
                    <span role="cell" className="tnum">{formatValue(lot.units)}</span>
                    <span role="cell" className={lot.stock_status === 'blocked' ? 'tone-risk' : lot.stock_status === 'priority_sell' ? 'tone-warn' : ''}>
                      {lot.stock_status === 'priority_sell'
                        ? 'Sell first'
                        : lot.stock_status === 'blocked'
                          ? 'Blocked'
                          : 'Normal'}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <WorkspaceEmpty>This catalogue row has no lot-level FEFO data attached.</WorkspaceEmpty>
            )}
          </WorkspaceSection>
        </>
      ) : null}
    </>
  )

  const renderToOrder = () => (
    <WorkspaceSection title="Worst first" count={orderLines.length ? pluralLabel(orderLines.length, 'product') : 'clear'}>
      {orderLines.length ? (
        <div className="workspace-list">
          {orderLines.map((line) => (
            <WorkspaceRow
              key={line.sku}
              label={humanizeOperationalText(line.sku ?? 'Product')}
              meta={line.conclusion}
              detail={[
                line.units_on_hand != null ? `${formatValue(line.units_on_hand)} on hand` : null,
                coverDays != null ? `${coverDays} days left` : null,
                line.supplier_lead_time_days ? `${line.supplier_lead_time_days}d lead` : null,
              ]
                .filter(Boolean)
                .join(' · ')}
              value={`${formatValue(line.transfer_units_recommended)} units`}
              tone="warn"
            />
          ))}
        </div>
      ) : (
        <WorkspaceEmpty>No replenishment exception is active.</WorkspaceEmpty>
      )}
    </WorkspaceSection>
  )

  const renderSellFirst = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Products flagged" value={String(sellFirstProducts)} tone={sellFirstProducts ? 'warn' : 'ok'} />
        <WorkspaceMetric label="Sell-first units" value={formatValue(sellFirstUnits)} tone={sellFirstProducts ? 'warn' : undefined} />
        <WorkspaceMetric label="Blocked units" value={formatValue(blockedUnits)} tone={blockedUnits > 0 ? 'risk' : undefined} />
      </div>
      <WorkspaceSection title="Product queue" count={sellFirstProducts ? pluralLabel(sellFirstProducts, 'product') : 'clear'}>
        {apiSellFirstItems.length ? (
          <div className="workspace-list">
            {apiSellFirstItems.map((item, index) => (
              <WorkspaceRow
                key={productKey(item, index)}
                label={productTitle(item)}
                meta={productMeta(item)}
                detail={productDetail(item)}
                value={productValue(item)}
                tone={productTone(item)}
              />
            ))}
          </div>
        ) : batch ? (
          <WorkspaceRow
            label={humanizeOperationalText(batch.sku ?? 'Product')}
            meta={batch.conclusion}
            detail={`${formatValue(batch.normal_units)} normal · ${formatValue(batch.blocked_units)} blocked · ${formatValue(batch.total_units)} total`}
            value={`${formatValue(batch.priority_sell_units)} sell first`}
            tone={sellFirstProducts ? 'warn' : undefined}
          />
        ) : (
          <WorkspaceEmpty>No FEFO exception is active.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Lot rotation" count={batches.length ? pluralLabel(batches.length, 'lot') : undefined}>
        {batches.length ? (
          <div className="workspace-table" role="table" aria-label="Sell first lots">
            <div className="workspace-table-head" role="row">
              <span role="columnheader">Lot</span>
              <span role="columnheader">Expiry</span>
              <span role="columnheader">Location</span>
              <span role="columnheader">Units</span>
              <span role="columnheader">Status</span>
            </div>
            {batches.map((lot, index) => (
              <div className="workspace-table-row" role="row" key={lot.lot ?? index}>
                <span role="cell" className="tnum">{lot.lot ?? '-'}</span>
                <span role="cell">{batchExpiryText(lot)}</span>
                <span role="cell">{formatLabel(lot.location ?? 'store')}</span>
                <span role="cell" className="tnum">{formatValue(lot.units)}</span>
                <span role="cell" className={lot.stock_status === 'blocked' ? 'tone-risk' : lot.stock_status === 'priority_sell' ? 'tone-warn' : ''}>
                  {lot.stock_status === 'priority_sell'
                    ? 'Sell first'
                    : lot.stock_status === 'blocked'
                      ? 'Blocked'
                      : 'Normal'}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No lot detail is available for this snapshot.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Investigation signals">
        <div className="workspace-list">
          <WorkspaceRow
            label="Short-dated stock remaining"
            meta="Check POS depletion against lot expiry order"
            value={`${formatValue(batch?.priority_sell_units)} units`}
            tone={sellFirstProducts ? 'warn' : undefined}
          />
          <WorkspaceRow
            label="Later-dated stock available"
            meta="Back stock should not move before priority lots"
            value={`${formatValue(batch?.normal_units)} units`}
          />
          <WorkspaceRow
            label="Reason to investigate"
            detail="If later-dated lots are selling while short-dated lots remain, ShelfWise should flag shelf rotation, picking, or POS lot-mapping drift."
            value="FEFO"
            tone="info"
          />
        </div>
      </WorkspaceSection>
    </>
  )

  const renderDeliveries = () => (
    <WorkspaceSection title="Problem purchase orders" count={deliveryIssues ? pluralLabel(deliveryIssues, 'issue') : 'clear'}>
      {delivery ? (
        <div className="workspace-list">
          <WorkspaceRow
            label={humanizeOperationalText(delivery.sku ?? 'Purchase order')}
            meta={delivery.conclusion}
            detail={[
              `${formatValue(delivery.ordered_units)} ordered`,
              `${formatValue(delivery.received_units)} received`,
              `${formatValue(delivery.accepted_units)} accepted`,
              `${formatValue(delivery.short_dated_units)} short dated`,
            ].join(' · ')}
            value={deliveryIssues ? `${formatValue(delivery.missing_units)} short` : formatLabel(delivery.status ?? 'clear')}
            tone={deliveryIssues ? 'warn' : 'ok'}
          />
        </div>
      ) : (
        <WorkspaceEmpty>No delivery exception is active.</WorkspaceEmpty>
      )}
    </WorkspaceSection>
  )

  const renderColdChain = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Status" value={formatLabel(coldChainValue)} tone={coldChainValue === 'review' ? 'risk' : 'ok'} />
        <WorkspaceMetric label="Feed" value={coldRunning ? 'Running' : coldEnabled ? 'Armed' : 'Idle'} tone={coldRunning ? 'ok' : coldEnabled ? 'warn' : undefined} />
        <WorkspaceMetric label="Events" value={String(ops.coldChainEvents.length)} tone={ops.coldChainEvents.length ? 'risk' : undefined} />
        <WorkspaceMetric label="Scenario" value={formatLabel(data?.scenario ?? 'normal')} />
      </div>
      <WorkspaceSection title="Active checks">
        {coldChainValue === 'review' ? (
          <WorkspaceRow
            label="Cold-chain scenario"
            detail="A temperature-risk scenario is active in the current cascade snapshot."
            value="review"
            tone="risk"
          />
        ) : (
          <WorkspaceEmpty>No cold-chain alert is active.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Feed events" count={ops.coldChainEvents.length ? pluralLabel(ops.coldChainEvents.length, 'event') : undefined}>
        {ops.coldChainEvents.length ? (
          <div className="workspace-list">
            {ops.coldChainEvents.slice(0, 8).map((event, index) => (
              <WorkspaceRow
                key={String(event.id ?? event.ts ?? index)}
                label={formatLabel(event.kind ?? event.type ?? 'Cold-chain event')}
                meta={fieldText(event, 'asset_id', fieldText(asObject(event.payload), 'asset_id', 'Feed'))}
                detail={fieldText(event, 'message', fieldText(event, 'ts', 'Buffered feed event'))}
                value={fieldText(event, 'status', 'event')}
                tone="risk"
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No buffered cold-chain feed events.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
    </>
  )

  const renderConnections = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Systems" value={String(connectorRows.length)} />
        <WorkspaceMetric label="Enabled" value={String(enabledConnectors)} tone={enabledConnectors ? 'ok' : undefined} />
        <WorkspaceMetric label="Inbound records" value={String(ops.inboundRecords.length || fieldNumber(obsConnectors, 'inbound_records'))} />
        <WorkspaceMetric label="Invalid records" value={String(fieldNumber(obsConnectors, 'invalid_records'))} tone={fieldNumber(obsConnectors, 'invalid_records') ? 'risk' : undefined} />
      </div>
      <WorkspaceSection title="Connector catalogue" count={pluralLabel(connectorRows.length, 'system')}>
        <div className="workspace-list">
          {connectorRows.map((system) => {
            const row = asObject(system)
            const status = String(system.status ?? 'available')
            const transport = String(system.transport ?? 'feed')
            const mode = String(system.write_back_mode ?? 'task_only')
            return (
              <WorkspaceRow
                key={system.system ?? system.label ?? transport}
                label={system.label ?? formatLabel(system.system ?? 'System')}
                meta={`${transport} · ${system.webhook_supported ? 'webhook' : 'read'} · ${mode}`}
                detail={system.enabled_for_tenant ? 'Enabled for this store.' : system.mapper_registered === false ? 'Mapper not registered yet.' : undefined}
                value={formatLabel(fieldText(row, 'status', status))}
                tone={optionalStatusTone(status)}
              />
            )
          })}
        </div>
      </WorkspaceSection>
      <WorkspaceSection title="Store connector policy">
        {tenantProfile.tenant_id ? (
          <div className="workspace-list">
            <WorkspaceRow
              label={fieldText(tenantProfile, 'name', 'Store profile')}
              meta={[fieldText(tenantProfile, 'country', 'ZA'), fieldText(tenantProfile, 'timezone', 'Africa/Johannesburg')].join(' · ')}
              detail={fieldText(tenantProfile, 'currency', 'ZAR')}
              value={fieldText(tenantProfile, 'status', 'active')}
              tone={optionalStatusTone(tenantProfile.status ?? 'active')}
            />
            <WorkspaceRow
              label="Connector policy"
              meta="Secrets are stored as references, not inline values."
              detail={formatValue(tenantProfile.connector_policy ?? {})}
              value="guarded"
              tone="info"
            />
          </div>
        ) : (
          <WorkspaceEmpty>Tenant profile is not available from this backend.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Recent inbound records" count={ops.inboundRecords.length ? pluralLabel(ops.inboundRecords.length, 'record') : undefined}>
        {ops.inboundRecords.length ? (
          <div className="workspace-list">
            {ops.inboundRecords.slice(0, 8).map((record, index) => {
              const ok = record.validation?.ok !== false
              return (
                <WorkspaceRow
                  key={record.id ?? record.raw_payload_hash ?? index}
                  label={formatLabel(record.source_system ?? 'Connector')}
                  meta={[record.source_object_type, record.canonical_type].filter(Boolean).join(' -> ')}
                  detail={record.event_id ? `Event ${record.event_id}` : record.validation?.errors?.join(', ')}
                  value={ok ? 'accepted' : 'invalid'}
                  tone={ok ? 'ok' : 'risk'}
                />
              )
            })}
          </div>
        ) : (
          <WorkspaceEmpty>No connector records have arrived in this session.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Webhook, scan, and intelligence gates" count={pluralLabel(GATED_ENDPOINTS.filter((item) => item.group === 'connections' || item.group === 'intelligence').length, 'endpoint')}>
        <div className="workspace-list">
          {GATED_ENDPOINTS.filter((item) => item.group === 'connections' || item.group === 'intelligence').map((item) => (
            <WorkspaceRow
              key={item.path}
              label={item.label}
              meta={`${item.method} ${item.path}`}
              detail={item.detail}
              value={routeAvailable(item.path) ? 'available' : 'missing'}
              tone={routeAvailable(item.path) ? 'info' : 'warn'}
            />
          ))}
        </div>
      </WorkspaceSection>
    </>
  )

  const renderOperations = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Health" value={ops.health.ok === true ? 'OK' : data ? 'Live' : 'Loading'} tone={ops.health.ok === true || data ? 'ok' : 'warn'} />
        <WorkspaceMetric label="Ready" value={ops.readiness.ready === true ? 'Ready' : ops.readiness.ready === false ? 'Check' : 'Unknown'} tone={ops.readiness.ready === true ? 'ok' : ops.readiness.ready === false ? 'risk' : 'warn'} />
        <WorkspaceMetric label="Decisions" value={String(fieldNumber(obsDecisions, 'total') || queue.length)} />
        <WorkspaceMetric label="Events" value={String(ops.events.length || fieldNumber(obsEvents, 'stored_events'))} />
        <WorkspaceMetric label="Model runs" value={String(ops.modelRuns.length || fieldNumber(obsInference, 'model_runs'))} />
        <WorkspaceMetric label="Tools" value={String(ops.platformTools.length)} />
        <WorkspaceMetric label="Routes" value={String(ops.apiPaths.length)} />
        <WorkspaceMetric label="Inference" value={formatLabel(ops.inferenceConfig.provider ?? data?.inference?.provider ?? 'offline')} />
      </div>
      <WorkspaceSection title="Service checks">
        <div className="workspace-list">
          <WorkspaceRow
            label="Backend health"
            meta={fieldText(ops.health, 'service', 'ShelfWise')}
            detail={fieldText(ops.health, 'version', '0.1.0')}
            value={ops.health.ok === true ? 'ok' : 'unknown'}
            tone={ops.health.ok === true ? 'ok' : 'warn'}
          />
          <WorkspaceRow
            label="Readiness"
            meta={fieldText(ops.readiness, 'ready', 'unknown')}
            detail={Object.entries(readinessChecks).slice(0, 6).map(([key, value]) => `${formatLabel(key)}: ${formatValue(value)}`).join(' · ')}
            value={ops.readiness.ready === true ? 'ready' : 'check'}
            tone={ops.readiness.ready === true ? 'ok' : 'warn'}
          />
          <WorkspaceRow
            label="Inference routing"
            meta={[
              fieldText(ops.inferenceConfig, 'provider', fieldText(asObject(ops.health.inference), 'provider', 'offline')),
              fieldText(ops.inferenceConfig, 'routine_model', 'routine'),
              fieldText(ops.inferenceConfig, 'strong_model', 'strong'),
            ].join(' · ')}
            detail={fieldText(ops.inferenceConfig, 'api_key_present', fieldText(asObject(ops.health.inference), 'api_key_present', 'credential flag unavailable'))}
            value={fieldText(ops.inferenceConfig, 'base_url_configured', 'offline-safe')}
            tone={optionalStatusTone(fieldText(ops.inferenceConfig, 'provider', 'offline'))}
          />
        </div>
      </WorkspaceSection>
      <WorkspaceSection title="Read-only API coverage" count={pluralLabel(OPERATION_READ_ENDPOINTS.length, 'endpoint')}>
        <div className="workspace-list">
          {OPERATION_READ_ENDPOINTS.map((item) => (
            <WorkspaceRow
              key={item.path}
              label={item.label}
              meta={`${item.method} ${item.path}`}
              detail={item.detail}
              value={routeAvailable(item.path) ? 'connected' : 'missing'}
              tone={routeAvailable(item.path) ? 'ok' : 'warn'}
            />
          ))}
        </div>
      </WorkspaceSection>
      <WorkspaceSection title="Trace registry" count={ops.traces.length ? pluralLabel(ops.traces.length, 'trace') : undefined}>
        {ops.traces.length ? (
          <div className="workspace-list">
            {ops.traces.slice(0, 8).map((trace, index) => (
              <WorkspaceRow
                key={String(trace.correlation_id ?? index)}
                label={fieldText(trace, 'scenario', 'Cascade trace')}
                meta={fieldText(trace, 'correlation_id', 'Correlation')}
                detail={formatValue(trace.evidence_agents ?? [])}
                value={fieldText(trace, 'status', 'ok')}
                tone={optionalStatusTone(trace.status ?? 'ok')}
              />
            ))}
          </div>
        ) : data?.trace?.length ? (
          <div className="workspace-list">
            {data.trace.map((span, index) => (
              <WorkspaceRow
                key={`${span.name}-${index}`}
                label={formatLabel(span.name)}
                meta={span.status}
                value={span.ms != null ? `${Math.round(span.ms)} ms` : undefined}
                tone={span.status === 'ok' ? 'ok' : span.status ? 'warn' : undefined}
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No trace spans are available.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Platform tools" count={ops.platformTools.length ? pluralLabel(ops.platformTools.length, 'tool') : undefined}>
        {ops.platformTools.length ? (
          <div className="workspace-list">
            {ops.platformTools.map((tool, index) => (
              <WorkspaceRow
                key={String(tool.name ?? index)}
                label={fieldText(tool, 'name', 'Tool')}
                meta={fieldText(tool, 'description', 'Read-only platform tool')}
                value={tool.read_only === false ? 'write' : 'read only'}
                tone={tool.read_only === false ? 'risk' : 'ok'}
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No platform tools are exposed by this backend.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="MLOps ledger">
        <div className="workspace-list">
          <WorkspaceRow
            label="Accountability"
            meta={`Models ${formatValue(ops.accountability.models_used ?? [])}`}
            detail={`Prompt versions ${formatValue(ops.accountability.prompt_versions ?? [])}`}
            value={recoveredReport}
            tone={moneyMinorUnits(ops.accountability.recovered) ? 'ok' : undefined}
          />
          <WorkspaceRow
            label="Model runs"
            meta={`${ops.modelRuns.length} recorded`}
            detail={ops.modelRuns.slice(0, 3).map((run) => [run.agent, run.model, run.provider].filter(Boolean).join(' / ')).filter(Boolean).join(' · ')}
            value={String(ops.modelRuns.length || fieldNumber(obsInference, 'model_runs'))}
          />
          <WorkspaceRow
            label="Prompt versions"
            meta={`${ops.promptVersions.length} registered`}
            detail={ops.promptVersions.slice(0, 3).map((prompt) => [prompt.agent, prompt.id ?? prompt.version].filter(Boolean).join(' / ')).filter(Boolean).join(' · ')}
            value={String(ops.promptVersions.length)}
          />
          <WorkspaceRow
            label="Tenant facts"
            meta={`${ops.tenantFacts.length} governed facts`}
            detail={ops.tenantFacts.slice(0, 3).map((fact) => [fact.sku, fact.action, fact.conclusion ?? fact.fact].filter(Boolean).join(' / ')).filter(Boolean).join(' · ')}
            value={String(ops.tenantFacts.length)}
          />
        </div>
      </WorkspaceSection>
      <WorkspaceSection title="Worker and worldgen">
        <div className="workspace-list">
          <WorkspaceRow
            label="Worker service"
            meta={formatValue(ops.worker)}
            detail={Object.entries(obsWorker).slice(0, 4).map(([key, value]) => `${formatLabel(key)}: ${formatValue(value)}`).join(' · ')}
            value={fieldText(ops.worker, 'status', fieldText(obsWorker, 'status', 'idle'))}
            tone={optionalStatusTone(fieldText(ops.worker, 'status', fieldText(obsWorker, 'status', 'idle')))}
          />
          {ops.workerRuns.length ? (
            ops.workerRuns.slice(0, 4).map((run, index) => (
              <WorkspaceRow
                key={String(run.run_id ?? index)}
                label={fieldText(run, 'run_id', 'Worker run')}
                meta={fieldText(run, 'tenant_id', 'Tenant')}
                detail={fieldText(run, 'started_at', 'Started')}
                value={fieldText(run, 'status', 'run')}
                tone={optionalStatusTone(run.status)}
              />
            ))
          ) : (
            <WorkspaceRow label="Worker runs" meta="No journaled runs yet." value="clear" />
          )}
          {ops.worldgenRuns.length ? (
            ops.worldgenRuns.slice(0, 4).map((run, index) => (
              <WorkspaceRow
                key={String(run.run_id ?? index)}
                label={fieldText(run, 'scenario_id', 'Worldgen run')}
                meta={fieldText(run, 'run_id', 'Run')}
                detail={[`events ${formatValue(run.events_total)}`, `decisions ${formatValue(run.decisions_total)}`].join(' · ')}
                value={fieldText(run, 'status', 'completed')}
                tone={optionalStatusTone(run.status ?? 'completed')}
              />
            ))
          ) : (
            <WorkspaceRow label="Worldgen runs" meta="No synthetic drill run recorded yet." value="clear" />
          )}
        </div>
      </WorkspaceSection>
      <WorkspaceSection title="Event bus" count={ops.busMessages.length ? pluralLabel(ops.busMessages.length, 'message') : undefined}>
        {ops.busMessages.length ? (
          <div className="workspace-list">
            {ops.busMessages.slice(0, 8).map((message, index) => {
              const event = message.event ?? {}
              return (
                <WorkspaceRow
                  key={message.id ?? message.message_id ?? event.id ?? index}
                  label={formatLabel(event.type ?? 'Event')}
                  meta={event.id}
                  detail={fieldText(asObject(event.payload), 'sku', fieldText(asObject(event.payload), 'location', 'Bus message'))}
                  value={message.message_id ?? message.id ?? 'queued'}
                  tone="info"
                />
              )
            })}
          </div>
        ) : (
          <WorkspaceEmpty>No event bus messages are buffered.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Write-back tasks" count={ops.writebackTasks.length ? pluralLabel(ops.writebackTasks.length, 'task') : undefined}>
        {ops.writebackTasks.length ? (
          <div className="workspace-list">
            {ops.writebackTasks.slice(0, 6).map((task, index) => (
              <WorkspaceRow
                key={task.id ?? index}
                label={task.title ?? describeAction(task.action)}
                meta={task.assignee_role}
                detail={task.created_at}
                value={formatLabel(task.status ?? 'pending')}
            tone={optionalStatusTone(task.status ?? 'pending')}
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No write-back tasks are waiting.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Learning memory" count={ops.learningEvents.length ? pluralLabel(ops.learningEvents.length, 'event') : undefined}>
        {ops.learningEvents.length ? (
          <div className="workspace-list">
            {ops.learningEvents.slice(0, 6).map((event, index) => (
              <WorkspaceRow
                key={String(event.id ?? event.decision_id ?? index)}
                label={fieldText(event, 'sku', 'Learning event')}
                meta={fieldText(event, 'decision_id', 'Decision')}
                detail={fieldText(event, 'message', 'Outcome recorded')}
                value={fieldText(asObject(event.outcome), 'success_score', 'learned')}
                tone="ok"
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No learning events recorded yet.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Gated operational endpoints" count={pluralLabel(GATED_ENDPOINTS.filter((item) => item.group === 'operations').length, 'endpoint')}>
        <div className="workspace-list">
          {GATED_ENDPOINTS.filter((item) => item.group === 'operations').map((item) => (
            <WorkspaceRow
              key={item.path}
              label={item.label}
              meta={`${item.method} ${item.path}`}
              detail={item.detail}
              value={routeAvailable(item.path) ? 'gated' : 'missing'}
              tone={routeAvailable(item.path) ? 'info' : 'warn'}
            />
          ))}
          <WorkspaceRow
            label="Root-cause SQL"
            meta="/detective/root-cause-sql"
            detail={ops.detectiveSql ? 'SQL template loaded for root-cause traversal.' : 'Root-cause SQL template unavailable.'}
            value={ops.detectiveSql ? 'connected' : 'missing'}
            tone={ops.detectiveSql ? 'ok' : 'warn'}
          />
        </div>
      </WorkspaceSection>
    </>
  )

  const renderResults = () => (
    <>
      <div className="workspace-metrics">
        <WorkspaceMetric label="Recovered today" value={recoveredToday ?? 'R0'} tone={recoveredToday ? 'ok' : undefined} />
        <WorkspaceMetric label="Approvals waiting" value={String(queue.length)} tone={queue.length ? 'warn' : 'ok'} />
        <WorkspaceMetric label="Write-back tasks" value={String(ops.writebackTasks.length || fieldNumber(obsWriteback, 'tasks'))} />
        <WorkspaceMetric label="Learning events" value={String(ops.learningEvents.length || fieldNumber(obsLearning, 'learning_events'))} />
      </div>
      <WorkspaceSection title="Today">
        {recoveredToday ? (
          <WorkspaceRow label="Resolved value" meta="Approved actions closed today" value={recoveredToday} tone="ok" />
        ) : (
          <WorkspaceEmpty>No recovered value has been recorded today.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Write-back ledger" count={ops.writebackTasks.length ? pluralLabel(ops.writebackTasks.length, 'task') : undefined}>
        {ops.writebackTasks.length ? (
          <div className="workspace-list">
            {ops.writebackTasks.slice(0, 6).map((task, index) => (
              <WorkspaceRow
                key={task.id ?? index}
                label={task.title ?? describeAction(task.action)}
                meta={task.assignee_role}
                value={formatLabel(task.status ?? 'pending')}
            tone={optionalStatusTone(task.status ?? 'pending')}
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No task-only write-back records yet.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
      <WorkspaceSection title="Learning outcomes" count={ops.learningEvents.length ? pluralLabel(ops.learningEvents.length, 'event') : undefined}>
        {ops.learningEvents.length ? (
          <div className="workspace-list">
            {ops.learningEvents.slice(0, 6).map((event, index) => (
              <WorkspaceRow
                key={String(event.id ?? event.decision_id ?? index)}
                label={fieldText(event, 'sku', 'Learning event')}
                meta={fieldText(event, 'decision_id', 'Decision')}
                detail={fieldText(event, 'message', 'Outcome recorded')}
                value={fieldText(asObject(event.outcome), 'success_score', 'learned')}
                tone="ok"
              />
            ))}
          </div>
        ) : (
          <WorkspaceEmpty>No learning outcome has been recorded yet.</WorkspaceEmpty>
        )}
      </WorkspaceSection>
    </>
  )

  const renderContent = () => {
    switch (surface) {
      case 'products':
        return renderProducts()
      case 'to-order':
        return renderToOrder()
      case 'sell-first':
        return renderSellFirst()
      case 'deliveries':
        return renderDeliveries()
      case 'cold-chain':
        return renderColdChain()
      case 'connections':
        return renderConnections()
      case 'operations':
        return renderOperations()
      case 'results':
        return renderResults()
    }
  }

  return (
    <main className="workspace-screen" aria-label={`${copy.title} workspace`}>
      <div className="workspace-shell">
        <div className="workspace-head">
          <button className="workspace-back" type="button" onClick={onBack}>
            <span className="nav-chevron back" aria-hidden />
            Back to chat
          </button>
          <div className="workspace-title">
            <span className="workspace-kicker">{copy.kicker}</span>
            <h1>{copy.title}</h1>
            <p>{copy.subtitle}</p>
          </div>
          <span className="workspace-status">{copy.status}</span>
        </div>
        <div className="workspace-content">{renderContent()}</div>
      </div>
    </main>
  )
}

// ---------------------------------------------------------------------------
// Composer (text + voice + suggestions)
// ---------------------------------------------------------------------------
// Quick actions are shortcuts, not decoration: each one either reveals the relevant surface
// directly or asks the concrete operational question it names.
type QuickAction = { label: string; run: (ctx: { send: (text: string) => void; openApprovals: () => void }) => void }
const QUICK_ACTIONS: QuickAction[] = [
  { label: 'Approval queue', run: ({ openApprovals }) => openApprovals() },
  { label: "What's at risk today?", run: ({ send }) => send("What's at risk today?") },
]

function Composer({ onSend, onOpenApprovals }: { onSend: (text: string) => void; onOpenApprovals: () => void }) {
  const [text, setText] = useState('')
  const voice = useVoiceInput((t) => setText((prev) => (prev ? `${prev} ${t}` : t)))
  const send = (value: string) => {
    const trimmed = value.trim()
    if (!trimmed) return
    onSend(trimmed)
    setText('')
  }
  return (
    <div className="composer">
      <div className="suggestions">
        {QUICK_ACTIONS.map((action) => (
          <button
            className="chip-btn"
            type="button"
            key={action.label}
            onClick={() => action.run({ send, openApprovals: onOpenApprovals })}
          >
            {action.label}
          </button>
        ))}
      </div>
      <div className="composer-row">
        {voice.supported ? (
          <button
            className={`icon-btn mic ${voice.listening ? 'is-live' : ''}`}
            type="button"
            aria-label={voice.listening ? 'Stop listening' : 'Talk to ShelfWise'}
            aria-pressed={voice.listening}
            onClick={() => (voice.listening ? voice.stop() : voice.start())}
          >
            <UiIcon name={voice.listening ? 'stop' : 'mic'} />
          </button>
        ) : null}
        <input
          className="composer-input"
          value={text}
          placeholder={voice.listening ? 'Listening...' : 'Ask ShelfWise...'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') send(text)
          }}
          aria-label="Message ShelfWise"
        />
        <button className="btn btn-primary send" type="button" aria-label="Send message" onClick={() => send(text)} disabled={!text.trim()}>
          <UiIcon name="send" />
        </button>
      </div>
    </div>
  )
}

/** The assistant's opening line, derived from the live queue - reused by first load and New chat. */
function greetingFor(pending: Decision[]): string {
  if (pending.length === 0) return "Queue clear. I'll surface exceptions as soon as they appear."
  if (pending.length === 1) return 'One approval is ready. Open the status bar to review the evidence.'
  return `${pending.length} approvals are ready. Open the status bar to review highest risk first.`
}

/** True when the sidebar behaves as a mobile overlay (so opening a surface must close it). */
function isOverlayViewport(): boolean {
  return typeof window !== 'undefined' && window.matchMedia('(max-width: 900px)').matches
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
function App() {
  const [data, setData] = useState<GoldenDemo | null>(null)
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [seed, setSeed] = useState<SeedSummary | null>(null)
  const [ops, setOps] = useState<OperationalSnapshot>(() => emptyOps())
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  // Sidebar is persistent on desktop (open by default), an overlay on mobile (closed by default).
  const [sidebarOpen, setSidebarOpen] = useState(() => !isOverlayViewport())
  const [approvalOpen, setApprovalOpen] = useState(false)
  const [activeWorkspace, setActiveWorkspace] = useState<{ surface: WorkspaceSurface; query?: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const transitionCtrl = useRef<AbortController | null>(null)
  const chatCtrl = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const queue = useMemo(() => pendingQueue(decisions, data?.decision), [decisions, data])
  const resolved = useMemo(() => {
    const byId = new Map<string, Decision>()
    for (const d of decisions) if (d.id) byId.set(d.id, d)
    if (data?.decision?.id) byId.set(data.decision.id, data.decision)
    return Array.from(byId.values()).filter((d) => (d.status ?? 'pending').toLowerCase() !== 'pending')
  }, [decisions, data])

  // Value recovered by decisions resolved today - one honest number, summed from real outcomes.
  const recoveredToday = useMemo(() => {
    let cents = 0
    for (const d of resolved) {
      if (dayLabel(d.updated_at ?? d.created_at) !== 'Today') continue
      const minor = moneyMinorUnits(d.outcome?.rand_recovered)
      if (minor && minor > 0) cents += minor
    }
    return cents > 0 ? `R${Math.round(cents / 100).toLocaleString('en-ZA')}` : null
  }, [resolved])

  // Recents: the current live conversation only. When Postgres persistence lands, resolved
  // conversations join this list; New chat archives the current one instead of clearing it.
  const recents = useMemo<Recent[]>(() => {
    const firstUser = messages.find((m) => m.role === 'user')
    return [{ id: 'current', title: firstUser ? firstUser.text : "Today's operations", active: true }]
  }, [messages])

  useEffect(() => {
    const controller = new AbortController()
    setLoadState('loading')
    setError(null)
    setOps(emptyOps())
    async function load() {
      const payload = await fetchDemo(DEMO_PATH, controller.signal)
      const openApi = await fetchOptional<{ paths?: JsonObject }>('/openapi.json', controller.signal)
      const apiPaths = Object.keys(asObject(openApi?.paths)).sort()
      const available = (path: string) => Boolean(asObject(openApi?.paths)[path])
      const fetchIfAvailable = <T,>(path: string, pathKey = path): Promise<T | null> =>
        available(pathKey) ? fetchOptional<T>(path, controller.signal) : Promise.resolve(null)
      const [
        healthPayload,
        readinessPayload,
        inferenceConfigPayload,
        logPayload,
        seedPayload,
        connectorCatalog,
        tenantConnectors,
        inboundRecords,
        eventsPayload,
        busPayload,
        coldChainPayload,
        tracesPayload,
        platformToolsPayload,
        platformToolAuditPayload,
        writebackPayload,
        learningPayload,
        productAttentionPayload,
        modelRunsPayload,
        promptVersionsPayload,
        accountabilityPayload,
        tenantFactsPayload,
        tenantProfilePayload,
        workerRunsPayload,
        observabilityPayload,
        workerPayload,
        worldgenRunsPayload,
        detectiveSqlPayload,
      ] = await Promise.all([
        fetchIfAvailable<JsonObject>('/health'),
        fetchIfAvailable<JsonObject>('/readiness'),
        fetchIfAvailable<JsonObject>('/inference/config'),
        fetchIfAvailable<DecisionLogResponse>('/decisions'),
        fetchIfAvailable<{ seed_data?: SeedSummary }>('/data/seed/summary'),
        fetchIfAvailable<{ systems?: ConnectorSystemRow[] }>('/connectors/systems'),
        fetchIfAvailable<{ systems?: ConnectorSystemRow[] }>('/connectors/me'),
        fetchIfAvailable<{ records?: InboundRecordRow[] }>('/connectors/inbound-records?limit=50', '/connectors/inbound-records'),
        fetchIfAvailable<{ events?: EventRow[] }>('/events?limit=80', '/events'),
        fetchIfAvailable<{ messages?: BusMessageRow[] }>('/events/bus'),
        fetchIfAvailable<{ status?: JsonObject; events?: JsonObject[] }>('/cold-chain/feed'),
        fetchIfAvailable<{ traces?: JsonObject[] }>('/traces'),
        fetchIfAvailable<{ tools?: JsonObject[] }>('/tools/platform'),
        fetchIfAvailable<{ events?: JsonObject[] }>('/tools/platform/audit'),
        fetchIfAvailable<{ tasks?: WritebackTaskRow[] }>('/writeback/tasks'),
        fetchIfAvailable<{ thresholds?: JsonObject; events?: JsonObject[] }>('/learning'),
        fetchIfAvailable<ProductAttentionPayload>('/products/attention?limit=20', '/products/attention'),
        fetchIfAvailable<{ model_runs?: JsonObject[] }>('/mlops/model-runs'),
        fetchIfAvailable<{ prompt_versions?: JsonObject[] }>('/mlops/prompts'),
        fetchIfAvailable<{ report?: JsonObject; markdown?: string }>('/mlops/accountability?tenant_id=sa_retail_demo', '/mlops/accountability'),
        fetchIfAvailable<{ facts?: JsonObject[] }>('/mlops/tenant-facts'),
        fetchIfAvailable<{ profile?: JsonObject }>('/tenants/me'),
        fetchIfAvailable<{ runs?: JsonObject[] }>('/worker/runs'),
        fetchIfAvailable<{ snapshot?: JsonObject }>('/mlops/observability?limit=200', '/mlops/observability'),
        fetchIfAvailable<{ worker?: JsonObject }>('/worker/status'),
        fetchIfAvailable<{ runs?: JsonObject[] }>('/demo/worldgen-runs?limit=20', '/demo/worldgen-runs'),
        fetchIfAvailable<{ sql?: string }>('/detective/root-cause-sql'),
      ])
      const log = Array.isArray(logPayload?.decisions) ? logPayload.decisions : payload.decision ? [payload.decision] : []
      const seedData = seedPayload?.seed_data ?? null
      if (controller.signal.aborted) return
      setData(payload)
      setDecisions(log)
      setSeed(seedData)
      setOps({
        apiPaths,
        health: asObject(healthPayload),
        readiness: asObject(readinessPayload),
        inferenceConfig: asObject(inferenceConfigPayload),
        connectorSystems: asArray<ConnectorSystemRow>(connectorCatalog?.systems),
        tenantConnectors: asArray<ConnectorSystemRow>(tenantConnectors?.systems),
        inboundRecords: asArray<InboundRecordRow>(inboundRecords?.records),
        events: asArray<EventRow>(eventsPayload?.events),
        busMessages: asArray<BusMessageRow>(busPayload?.messages),
        coldChainStatus: asObject(coldChainPayload?.status),
        coldChainEvents: asArray<JsonObject>(coldChainPayload?.events),
        traces: asArray<JsonObject>(tracesPayload?.traces),
        platformTools: asArray<JsonObject>(platformToolsPayload?.tools),
        platformToolAudit: asArray<JsonObject>(platformToolAuditPayload?.events),
        writebackTasks: asArray<WritebackTaskRow>(writebackPayload?.tasks),
        learningThresholds: asObject(learningPayload?.thresholds),
        learningEvents: asArray<JsonObject>(learningPayload?.events),
        productAttention: productAttentionPayload ?? {},
        modelRuns: asArray<JsonObject>(modelRunsPayload?.model_runs),
        promptVersions: asArray<JsonObject>(promptVersionsPayload?.prompt_versions),
        accountability: asObject(accountabilityPayload?.report),
        tenantFacts: asArray<JsonObject>(tenantFactsPayload?.facts),
        tenantProfile: asObject(tenantProfilePayload?.profile),
        workerRuns: asArray<JsonObject>(workerRunsPayload?.runs),
        worldgenRuns: asArray<JsonObject>(worldgenRunsPayload?.runs),
        detectiveSql: typeof detectiveSqlPayload?.sql === 'string' ? detectiveSqlPayload.sql : '',
        observability: asObject(observabilityPayload),
        worker: asObject(workerPayload?.worker),
      })
      setMessages([{ id: newMsgId(), role: 'assistant', text: greetingFor(pendingQueue(log, payload.decision)) }])
      setLoadState('ready')
    }
    load().catch((e) => {
      if (controller.signal.aborted) return
      setError(e instanceof Error ? e.message : String(e))
      setLoadState('error')
    })
    return () => controller.abort()
  }, [reloadKey])

  useEffect(() => () => {
    transitionCtrl.current?.abort()
    chatCtrl.current?.abort()
  }, [])
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])
  useEffect(() => {
    const closeOverlay = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setApprovalOpen(false)
      setActiveWorkspace(null)
      if (isOverlayViewport()) setSidebarOpen(false)
    }
    window.addEventListener('keydown', closeOverlay)
    return () => window.removeEventListener('keydown', closeOverlay)
  }, [])

  const conn = loadState === 'ready' ? 'live' : loadState === 'error' ? 'error' : 'loading'

  // Opening a surface (approvals) or starting a new chat must reveal the chat on mobile, where the
  // sidebar is a full overlay; on desktop the persistent sidebar stays put.
  const openApprovals = () => {
    setActiveWorkspace(null)
    setApprovalOpen(true)
    if (isOverlayViewport()) setSidebarOpen(false)
  }
  const openWorkspace = (surface: WorkspaceSurface, options?: WorkspaceOpenOptions) => {
    setApprovalOpen(false)
    setActiveWorkspace({ surface, query: options?.query?.trim() || undefined })
    if (isOverlayViewport()) setSidebarOpen(false)
  }
  const newChat = () => {
    chatCtrl.current?.abort()
    setApprovalOpen(false)
    setActiveWorkspace(null)
    setMessages([{ id: newMsgId(), role: 'assistant', text: greetingFor(pendingQueue(decisions, data?.decision)) }])
    if (isOverlayViewport()) setSidebarOpen(false)
  }

  const send = (text: string) => {
    const fallback = replyFor(text, data, pendingQueue(decisions, data?.decision))
    const assistantId = newMsgId()
    const controller = new AbortController()
    chatCtrl.current = controller
    setMessages((prev) => [
      ...prev,
      { id: newMsgId(), role: 'user', text },
      { id: assistantId, role: 'assistant', text: 'Checking current ShelfWise state...' },
    ])
    postChat(text, controller.signal)
      .then((answer) => {
        const clean = answer.trim() || fallback
        setMessages((prev) => prev.map((message) => (
          message.id === assistantId ? { ...message, text: clean } : message
        )))
      })
      .catch(() => {
        setMessages((prev) => prev.map((message) => (
          message.id === assistantId ? { ...message, text: fallback } : message
        )))
      })
  }

  const resolve = (id: string, kind: 'approve' | 'reject') => {
    if (busyId) return
    transitionCtrl.current?.abort()
    const controller = new AbortController()
    transitionCtrl.current = controller
    setBusyId(id)
    postTransition(id, kind, controller.signal)
      .then((result) => {
        const { decision, learning_event } = result
        const nextById = new Map(decisions.map((item) => [item.id, item]))
        nextById.set(decision.id, decision)
        const currentDecision = data?.decision?.id === decision.id ? decision : data?.decision
        const remaining = pendingQueue(Array.from(nextById.values()), currentDecision)
        const queueNote = remaining.length === 0
          ? ' Queue clear.'
          : ` ${remaining.length} approval${remaining.length > 1 ? 's' : ''} still waiting.`
        setDecisions((cur) => {
          const byId = new Map(cur.map((d) => [d.id, d]))
          byId.set(decision.id, decision)
          return Array.from(byId.values())
        })
        setData((cur) => {
          if (!cur || cur.decision?.id !== decision.id) return cur
          return {
            ...cur,
            decision,
            learning: learning_event?.message
              ? { status: 'threshold_adjusted', message: learning_event.message }
              : cur.learning,
          }
        })
        setBusyId(null)
        setMessages((prev) => [
          ...prev,
          {
            id: newMsgId(),
            role: 'assistant',
            text: `${kind === 'approve' ? 'Approved' : 'Rejected'}: ${describeAction(decision.action)}. Logged to the audit trail.${queueNote}`,
          },
        ])
      })
      .catch((e) => {
        if (controller.signal.aborted) return
        setBusyId(null)
        setMessages((prev) => [
          ...prev,
          {
            id: newMsgId(),
            role: 'assistant',
            text: `That ${kind} did not go through. Nothing changed. Try again.`,
          },
        ])
      })
  }

  return (
    <div className="app-shell">
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onSelectRecent={() => {
          if (isOverlayViewport()) setSidebarOpen(false)
        }}
        onNewChat={newChat}
        onOpenApprovals={openApprovals}
        onOpenWorkspace={openWorkspace}
        activeWorkspace={activeWorkspace?.surface ?? null}
        recents={recents}
        queue={queue}
        data={data}
        seed={seed}
        ops={ops}
        recoveredToday={recoveredToday}
      />

      <div className="app-main">
        <header className="topbar">
          <button
            className="icon-btn"
            type="button"
            aria-label={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            aria-expanded={sidebarOpen}
            onClick={() => {
              setApprovalOpen(false)
              setSidebarOpen((v) => !v)
            }}
          >
            <UiIcon name="menu" />
          </button>
          <span className="brand">
            <span className="brand-mark" />
            <span className="brand-name">ShelfWise</span>
          </span>
          <div className="topbar-right">
            <InferencePill config={data?.inference} />
            <span className={`conn conn-${conn}`}>
              <span className="conn-dot" /> {conn === 'live' ? 'Live' : conn === 'error' ? 'Offline' : 'Connecting'}
            </span>
          </div>
        </header>

        {data ? (
          <StatusBar
            queue={queue}
            open={approvalOpen}
            onToggle={() => {
              setActiveWorkspace(null)
              setApprovalOpen((v) => !v)
            }}
          />
        ) : null}

        {/* Everything below the status bar is one positioned zone so the approval panel can slide
            down from directly under it - the global chrome above is never covered or clipped. */}
        <div className={`chat-zone${activeWorkspace ? ' has-workspace' : ''}`}>
          {activeWorkspace ? (
            <WorkspaceScreen
              surface={activeWorkspace.surface}
              initialQuery={activeWorkspace.query}
              onBack={() => setActiveWorkspace(null)}
              data={data}
              seed={seed}
              queue={queue}
              ops={ops}
              recoveredToday={recoveredToday}
            />
          ) : (
            <>
              <main className="chat" ref={scrollRef}>
                <div className={`chat-inner ${!error && messages.length <= 1 ? 'is-sparse' : ''}`}>
                  {error ? (
                    <div className="row assistant-row">
                      <div className="avatar" aria-hidden>
                        <span className="avatar-mark" />
                      </div>
                      <div className="bubble assistant">
                        <p>I could not reach the cascade. Check the backend and retry.</p>
                        <p className="muted">No store data changed.</p>
                        <button className="btn btn-secondary" type="button" onClick={() => setReloadKey((v) => v + 1)}>
                          Retry
                        </button>
                      </div>
                    </div>
                  ) : null}

                  {loadState === 'loading' && messages.length === 0 ? (
                    <div className="row assistant-row">
                      <div className="avatar" aria-hidden>
                        <span className="avatar-mark" />
                      </div>
                      <div className="bubble assistant typing">
                        <span />
                        <span />
                        <span />
                      </div>
                    </div>
                  ) : null}

                  <ErrorBoundary>
                    {messages.map((m) =>
                      m.role === 'user' ? <UserBubble key={m.id} text={m.text} /> : <AssistantBubble key={m.id} text={m.text} />,
                    )}
                  </ErrorBoundary>
                </div>
              </main>

              <Composer onSend={send} onOpenApprovals={openApprovals} />

              {approvalOpen ? (
                <div className="approval-scrim is-open" onClick={() => setApprovalOpen(false)}>
                  <div id="approval-panel" className="approval-panel open" role="region" onClick={(e) => e.stopPropagation()} aria-label="Approval queue">
                    <ApprovalPanel
                      queue={queue}
                      resolved={resolved}
                      currentId={data?.decision?.id}
                      evidence={data?.evidence}
                      busyId={busyId}
                      onApprove={(id) => resolve(id, 'approve')}
                      onReject={(id) => resolve(id, 'reject')}
                    />
                  </div>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default App

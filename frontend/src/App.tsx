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
type DecisionLogResponse = { decisions?: Decision[] }
type TransitionResult = { decision: Decision; learning_event?: LearningEvent | null }
type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type Tone = 'ok' | 'warn' | 'risk' | 'info' | 'mute' | 'accent'

// Chat is Q&A only now - decisions live in the persistent status bar + slide-down panel, never
// embedded as an interactive card inside a message (that was duplicate UI: the same pending
// decision rendered twice, in two different places, with two different ways to act on it).
type ChatMessage = { id: string; role: 'user' | 'assistant'; text: string }
type UiIconName = 'close' | 'menu' | 'mic' | 'moon' | 'send' | 'stop' | 'sun'

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
const DEFAULT_API_BASE = 'http://localhost:8000'
const DEMO_PATH = '/demo/golden'

function configuredBase(): string {
  const env = import.meta.env as Record<string, string | undefined>
  return (env.VITE_API_BASE ?? env.VITE_API_BASE_URL ?? '').trim()
}
function apiKey(): string {
  return ((import.meta.env as Record<string, string | undefined>).VITE_API_KEY ?? '').trim()
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
async function fetchJson<T>(path: string, init: RequestInit, signal: AbortSignal): Promise<T> {
  let lastError = 'Unknown error'
  for (const url of requestUrls(path)) {
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
async function fetchDecisionLog(signal: AbortSignal): Promise<Decision[]> {
  const payload = await fetchJson<DecisionLogResponse>('/decisions', { method: 'GET' }, signal)
  return Array.isArray(payload.decisions) ? payload.decisions : []
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
// on desktop, an overlay on mobile. Detail opens as a settings-style slide-in page
// (a page stack with a back control), so the root list stays small and never scrolls.
// ---------------------------------------------------------------------------
type SidebarPage = 'store' | 'stock' | 'product' | 'order' | 'batch' | 'delivery' | 'outcomes' | 'settings'
type Recent = { id: string; title: string; active?: boolean }

// Plain-language names a store person reads without a manual. Each surface is a COLLECTION
// (stock is never one product - a real store carries thousands), shaped to scale from today's
// seeded items to the full assortment when the store-wide API lands.
const PAGE_TITLE: Record<SidebarPage, string> = {
  store: 'My store',
  stock: 'Stock',
  product: 'Product', // the header shows the selected product's name instead
  order: 'To order',
  batch: 'Sell first',
  delivery: 'Short delivery',
  outcomes: "Today's results",
  settings: 'Settings',
}

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
  onOpen,
}: {
  label: string
  sku?: string
  value?: string
  tone?: Tone
  onOpen: () => void
}) {
  return (
    <button className="nav-row" type="button" onClick={onOpen}>
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

function Sidebar({
  open,
  onClose,
  onSelectRecent,
  onNewChat,
  onOpenApprovals,
  recents,
  queue,
  data,
  seed,
  recoveredToday,
}: {
  open: boolean
  onClose: () => void
  onSelectRecent: () => void
  onNewChat: () => void
  onOpenApprovals: () => void
  recents: Recent[]
  queue: Decision[]
  data: GoldenDemo | null
  seed: SeedSummary | null
  recoveredToday: string | null
}) {
  const intel = data?.store_intelligence
  const cover = intel?.supplier_cover
  const batches = intel?.batch_split?.fefo_batches ?? []

  // Identity zone - real fields, no fabrication: role routed by the backend, store from the seed.
  // A person's name fills in when company-account login lands (see roadmap).
  const role = data?.decision?.role ? formatLabel(data.decision.role) : 'Store manager'
  const store = seed?.location ? formatLabel(seed.location) : 'Store'
  const monogram = role.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase()

  // Stock is a COLLECTION: every product the data layer reports on today, in one list that
  // scales to the full assortment. A product is "low" when it needs ordering now.
  type ProductId = 'seed' | 'cover'
  const [productId, setProductId] = useState<ProductId | null>(null)
  const coverDays = daysNumber(cover?.days_of_supply)
  const products: Array<{ id: ProductId; name: string; sub?: string; value: string; low: boolean }> = []
  if (seed) {
    products.push({
      id: 'seed',
      name: seed.product_name ?? humanizeOperationalText(seed.sku ?? 'Product'),
      sub: [seed.category, seed.supplier].filter(Boolean).join(' · ') || undefined,
      value: seed.units_on_hand != null ? `${seed.units_on_hand} on hand` : '-',
      low: seed.units_on_hand != null && seed.reorder_point != null && seed.units_on_hand <= seed.reorder_point,
    })
  }
  if (cover?.sku) {
    products.push({
      id: 'cover',
      name: humanizeOperationalText(cover.sku),
      value: coverDays != null ? `${coverDays} days left` : cover.units_on_hand != null ? `${cover.units_on_hand} on hand` : '-',
      low: true,
    })
  }
  // The shopping list: products that will run out before a normal order can arrive.
  const orderLines = cover?.transfer_units_recommended ? [cover] : []
  const productName =
    productId === 'cover'
      ? humanizeOperationalText(cover?.sku ?? 'Product')
      : seed?.product_name ?? humanizeOperationalText(seed?.sku ?? 'Product')

  // Page stack: back always pops one level (root -> store -> stock -> product -> back...).
  const [stack, setStack] = useState<SidebarPage[]>([])
  const page = stack[stack.length - 1] ?? null
  const push = (p: SidebarPage) => setStack((s) => [...s, p])
  const back = () => setStack((s) => s.slice(0, -1))
  const backRef = useRef<HTMLButtonElement | null>(null)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    if (!open) {
      setStack([])
      setProductId(null)
      setSearching(false)
      setQuery('')
    }
  }, [open])
  useEffect(() => {
    if (page) backRef.current?.focus()
  }, [page])

  const shownRecents = recents.filter((r) => !query || r.title.toLowerCase().includes(query.toLowerCase()))

  return (
    <>
      <aside className={`sidebar ${open ? 'is-open' : 'is-collapsed'}`} aria-label="Navigation" aria-hidden={!open}>
        <div className="sidebar-inner">
          <div className="sidebar-head">
            {page ? (
              <button className="drawer-back" type="button" ref={backRef} onClick={back}>
                <span className="nav-chevron back" aria-hidden />
                {page === 'product' ? productName : PAGE_TITLE[page]}
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
            {/* ROOT - create + find, then continuity + surfaces */}
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
                      placeholder="Search conversations..."
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      aria-label="Search conversations"
                    />
                  ) : null}
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
                </div>

                <div className="sidebar-section">
                  <NavRow label="Approvals & history" value={queue.length ? `${queue.length} waiting` : 'clear'} onOpen={onOpenApprovals} />
                  <NavRow label="My store" value={store} onOpen={() => push('store')} />
                </div>
              </>
            ) : null}

            {/* MY STORE - store-wide categories; every row is a collection, not one product's card */}
            {page === 'store' ? (
              <div className="sidebar-section">
                <NavRow
                  label="Stock"
                  value={`${products.length} product${products.length === 1 ? '' : 's'}`}
                  onOpen={() => push('stock')}
                />
                {orderLines.length ? (
                  <NavRow
                    label="To order"
                    value={`${orderLines.length} item${orderLines.length === 1 ? '' : 's'}`}
                    tone="warn"
                    onOpen={() => push('order')}
                  />
                ) : null}
                {intel?.batch_split ? (
                  <NavRow
                    label="Sell first"
                    sku={intel.batch_split.sku}
                    value={`${formatValue(intel.batch_split.priority_sell_units)} units`}
                    onOpen={() => push('batch')}
                  />
                ) : null}
                {intel?.delivery_reconciliation ? (
                  <NavRow
                    label="Short delivery"
                    sku={intel.delivery_reconciliation.sku}
                    value={`${formatValue(intel.delivery_reconciliation.missing_units)} short`}
                    onOpen={() => push('delivery')}
                  />
                ) : null}
                {recoveredToday ? <NavRow label="Today's results" value={recoveredToday} onOpen={() => push('outcomes')} /> : null}
              </div>
            ) : null}

            {/* STOCK - the product list; tapping a product opens its card */}
            {page === 'stock' ? (
              <div className="sidebar-section">
                {products.length ? (
                  products.map((p) => (
                    <NavRow
                      key={p.id}
                      label={p.name}
                      sku={p.sub}
                      value={p.value}
                      tone={p.low ? 'warn' : undefined}
                      onOpen={() => {
                        setProductId(p.id)
                        push('product')
                      }}
                    />
                  ))
                ) : (
                  <p className="side-empty">No stock data yet.</p>
                )}
              </div>
            ) : null}

            {/* TO ORDER - the shopping list; each line opens the product it belongs to */}
            {page === 'order' ? (
              <div className="sidebar-section">
                <p className="side-empty">Will run out before a normal order arrives.</p>
                {orderLines.map((o) => (
                  <NavRow
                    key={o.sku}
                    label={humanizeOperationalText(o.sku ?? 'Product')}
                    value={`${formatValue(o.transfer_units_recommended)} units`}
                    tone="warn"
                    onOpen={() => {
                      setProductId('cover')
                      push('product')
                    }}
                  />
                ))}
              </div>
            ) : null}

            {/* PRODUCT - one product's card; the single home for its numbers */}
            {page === 'product' ? (
              <section className="rail-section">
                {productId === 'seed' && seed ? (
                  <>
                    <p className="snapshot-title">
                      {seed.product_name ?? humanizeOperationalText(seed.sku ?? 'Product')}
                      {seed.category ? <span className="snapshot-cat"> · {seed.category}</span> : null}
                    </p>
                    <dl className="kv">
                      <div>
                        <dt>On hand</dt>
                        <dd className="tnum">{formatValue(seed.units_on_hand)} units</dd>
                      </div>
                      <div>
                        <dt>Reorder at</dt>
                        <dd className="tnum">{formatValue(seed.reorder_point)}</dd>
                      </div>
                      <div>
                        <dt>Expires in</dt>
                        <dd className={`tnum ${seed.days_to_expiry != null && seed.days_to_expiry <= 3 ? 'tone-warn' : ''}`}>
                          {seed.days_to_expiry != null ? `${seed.days_to_expiry} days` : '-'}
                        </dd>
                      </div>
                      <div>
                        <dt>Supplier</dt>
                        <dd className="tnum">
                          {seed.supplier ?? '-'}
                          {seed.supplier_lead_time_days ? `, ${Math.round(Number(seed.supplier_lead_time_days))}d lead` : ''}
                        </dd>
                      </div>
                    </dl>
                  </>
                ) : null}
                {productId === 'cover' && cover ? (
                  <>
                    <p className="snapshot-title">{humanizeOperationalText(cover.sku ?? 'Product')}</p>
                    <dl className="kv">
                      <div>
                        <dt>On hand</dt>
                        <dd className="tnum">{formatValue(cover.units_on_hand)} units</dd>
                      </div>
                      <div>
                        <dt>Sells per day</dt>
                        <dd className="tnum">{daysNumber(cover.forecast_daily_units) ?? '-'}</dd>
                      </div>
                      <div>
                        <dt>Days left</dt>
                        <dd className="tnum tone-warn">{coverDays != null ? `${coverDays} days` : '-'}</dd>
                      </div>
                      <div>
                        <dt>Needed now</dt>
                        <dd className="tnum">{formatValue(cover.transfer_units_recommended)} units</dd>
                      </div>
                    </dl>
                    {cover.conclusion ? <p className="muted">{humanizeOperationalText(cover.conclusion)}</p> : null}
                  </>
                ) : null}
              </section>
            ) : null}

            {page === 'batch' ? (
              <section className="rail-section">
                {batches.length > 0 ? (
                  <ul className="lot-list">
                    {batches.map((b, i) => (
                      <li className="lot-row" key={b.lot ?? i}>
                        <span className="lot-id tnum">{b.lot ?? '-'}</span>
                        <span className="tnum">{formatValue(b.units)} units</span>
                        <span className="tnum">{b.days_to_expiry != null ? `${b.days_to_expiry}d left` : '-'}</span>
                        <span
                          className={`lot-status tone-${
                            b.stock_status === 'priority_sell' ? 'warn' : b.stock_status === 'blocked' ? 'risk' : 'mute'
                          }`}
                        >
                          {b.stock_status === 'priority_sell' ? 'Sell first' : formatLabel(b.stock_status ?? 'normal')}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : null}
                {intel?.batch_split?.conclusion || batches.length === 0 ? (
                  <p className="muted">{humanizeOperationalText(intel?.batch_split?.conclusion ?? 'No batch detail available.')}</p>
                ) : null}
              </section>
            ) : null}

            {page === 'delivery' ? (
              <section className="rail-section">
                <dl className="kv">
                  <div>
                    <dt>Ordered</dt>
                    <dd className="tnum">{formatValue(intel?.delivery_reconciliation?.ordered_units)}</dd>
                  </div>
                  <div>
                    <dt>Received</dt>
                    <dd className="tnum">{formatValue(intel?.delivery_reconciliation?.received_units)}</dd>
                  </div>
                  <div>
                    <dt>Accepted</dt>
                    <dd className="tnum">{formatValue(intel?.delivery_reconciliation?.accepted_units)}</dd>
                  </div>
                  <div>
                    <dt>Short dated</dt>
                    <dd className="tnum">{formatValue(intel?.delivery_reconciliation?.short_dated_units)}</dd>
                  </div>
                  <div>
                    <dt>Fill rate</dt>
                    <dd className="tnum">{formatValue(intel?.delivery_reconciliation?.supplier_fill_rate)}</dd>
                  </div>
                </dl>
                {intel?.delivery_reconciliation?.conclusion ? (
                  <p className="muted">{humanizeOperationalText(intel.delivery_reconciliation.conclusion)}</p>
                ) : null}
              </section>
            ) : null}

            {page === 'outcomes' ? (
              <section className="rail-section">
                {recoveredToday ? (
                  <p className="outcome-line">
                    <span className="tnum tone-ok">{recoveredToday}</span> recovered today
                  </p>
                ) : (
                  <p className="muted">No results recorded yet today.</p>
                )}
              </section>
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
                </dl>
                <p className="muted">Company account sign-in is coming soon.</p>
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
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  // Sidebar is persistent on desktop (open by default), an overlay on mobile (closed by default).
  const [sidebarOpen, setSidebarOpen] = useState(() => !isOverlayViewport())
  const [approvalOpen, setApprovalOpen] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const transitionCtrl = useRef<AbortController | null>(null)
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
    async function load() {
      const payload = await fetchDemo(DEMO_PATH, controller.signal)
      let log: Decision[] = payload.decision ? [payload.decision] : []
      try {
        log = await fetchDecisionLog(controller.signal)
      } catch {
        /* decision log optional */
      }
      let seedData: SeedSummary | null = null
      try {
        const summary = await fetchJson<{ seed_data?: SeedSummary }>('/data/seed/summary', { method: 'GET' }, controller.signal)
        seedData = summary.seed_data ?? null
      } catch {
        /* snapshot optional */
      }
      if (controller.signal.aborted) return
      setData(payload)
      setDecisions(log)
      setSeed(seedData)
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

  useEffect(() => () => transitionCtrl.current?.abort(), [])
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])
  useEffect(() => {
    const closeOverlay = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setApprovalOpen(false)
      if (isOverlayViewport()) setSidebarOpen(false)
    }
    window.addEventListener('keydown', closeOverlay)
    return () => window.removeEventListener('keydown', closeOverlay)
  }, [])

  const conn = loadState === 'ready' ? 'live' : loadState === 'error' ? 'error' : 'loading'

  // Opening a surface (approvals) or starting a new chat must reveal the chat on mobile, where the
  // sidebar is a full overlay; on desktop the persistent sidebar stays put.
  const openApprovals = () => {
    setApprovalOpen(true)
    if (isOverlayViewport()) setSidebarOpen(false)
  }
  const newChat = () => {
    setApprovalOpen(false)
    setMessages([{ id: newMsgId(), role: 'assistant', text: greetingFor(pendingQueue(decisions, data?.decision)) }])
    if (isOverlayViewport()) setSidebarOpen(false)
  }

  const send = (text: string) => {
    setMessages((prev) => {
      const reply = replyFor(text, data, pendingQueue(decisions, data?.decision))
      return [...prev, { id: newMsgId(), role: 'user', text }, { id: newMsgId(), role: 'assistant', text: reply }]
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
        recents={recents}
        queue={queue}
        data={data}
        seed={seed}
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
            <span className={`conn conn-${conn}`}>
              <span className="conn-dot" /> {conn === 'live' ? 'Live' : conn === 'error' ? 'Offline' : 'Connecting'}
            </span>
          </div>
        </header>

        {data ? <StatusBar queue={queue} open={approvalOpen} onToggle={() => setApprovalOpen((v) => !v)} /> : null}

        {/* Everything below the status bar is one positioned zone so the approval panel can slide
            down from directly under it - the global chrome above is never covered or clipped. */}
        <div className="chat-zone">
      <main className="chat" ref={scrollRef}>
        <div className={`chat-inner ${!error && messages.length <= 1 ? 'is-sparse' : ''}`}>
          {error ? (
            <div className="row assistant-row">
              <div className="avatar" aria-hidden>
                <span className="avatar-mark" />
              </div>
              <div className="bubble assistant">
                <p>I could not reach the cascade. Check the backend and retry.</p>
                <p className="muted">{error}</p>
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
            {messages.map((m) => (m.role === 'user' ? <UserBubble key={m.id} text={m.text} /> : <AssistantBubble key={m.id} text={m.text} />))}
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
        </div>
      </div>
    </div>
  )
}

export default App

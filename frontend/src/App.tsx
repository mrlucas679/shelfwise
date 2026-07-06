import { Component, type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
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
  outcome?: {
    units_cleared?: number
    waste_units?: number
    rand_recovered?: unknown
    success_score?: string
  }
  learning_event?: LearningEvent
  write_back?: { status?: string; target?: string }
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
type StoreIntelligence = {
  batch_split?: {
    total_units?: number
    priority_sell_units?: number
    normal_units?: number
    blocked_units?: number
    conclusion?: string
  }
  delivery_reconciliation?: {
    ordered_units?: number
    received_units?: number
    missing_units?: number
    short_dated_units?: number
    supplier_fill_rate?: string
    status?: string
    conclusion?: string
  }
  supplier_cover?: {
    days_of_supply?: string
    gap_before_delivery_units?: number
    transfer_units_recommended?: number
    recommended_action?: string
    conclusion?: string
  }
  learning_summary?: {
    sell_through_delta_units?: number
    waste_delta_units?: number
    score?: string
    lesson?: string
  }
}
type LearningEvent = {
  id?: string
  decision_id?: string
  sku?: string
  metric?: string
  previous_threshold?: number
  updated_threshold?: number
  delta_units?: number
  outcome?: Decision['outcome']
  message?: string
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
type TransitionResult = { decision: Decision; learning_event?: LearningEvent | null }
type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type ScenarioMode = 'approval' | 'critic'
type Tone = 'ok' | 'warn' | 'risk' | 'info' | 'mute' | 'accent'
type Metric = { label: string; value: string; tone?: Tone }
type Priority = { label: string; value: string; tone: Tone }

// ---------------------------------------------------------------------------
// API - same endpoints, with blueprint env-var compatibility + optional key.
// ---------------------------------------------------------------------------
const DEFAULT_API_BASE = 'http://localhost:8000'
const DEMO_PATHS: Record<ScenarioMode, string> = {
  approval: '/demo/golden',
  critic: '/demo/critic-rejection',
}

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

const fetchDemo = (path: string, signal: AbortSignal) =>
  fetchJson<GoldenDemo>(path, { method: 'GET' }, signal)

async function postTransition(
  id: string,
  transition: 'approve' | 'reject',
  signal: AbortSignal,
): Promise<TransitionResult> {
  const payload = await fetchJson<{ decision?: Decision; learning_event?: LearningEvent | null }>(
    `/decisions/${encodeURIComponent(id)}/${transition}`,
    { method: 'POST' },
    signal,
  )
  if (!payload.decision) throw new Error('Transition response did not include a decision.')
  return { decision: payload.decision, learning_event: payload.learning_event ?? null }
}

// ---------------------------------------------------------------------------
// Formatting - money as R, provenance via SourceRef, confidence as %.
// ---------------------------------------------------------------------------
function formatLabel(value: unknown): string {
  return String(value ?? 'unknown')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Render a Rand figure from Money objects, "ZAR 378.00"/"R378" strings, or raw display strings. */
function formatMoneyish(value: unknown): string | null {
  if (value && typeof value === 'object') {
    const m = value as { minor_units?: number; currency?: string; amount?: string }
    if (typeof m.minor_units === 'number') {
      return `R${Math.round(m.minor_units / 100).toLocaleString('en-ZA')}`
    }
  }
  if (typeof value === 'string') {
    const m = value.match(/^\s*(?:ZAR|R)\s*([\d,]+(?:\.\d+)?)\s*$/i)
    if (m) return `R${Math.round(Number(m[1].replace(/,/g, ''))).toLocaleString('en-ZA')}`
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
  const locator = source.locator ? `#${source.locator}` : ''
  return `${source.ref ?? 'unknown'}${locator}`
}

function formatParamValue(key: string, value: unknown): string {
  const numeric = Number(value)
  if (Number.isFinite(numeric) && key.toLowerCase().includes('pct')) {
    return `${Math.round((numeric > 1 ? numeric / 100 : numeric) * 100)}%`
  }
  return formatValue(value)
}

function riskTone(tier?: string): Tone {
  const t = (tier ?? '').toLowerCase()
  if (t === 'high') return 'risk'
  if (t === 'medium') return 'warn'
  return 'ok'
}
function statusTone(status?: string): Tone {
  const s = (status ?? '').toLowerCase()
  if (s === 'ok' || s === 'approved') return 'ok'
  if (s === 'rejected' || s === 'error' || s === 'timeout') return 'risk'
  if (s === 'pending' || s === 'degraded') return 'warn'
  return 'mute'
}

const GLYPH: Record<Tone, string> = { ok: '●', warn: '◆', risk: '▲', info: '■', mute: '◐', accent: '●' }
const toneVar: Record<Tone, string> = {
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  risk: 'var(--risk)',
  info: 'var(--info)',
  mute: 'var(--mute)',
  accent: 'var(--accent)',
}

function listWithAnd(items: string[]): string {
  if (items.length === 0) return 'No agent'
  if (items.length === 1) return items[0]
  if (items.length === 2) return `${items[0]} and ${items[1]}`
  return `${items.slice(0, -1).join(', ')}, and ${items[items.length - 1]}`
}

function firstActionEvidence(evidence?: EvidenceObject[], actionType?: string): EvidenceObject | undefined {
  const items = evidence ?? []
  return (
    items.find((item) => item.recommended_action?.type === actionType && item.requires_human_review) ??
    items.find((item) => item.recommended_action?.type === actionType) ??
    items.find((item) => item.requires_human_review) ??
    items.find((item) => item.recommended_action) ??
    items[0]
  )
}

function recommendation(data: GoldenDemo | null): RecommendedAction | undefined {
  return data?.decision?.action ?? firstActionEvidence(data?.evidence)?.recommended_action
}

function describeAction(action?: RecommendedAction): string {
  if (!action) return 'No action returned'
  const discount = action.params?.discount_pct
  const suffix =
    typeof discount === 'string' || typeof discount === 'number'
      ? ` ${formatParamValue('discount_pct', Number(discount))}`
      : ''
  return `${formatLabel(action.type)}${suffix}`
}

function buildActionMetrics(action?: RecommendedAction, evidence?: EvidenceObject): Metric[] {
  const metrics: Metric[] = []
  const seen = new Set<string>()
  const add = (label: string, value: string, tone: Tone = 'mute') => {
    const key = label.toLowerCase()
    if (!value || value === '-' || seen.has(key)) return
    metrics.push({ label, value, tone })
    seen.add(key)
  }

  Object.entries(action?.params ?? {}).forEach(([key, value]) => {
    add(formatLabel(key), formatParamValue(key, value), key.toLowerCase().includes('pct') ? 'accent' : 'mute')
  })

  ;(evidence?.supporting_data ?? []).forEach((fact) => {
    if (metrics.length >= 5) return
    const label = formatLabel(fact.fact ?? fact.method ?? 'Fact')
    const value = formatValue(fact.value)
    const tone = formatMoneyish(fact.value) ? 'ok' : 'mute'
    add(label, value, tone)
  })

  return metrics.slice(0, 5)
}

function storeIntelligenceMetrics(data?: GoldenDemo | null): Metric[] {
  const intelligence = data?.store_intelligence
  const metrics: Metric[] = []
  const add = (label: string, value: unknown, tone: Tone = 'mute') => {
    if (value === undefined || value === null || value === '') return
    metrics.push({ label, value: formatValue(value), tone })
  }

  add('Urgent batch', intelligence?.batch_split?.priority_sell_units, 'warn')
  add('Normal stock', intelligence?.batch_split?.normal_units, 'ok')
  add('Missing delivery', intelligence?.delivery_reconciliation?.missing_units, 'risk')
  add('Transfer now', intelligence?.supplier_cover?.transfer_units_recommended, 'accent')
  add('Learning score', intelligence?.learning_summary?.score, 'info')
  return metrics
}

function buildProofLine(evidence?: EvidenceObject[]): string {
  const names = (evidence ?? []).map((item) => formatLabel(item.agent)).filter(Boolean)
  if (names.length === 0) return 'No agent proof returned yet.'
  return `${listWithAnd(names)} checks completed.`
}

function executiveAnswer(data: GoldenDemo | null): { heading: string; body: string } {
  if (!data) {
    return { heading: 'Waiting for the demo cascade', body: 'ShelfWise will summarize the next decision here.' }
  }
  const action = recommendation(data)
  const evidence = firstActionEvidence(data.evidence, action?.type)
  const metrics = [...buildActionMetrics(action, evidence), ...storeIntelligenceMetrics(data)]
  const metricText = metrics.length > 0 ? metrics.slice(0, 3).map((m) => `${m.label}: ${m.value}`).join(', ') : null
  const summary = data.decision?.summary ?? evidence?.conclusion ?? 'The cascade returned a single operational decision.'
  const body = metricText
    ? `${summary} The useful numbers are ${metricText}.`
    : summary
  return {
    heading: describeAction(action),
    body,
  }
}

function buildPriorities(data: GoldenDemo): Priority[] {
  const action = recommendation(data)
  const evidence = firstActionEvidence(data.evidence, action?.type)
  const metrics = buildActionMetrics(action, evidence)
  const intelligence = storeIntelligenceMetrics(data)
  const topMetric = metrics.find((m) => m.tone === 'ok') ?? metrics[0]
  const reviewRequired = Boolean(data.evidence?.some((item) => item.requires_human_review))

  return [
    {
      label: 'Approval',
      value: formatLabel(data.decision?.status ?? 'pending'),
      tone: statusTone(data.decision?.status ?? 'pending'),
    },
    {
      label: 'Action risk',
      value: formatLabel(action?.risk_tier ?? 'low'),
      tone: riskTone(action?.risk_tier),
    },
    {
      label: intelligence[2]?.label ?? topMetric?.label ?? 'Action',
      value: intelligence[2]?.value ?? topMetric?.value ?? describeAction(action),
      tone: intelligence[2]?.tone ?? topMetric?.tone ?? 'accent',
    },
    {
      label: 'Human review',
      value: reviewRequired ? 'required' : 'not required',
      tone: reviewRequired ? 'warn' : 'ok',
    },
  ]
}

function traceSummary(trace?: TraceSpan[]) {
  const spans = trace ?? []
  const totalMs = spans.reduce((n, s) => n + (s.ms ?? 0), 0)
  const failed = spans.filter((s) => statusTone(s.status) === 'risk').length
  const slowest = spans.reduce<TraceSpan | undefined>((acc, span) => {
    if (!acc || (span.ms ?? 0) > (acc.ms ?? 0)) return span
    return acc
  }, undefined)

  return { spans, totalMs, failed, slowest }
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------
function StatusGlyph({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`glyph tone-${tone}`}>
      <span className="glyph-shape" aria-hidden>
        {GLYPH[tone]}
      </span>
      <span>{label}</span>
    </span>
  )
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

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section className="panel alert" role="alert">
      <div className="section-kicker">API error</div>
      <h2>Demo cascade unavailable</h2>
      <p>{message}</p>
      <button className="btn btn-secondary" type="button" onClick={onRetry}>
        Retry
      </button>
    </section>
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
      <section className="panel alert" role="alert">
        <h2>Something failed to render</h2>
        <button className="btn btn-secondary" type="button" onClick={() => this.setState({ failed: false })}>
          Retry
        </button>
      </section>
    )
  }
}

// ---------------------------------------------------------------------------
// Conversation surface
// ---------------------------------------------------------------------------
function ChatHeader({ data, conn }: { data: GoldenDemo | null; conn: string }) {
  return (
    <div className="chat-header">
      <div>
        <div className="section-kicker">Executive answer</div>
        <h1>ShelfWise decision layer</h1>
      </div>
      <div className="chat-meta">
        <span className={`conn conn-${conn}`}>
          <span className="conn-dot" /> {conn}
        </span>
        <span className="tnum">{data?.correlation_id ?? 'no correlation id'}</span>
      </div>
    </div>
  )
}

function ActionBlock({
  data,
  busy,
  error,
  reasoningOpen,
  onApprove,
  onReject,
  onToggleReasoning,
}: {
  data: GoldenDemo
  busy: 'approve' | 'reject' | null
  error: string | null
  reasoningOpen: boolean
  onApprove: () => void
  onReject: () => void
  onToggleReasoning: () => void
}) {
  const action = recommendation(data)
  const evidence = firstActionEvidence(data.evidence, action?.type)
  const metrics = buildActionMetrics(action, evidence)
  const status = data.decision?.status ?? 'pending'
  const pending = status.toLowerCase() === 'pending'
  const tone = riskTone(action?.risk_tier)

  return (
    <section className="action-record" style={{ '--rail': toneVar[tone] } as CSSProperties} aria-label="Recommended action">
      <div className="action-main">
        <div className="action-copy">
          <div className="section-kicker">Recommended action</div>
          <h2>{describeAction(action)}</h2>
          <p>{evidence?.conclusion ?? data.decision?.summary ?? 'No recommendation detail was returned.'}</p>
        </div>
        <StatusGlyph tone={pending ? 'warn' : statusTone(status)} label={formatLabel(status)} />
      </div>

      {metrics.length > 0 ? (
        <dl className="metric-strip">
          {metrics.map((metric) => (
            <div key={metric.label}>
              <dt>{metric.label}</dt>
              <dd className={`tnum tone-${metric.tone ?? 'mute'}`}>{metric.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <div className="action-controls">
        <button className="btn btn-primary" type="button" disabled={!pending || busy !== null} onClick={onApprove}>
          {busy === 'approve' ? 'Approving' : 'Approve'}
        </button>
        <button className="btn btn-secondary" type="button" disabled={!pending || busy !== null} onClick={onReject}>
          {busy === 'reject' ? 'Rejecting' : 'Reject'}
        </button>
        <button className="btn btn-ghost" type="button" aria-expanded={reasoningOpen} onClick={onToggleReasoning}>
          {reasoningOpen ? 'Hide reasoning' : 'Show reasoning'}
        </button>
      </div>

      {error ? (
        <p className="action-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  )
}

function ReasoningChain({ evidence }: { evidence?: EvidenceObject[] }) {
  const items = evidence ?? []
  const [active, setActive] = useState(0)
  const selected = items[active]

  if (items.length === 0) return <p className="empty-line">No reasoning chain returned.</p>

  return (
    <section className="reasoning" aria-label="Reasoning chain">
      <div className="reasoning-head">
        <div>
          <div className="section-kicker">Reasoning chain</div>
          <h2>Compact agent steps</h2>
        </div>
        <span className="section-count tnum">{items.length} steps</span>
      </div>

      <div className="reason-grid">
        <div className="step-list" role="tablist" aria-label="Agent steps">
          {items.map((item, index) => {
            const isActive = index === active
            return (
              <button
                className={`step-row ${isActive ? 'is-active' : ''}`}
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-controls="reason-detail"
                id={`reason-step-${index}`}
                key={`${item.agent ?? 'agent'}-${index}`}
                onClick={() => setActive(index)}
              >
                <span className="step-index tnum">{String(index + 1).padStart(2, '0')}</span>
                <span className="step-text">
                  <span>{formatLabel(item.agent)}</span>
                  <small>{item.conclusion ?? 'No conclusion supplied.'}</small>
                </span>
                <Confidence value={item.confidence} />
              </button>
            )
          })}
        </div>

        <article
          className="reason-detail"
          id="reason-detail"
          role="tabpanel"
          aria-labelledby={`reason-step-${active}`}
          style={{ '--rail': toneVar[riskTone(selected?.recommended_action?.risk_tier)] } as CSSProperties}
        >
          <header>
            <span className="section-kicker">{formatLabel(selected?.agent)}</span>
            <h3>{selected?.conclusion ?? 'No conclusion supplied.'}</h3>
          </header>
          <FactTable facts={selected?.supporting_data ?? []} />
          <SourceLine sources={selected?.sources ?? []} />
        </article>
      </div>
    </section>
  )
}

function FactTable({ facts }: { facts: SupportingFact[] }) {
  if (facts.length === 0) return <p className="empty-line">No supporting facts returned for this step.</p>
  return (
    <dl className="facts">
      {facts.map((fact, index) => (
        <div className="fact-row" key={`${fact.fact ?? 'fact'}-${index}`}>
          <dt>{formatLabel(fact.fact ?? fact.method ?? 'Fact')}</dt>
          <dd className="fact-value tnum">{formatValue(fact.value)}</dd>
          <dd className="fact-source">{fact.source ?? fact.method ?? '-'}</dd>
        </div>
      ))}
    </dl>
  )
}

function SourceLine({ sources }: { sources: SourceRef[] }) {
  if (sources.length === 0) return null
  return (
    <p className="source-line">
      {sources.map((source, index) => (
        <span key={`${source.ref ?? 'source'}-${index}`} title={source.kind}>
          {formatSource(source)}
        </span>
      ))}
    </p>
  )
}

function Conversation({
  data,
  conn,
  busy,
  transitionError,
  onApprove,
  onReject,
}: {
  data: GoldenDemo
  conn: string
  busy: 'approve' | 'reject' | null
  transitionError: string | null
  onApprove: () => void
  onReject: () => void
}) {
  const [reasoningOpen, setReasoningOpen] = useState(false)
  const answer = executiveAnswer(data)

  return (
    <section className="chat-main panel" aria-label="Executive conversation">
      <ChatHeader data={data} conn={conn} />

      <div className="turn user-turn">
        <div className="speaker">Ops lead</div>
        <p>What should I approve right now?</p>
      </div>

      <div className="turn answer-turn">
        <div className="speaker">ShelfWise</div>
        <div className="answer-copy">
          <h2>{answer.heading}</h2>
          <p>{answer.body}</p>
          <p className="proof-line">
            <StatusGlyph tone="ok" label={buildProofLine(data.evidence)} />
          </p>
        </div>
      </div>

      <ActionBlock
        data={data}
        busy={busy}
        error={transitionError}
        reasoningOpen={reasoningOpen}
        onApprove={onApprove}
        onReject={onReject}
        onToggleReasoning={() => setReasoningOpen((value) => !value)}
      />

      {reasoningOpen ? <ReasoningChain evidence={data.evidence} /> : null}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Side rail
// ---------------------------------------------------------------------------
function SideRail({ data }: { data: GoldenDemo }) {
  return (
    <aside className="side-rail panel" aria-label="Approval and system summary">
      <ApprovalSummary data={data} />
      <PriorityList priorities={buildPriorities(data)} />
      <StoreIntelligencePanel intelligence={data.store_intelligence} />
      <TraceSummaryPanel trace={data.trace} inference={data.inference} />
      <LearningNote learning={data.learning} />
    </aside>
  )
}

function ApprovalSummary({ data }: { data: GoldenDemo }) {
  const status = data.decision?.status ?? 'pending'
  const outcome = data.decision?.outcome
  const writeBack = data.decision?.write_back
  return (
    <section className="rail-section">
      <div className="rail-heading">
        <div>
          <div className="section-kicker">Pending approval</div>
          <h2>{data.decision?.id ?? 'No decision id'}</h2>
        </div>
        <StatusGlyph tone={statusTone(status)} label={formatLabel(status)} />
      </div>
      <p>{data.decision?.summary ?? 'No approval summary returned.'}</p>
      {outcome ? (
        <dl className="trace-kv">
          <div>
            <dt>Units cleared</dt>
            <dd className="tnum">{formatValue(outcome.units_cleared)}</dd>
          </div>
          <div>
            <dt>Recovered</dt>
            <dd className="tnum">{formatValue(outcome.rand_recovered)}</dd>
          </div>
          <div>
            <dt>Score</dt>
            <dd className="tnum">{formatValue(outcome.success_score)}</dd>
          </div>
        </dl>
      ) : null}
      {writeBack?.status ? <p>Write-back: {formatLabel(writeBack.status)}</p> : null}
    </section>
  )
}

function PriorityList({ priorities }: { priorities: Priority[] }) {
  return (
    <section className="rail-section">
      <div className="rail-heading">
        <div>
          <div className="section-kicker">Current priorities</div>
          <h2>Operator queue</h2>
        </div>
        <span className="section-count tnum">{priorities.length}</span>
      </div>
      <dl className="priority-list">
        {priorities.map((priority) => (
          <div key={priority.label} className="priority-row">
            <dt>
              <span className={`glyph-shape tone-${priority.tone}`} aria-hidden>
                {GLYPH[priority.tone]}
              </span>
              {priority.label}
            </dt>
            <dd>{priority.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function StoreIntelligencePanel({ intelligence }: { intelligence?: StoreIntelligence }) {
  if (!intelligence) return null

  const rows: Priority[] = [
    {
      label: 'Old batch to sell',
      value: formatValue(intelligence.batch_split?.priority_sell_units),
      tone: 'warn',
    },
    {
      label: 'Normal units',
      value: formatValue(intelligence.batch_split?.normal_units),
      tone: 'ok',
    },
    {
      label: 'Delivery missing',
      value: formatValue(intelligence.delivery_reconciliation?.missing_units),
      tone: 'risk',
    },
    {
      label: 'Supplier action',
      value: formatLabel(intelligence.supplier_cover?.recommended_action ?? 'hold'),
      tone: intelligence.supplier_cover?.recommended_action === 'transfer' ? 'accent' : 'ok',
    },
  ]

  return (
    <section className="rail-section">
      <div className="rail-heading">
        <div>
          <div className="section-kicker">Store intelligence</div>
          <h2>Numeric proof</h2>
        </div>
        <span className="section-count">FEFO</span>
      </div>
      <p>{intelligence.batch_split?.conclusion ?? 'Batch-level stock proof is available.'}</p>
      <dl className="priority-list">
        {rows.map((row) => (
          <div key={row.label} className="priority-row">
            <dt>
              <span className={`glyph-shape tone-${row.tone}`} aria-hidden>
                {GLYPH[row.tone]}
              </span>
              {row.label}
            </dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
      {intelligence.learning_summary?.lesson ? <p>{intelligence.learning_summary.lesson}</p> : null}
    </section>
  )
}

function TraceSummaryPanel({
  trace,
  inference,
}: {
  trace?: TraceSpan[]
  inference?: InferenceConfig
}) {
  const summary = traceSummary(trace)
  const provider = inference?.provider ?? 'offline'
  const routingCount =
    (inference?.routing?.routine_agents?.length ?? 0) + (inference?.routing?.strong_agents?.length ?? 0)

  return (
    <section className="rail-section">
      <div className="rail-heading">
        <div>
          <div className="section-kicker">System thinking</div>
          <h2>Trace summary</h2>
        </div>
        <span className="section-count tnum">{summary.totalMs}ms</span>
      </div>

      <dl className="trace-kv">
        <div>
          <dt>Provider</dt>
          <dd>{formatLabel(provider)}</dd>
        </div>
        <div>
          <dt>Routed agents</dt>
          <dd className="tnum">{routingCount}</dd>
        </div>
        <div>
          <dt>Failed spans</dt>
          <dd className="tnum">{summary.failed}</dd>
        </div>
        <div>
          <dt>Slowest</dt>
          <dd>{summary.slowest ? `${summary.slowest.name ?? 'span'} ${summary.slowest.ms ?? 0}ms` : '-'}</dd>
        </div>
      </dl>

      <ol className="trace-list">
        {summary.spans.slice(0, 5).map((span, index) => {
          const tone = statusTone(span.status)
          return (
            <li key={`${span.name ?? 'span'}-${index}`}>
              <span className={`glyph-shape tone-${tone}`} aria-hidden>
                {GLYPH[tone]}
              </span>
              <span>{span.name ?? 'span'}</span>
              <span className="tnum">{span.ms ?? 0}ms</span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function LearningNote({ learning }: { learning?: GoldenDemo['learning'] }) {
  if (!learning?.status && !learning?.message) return null
  return (
    <section className="rail-section">
      <div className="rail-heading">
        <div>
          <div className="section-kicker">Learning</div>
          <h2>{formatLabel(learning.status ?? 'ready')}</h2>
        </div>
      </div>
      <p>{learning.message}</p>
    </section>
  )
}

function LoadingShell({ endpoint }: { endpoint: string }) {
  return (
    <section className="panel loading-panel" aria-live="polite">
      <div className="chat-header">
        <div>
          <div className="section-kicker">Loading</div>
          <h1>Fetching demo cascade</h1>
        </div>
        <span className="chat-meta">{endpoint}</span>
      </div>
      {[0, 1, 2, 3, 4].map((index) => (
        <div className="skeleton-row" key={index} style={{ width: `${82 - index * 9}%` }} />
      ))}
    </section>
  )
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    applyTheme(next)
    setTheme(next)
  }
  return (
    <button className="btn btn-secondary theme-toggle" type="button" aria-label="toggle theme" onClick={toggle}>
      {theme === 'dark' ? 'Light' : 'Dark'}
    </button>
  )
}

function App() {
  const [data, setData] = useState<GoldenDemo | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [scenarioMode, setScenarioMode] = useState<ScenarioMode>('approval')
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [transitionError, setTransitionError] = useState<string | null>(null)
  const transitionCtrl = useRef<AbortController | null>(null)
  const activePath = DEMO_PATHS[scenarioMode]

  useEffect(() => {
    const controller = new AbortController()
    setLoadState('loading')
    setError(null)
    fetchDemo(activePath, controller.signal)
      .then((payload) => {
        setData(payload)
        setBusy(null)
        setTransitionError(null)
        setLoadState('ready')
      })
      .catch((e) => {
        if (controller.signal.aborted) return
        setError(e instanceof Error ? e.message : String(e))
        setLoadState('error')
      })
    return () => controller.abort()
  }, [reloadKey, activePath])

  useEffect(() => () => transitionCtrl.current?.abort(), [])

  const endpoint = useMemo(() => joinUrl(configuredBase() || DEFAULT_API_BASE, activePath), [activePath])
  const conn = loadState === 'ready' ? 'live' : loadState === 'error' ? 'error' : 'loading'

  const selectScenario = (next: ScenarioMode) => {
    if (next !== scenarioMode) setData(null)
    transitionCtrl.current?.abort()
    setScenarioMode(next)
    setBusy(null)
    setTransitionError(null)
  }

  const transition = (kind: 'approve' | 'reject') => {
    const id = data?.decision?.id
    if (!id || busy) {
      if (!id) setTransitionError('No decision id is available.')
      return
    }
    transitionCtrl.current?.abort()
    const controller = new AbortController()
    transitionCtrl.current = controller
    setBusy(kind)
    setTransitionError(null)
    postTransition(id, kind, controller.signal)
      .then((result) => {
        setData((cur) =>
          cur
            ? {
                ...cur,
                decision: result.decision,
                learning: result.learning_event
                  ? {
                      status: 'threshold_adjusted',
                      message: result.learning_event.message,
                    }
                  : cur.learning,
              }
            : cur,
        )
        setBusy(null)
      })
      .catch((e) => {
        if (controller.signal.aborted) return
        setBusy(null)
        setTransitionError(
          `${kind} failed: ${e instanceof Error ? e.message : String(e)}. Nothing was changed; try again.`,
        )
      })
  }

  return (
    <div className="app-shell">
      <header className="header">
        <span className="brand">
          <span className="brand-mark" />
          <span className="brand-name">ShelfWise</span>
        </span>
        {data?.scenario ? <span className="scenario">{data.scenario.replace(/_/g, ' ')}</span> : null}
        <div className="header-right">
          <div className="scenario-switch" aria-label="Demo scenario">
            <button
              className={`btn btn-secondary ${scenarioMode === 'approval' ? 'is-active' : ''}`}
              type="button"
              aria-pressed={scenarioMode === 'approval'}
              onClick={() => selectScenario('approval')}
            >
              Approval case
            </button>
            <button
              className={`btn btn-secondary ${scenarioMode === 'critic' ? 'is-active' : ''}`}
              type="button"
              aria-pressed={scenarioMode === 'critic'}
              onClick={() => selectScenario('critic')}
            >
              Critic rejection
            </button>
          </div>
          <button className="btn btn-secondary" type="button" onClick={() => setReloadKey((value) => value + 1)}>
            Reload
          </button>
          <ThemeToggle />
        </div>
      </header>

      <main className="content">
        {error ? <ErrorState message={error} onRetry={() => setReloadKey((value) => value + 1)} /> : null}
        {loadState === 'loading' && !data ? <LoadingShell endpoint={endpoint} /> : null}

        {data ? (
          <ErrorBoundary>
            <div className="intel-layout">
              <Conversation
                data={data}
                conn={conn}
                busy={busy}
                transitionError={transitionError}
                onApprove={() => transition('approve')}
                onReject={() => transition('reject')}
              />
              <SideRail data={data} />
            </div>
          </ErrorBoundary>
        ) : null}
      </main>
    </div>
  )
}

export default App

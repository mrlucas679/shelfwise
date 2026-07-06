import { useEffect, useMemo, useState } from 'react'

type RecommendedAction = {
  type?: string
  params?: Record<string, unknown>
  risk_tier?: string
}

type SourceRef = {
  kind?: string
  ref?: string
  locator?: string | null
}

type SupportingFact = {
  fact?: string
  value?: unknown
  source?: string
  method?: string
}

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
  review?: Record<string, unknown>
}

type TraceSpan = {
  name?: string
  status?: string
  ms?: number
  detail?: Record<string, unknown>
}

type InferenceRouting = {
  routine_agents?: string[]
  strong_agents?: string[]
}

type InferenceConfig = {
  provider?: string
  base_url_configured?: boolean
  routine_model?: string
  strong_model?: string
  api_key_present?: boolean
  routing?: InferenceRouting
}

type GoldenDemo = {
  correlation_id?: string
  scenario?: string
  evidence?: EvidenceObject[]
  decision?: Decision
  trace?: TraceSpan[]
  inference?: InferenceConfig
  learning?: {
    status?: string
    message?: string
  }
}

type LoadState = 'idle' | 'loading' | 'ready' | 'error'

const DEFAULT_API_BASE_URL = 'http://localhost:8000'
const DEMO_PATH = '/demo/golden'

function configuredApiBaseUrl() {
  return (import.meta.env.VITE_API_BASE_URL ?? '').trim()
}

function joinUrl(baseUrl: string, path: string) {
  if (!baseUrl) {
    return path
  }

  return `${baseUrl.replace(/\/+$/, '')}${path}`
}

function goldenRequestUrls() {
  const configured = configuredApiBaseUrl()
  const primaryBaseUrl = configured || DEFAULT_API_BASE_URL
  const urls = [joinUrl(primaryBaseUrl, DEMO_PATH)]

  if (!configured && import.meta.env.DEV) {
    urls.push(DEMO_PATH)
  }

  return Array.from(new Set(urls))
}

async function fetchGoldenDemo(signal: AbortSignal): Promise<GoldenDemo> {
  const urls = goldenRequestUrls()
  let lastError = 'Unknown error'

  for (const url of urls) {
    try {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' },
        signal,
      })

      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText || 'HTTP error'}`.trim())
      }

      return (await response.json()) as GoldenDemo
    } catch (error) {
      if (signal.aborted) {
        throw error
      }

      lastError = error instanceof Error ? error.message : String(error)
    }
  }

  throw new Error(`Could not load ${DEMO_PATH}. Tried ${urls.join(' and ')}. ${lastError}`)
}

async function postDecisionTransition(
  decisionId: string,
  transition: 'approve' | 'reject',
  signal: AbortSignal,
): Promise<Decision> {
  const urls = goldenRequestUrls().map((url) =>
    url.replace(DEMO_PATH, `/decisions/${encodeURIComponent(decisionId)}/${transition}`),
  )
  let lastError = 'Unknown error'

  for (const url of urls) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        signal,
      })

      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText || 'HTTP error'}`.trim())
      }

      const payload = (await response.json()) as { decision?: Decision }
      if (!payload.decision) {
        throw new Error('Transition response did not include a decision.')
      }

      return payload.decision
    } catch (error) {
      if (signal.aborted) {
        throw error
      }

      lastError = error instanceof Error ? error.message : String(error)
    }
  }

  throw new Error(`Could not ${transition} decision. Tried ${urls.join(' and ')}. ${lastError}`)
}

function formatLabel(value: unknown) {
  const text = String(value ?? 'unknown').replace(/[_-]+/g, ' ')
  return text.replace(/\b\w/g, (letter) => letter.toUpperCase())
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return 'n/a'
  }

  if (typeof value === 'boolean') {
    return value ? 'yes' : 'no'
  }

  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : 'n/a'
  }

  if (typeof value === 'string') {
    return value
  }

  if (Array.isArray(value)) {
    return value.map(formatValue).join(', ')
  }

  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function formatConfidence(value: string | number | undefined) {
  const numeric = Number(value)

  if (!Number.isFinite(numeric)) {
    return formatValue(value)
  }

  return `${Math.round(numeric * 100)}%`
}

function statusClass(value: unknown) {
  return String(value ?? 'unknown')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
}

function formatSource(source: SourceRef) {
  const kind = source.kind ? `${source.kind}:` : ''
  const locator = source.locator ? `#${source.locator}` : ''
  return `${kind}${source.ref ?? 'unknown'}${locator}`
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError'
}

function StatusPill({ value, tone }: { value: unknown; tone?: string }) {
  return (
    <span className={`pill pill-${statusClass(tone ?? value)}`}>
      {formatLabel(value)}
    </span>
  )
}

function KeyValueList({ items, compact = false }: { items: Array<[string, unknown]>; compact?: boolean }) {
  const visibleItems = items.filter(([, value]) => value !== undefined && value !== null && value !== '')

  if (visibleItems.length === 0) {
    return null
  }

  return (
    <dl className={compact ? 'kv-list kv-list-compact' : 'kv-list'}>
      {visibleItems.map(([label, value]) => (
        <div className="kv-row" key={label}>
          <dt>{label}</dt>
          <dd>{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function ActionSummary({ action }: { action?: RecommendedAction }) {
  if (!action) {
    return <span className="muted">No action</span>
  }

  const params = Object.entries(action.params ?? {})

  return (
    <div className="action-summary">
      <div className="action-heading">
        <strong>{formatLabel(action.type ?? 'action')}</strong>
        <StatusPill value={action.risk_tier ?? 'unknown'} tone={action.risk_tier} />
      </div>
      <KeyValueList items={params} compact />
    </div>
  )
}

function DecisionPanel({
  decision,
  transitionStatus,
  transitionError,
  onApprove,
  onReject,
}: {
  decision?: Decision
  transitionStatus: LoadState
  transitionError: string | null
  onApprove: () => void
  onReject: () => void
}) {
  const effectiveStatus = decision?.status ?? 'pending'
  const buttonsDisabled = !decision || transitionStatus === 'loading' || effectiveStatus !== 'pending'

  return (
    <section className="panel decision-panel" aria-labelledby="decision-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Pending Decision</p>
          <h2 id="decision-heading">{decision?.summary ?? 'No decision loaded'}</h2>
        </div>
        <StatusPill value={effectiveStatus} tone={effectiveStatus} />
      </div>

      <ActionSummary action={decision?.action} />

      <KeyValueList
        items={[
          ['Decision ID', decision?.id],
          ['Caused by', decision?.caused_by],
        ]}
      />

      <div className="button-row">
        <button className="button button-approve" disabled={buttonsDisabled} onClick={onApprove} type="button">
          {transitionStatus === 'loading' ? 'Working' : 'Approve'}
        </button>
        <button className="button button-reject" disabled={buttonsDisabled} onClick={onReject} type="button">
          Reject
        </button>
      </div>

      {transitionError ? <p className="local-note local-note-error">{transitionError}</p> : null}
      {decision?.review ? <p className="local-note">Review recorded by {formatValue(decision.review.reviewer)}.</p> : null}
    </section>
  )
}

function EvidenceCard({ item, index }: { item: EvidenceObject; index: number }) {
  const facts = item.supporting_data ?? []
  const sources = item.sources ?? []

  return (
    <article className="evidence-card">
      <div className="evidence-card-heading">
        <div>
          <p className="eyebrow">Step {index + 1}</p>
          <h3>{formatLabel(item.agent ?? 'agent')}</h3>
        </div>
        <div className="evidence-badges">
          <StatusPill value={formatConfidence(item.confidence)} tone="confidence" />
          {item.requires_human_review ? <StatusPill value="HITL" tone="review" /> : null}
        </div>
      </div>

      <p className="conclusion">{item.conclusion ?? 'No conclusion supplied.'}</p>

      <div className="evidence-detail">
        <h4>Supporting Data</h4>
        {facts.length > 0 ? (
          <div className="fact-list">
            {facts.map((fact, factIndex) => (
              <div className="fact-row" key={`${fact.fact ?? 'fact'}-${factIndex}`}>
                <strong>{formatLabel(fact.fact ?? 'fact')}</strong>
                <span>{formatValue(fact.value)}</span>
                <small>
                  {formatValue(fact.method)}
                  {fact.source ? ` from ${fact.source}` : ''}
                </small>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">No supporting data supplied.</p>
        )}
      </div>

      <div className="evidence-footer">
        <ActionSummary action={item.recommended_action} />
        <div>
          <h4>Sources</h4>
          {sources.length > 0 ? (
            <ul className="source-list">
              {sources.map((source, sourceIndex) => (
                <li key={`${source.ref ?? 'source'}-${sourceIndex}`}>{formatSource(source)}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">No sources supplied.</p>
          )}
        </div>
      </div>
    </article>
  )
}

function EvidenceSection({ evidence }: { evidence?: EvidenceObject[] }) {
  const items = evidence ?? []

  return (
    <section className="panel" aria-labelledby="evidence-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Cascade</p>
          <h2 id="evidence-heading">Evidence</h2>
        </div>
        <StatusPill value={`${items.length} agents`} tone="count" />
      </div>

      {items.length > 0 ? (
        <div className="evidence-grid">
          {items.map((item, index) => (
            <EvidenceCard item={item} index={index} key={`${item.agent ?? 'agent'}-${index}`} />
          ))}
        </div>
      ) : (
        <p className="muted">No evidence returned.</p>
      )}
    </section>
  )
}

function TraceSection({ trace }: { trace?: TraceSpan[] }) {
  const spans = trace ?? []

  return (
    <section className="panel" aria-labelledby="trace-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Trace</p>
          <h2 id="trace-heading">Decision Science Spans</h2>
        </div>
        <StatusPill value={`${spans.length} spans`} tone="count" />
      </div>

      {spans.length > 0 ? (
        <div className="trace-table" role="table" aria-label="Trace spans">
          <div className="trace-row trace-row-head" role="row">
            <span role="columnheader">Name</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">Time</span>
            <span role="columnheader">Detail</span>
          </div>
          {spans.map((span, index) => (
            <div className="trace-row" role="row" key={`${span.name ?? 'span'}-${index}`}>
              <span role="cell">{span.name ?? 'unnamed_span'}</span>
              <span role="cell">
                <StatusPill value={span.status ?? 'unknown'} tone={span.status} />
              </span>
              <span role="cell">{formatValue(span.ms)} ms</span>
              <span role="cell">{formatValue(span.detail ?? {})}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">No trace spans returned.</p>
      )}
    </section>
  )
}

function InferenceSection({ inference }: { inference?: InferenceConfig }) {
  const routing = inference?.routing ?? {}
  const routineAgents = routing.routine_agents ?? []
  const strongAgents = routing.strong_agents ?? []

  return (
    <section className="panel" aria-labelledby="inference-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Inference</p>
          <h2 id="inference-heading">Routing</h2>
        </div>
        <StatusPill value={inference?.provider ?? 'offline'} tone={inference?.provider ?? 'offline'} />
      </div>

      <KeyValueList
        items={[
          ['Routine model', inference?.routine_model],
          ['Strong model', inference?.strong_model],
          ['Base URL configured', inference?.base_url_configured],
          ['API key present', inference?.api_key_present],
        ]}
      />

      <div className="route-grid">
        <div>
          <h3>Routine Agents</h3>
          <AgentList agents={routineAgents} />
        </div>
        <div>
          <h3>Strong Agents</h3>
          <AgentList agents={strongAgents} />
        </div>
      </div>
    </section>
  )
}

function AgentList({ agents }: { agents: string[] }) {
  if (agents.length === 0) {
    return <p className="muted">No agents listed.</p>
  }

  return (
    <ul className="agent-list">
      {agents.map((agent) => (
        <li key={agent}>{formatLabel(agent)}</li>
      ))}
    </ul>
  )
}

function LearningPanel({ learning }: { learning?: GoldenDemo['learning'] }) {
  if (!learning?.status && !learning?.message) {
    return null
  }

  return (
    <section className="panel learning-panel" aria-labelledby="learning-heading">
      <div>
        <p className="eyebrow">Learning</p>
        <h2 id="learning-heading">{formatLabel(learning.status ?? 'ready')}</h2>
      </div>
      <p>{learning.message}</p>
    </section>
  )
}

function LoadingState() {
  return (
    <section className="panel state-panel">
      <p className="eyebrow">Loading</p>
      <h2>Fetching golden cascade</h2>
      <p className="muted">{joinUrl(configuredApiBaseUrl() || DEFAULT_API_BASE_URL, DEMO_PATH)}</p>
    </section>
  )
}

function App() {
  const [data, setData] = useState<GoldenDemo | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [transitionStatus, setTransitionStatus] = useState<LoadState>('idle')
  const [transitionError, setTransitionError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    setLoadState('loading')
    setError(null)

    fetchGoldenDemo(controller.signal)
      .then((payload) => {
        setData(payload)
        setTransitionStatus('idle')
        setTransitionError(null)
        setLoadState('ready')
      })
      .catch((fetchError) => {
        if (isAbortError(fetchError)) {
          return
        }

        setError(fetchError instanceof Error ? fetchError.message : String(fetchError))
        setLoadState('error')
      })

    return () => controller.abort()
  }, [reloadKey])

  const decisionStatus = data?.decision?.status ?? 'pending'
  const endpointLabel = useMemo(() => joinUrl(configuredApiBaseUrl() || DEFAULT_API_BASE_URL, DEMO_PATH), [])
  const isLoadingWithoutData = loadState === 'loading' && !data

  const transitionDecision = (transition: 'approve' | 'reject') => {
    if (!data?.decision?.id) {
      setTransitionError('No decision id is available.')
      return
    }

    const controller = new AbortController()
    setTransitionStatus('loading')
    setTransitionError(null)

    postDecisionTransition(data.decision.id, transition, controller.signal)
      .then((decision) => {
        setData((current) => (current ? { ...current, decision } : current))
        setTransitionStatus('ready')
      })
      .catch((transitionFailure) => {
        if (isAbortError(transitionFailure)) {
          return
        }

        setTransitionError(transitionFailure instanceof Error ? transitionFailure.message : String(transitionFailure))
        setTransitionStatus('error')
      })
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div>
          <p className="eyebrow">ShelfWise</p>
          <h1>Ops Console</h1>
          <p className="hero-copy">{data?.scenario ? formatLabel(data.scenario) : 'Golden cascade monitor'}</p>
        </div>
        <div className="hero-actions">
          <StatusPill value={decisionStatus} tone={decisionStatus} />
          <button className="button button-secondary" onClick={() => setReloadKey((value) => value + 1)} type="button">
            Retry
          </button>
        </div>
      </header>

      <section className="meta-strip" aria-label="Run metadata">
        <KeyValueList
          compact
          items={[
            ['Endpoint', endpointLabel],
            ['Correlation', data?.correlation_id],
            ['Load state', loadState],
          ]}
        />
      </section>

      {error ? (
        <section className="alert-panel" role="alert">
          <div>
            <p className="eyebrow">API Error</p>
            <h2>Golden cascade unavailable</h2>
            <p>{error}</p>
          </div>
          <button className="button button-secondary" onClick={() => setReloadKey((value) => value + 1)} type="button">
            Retry
          </button>
        </section>
      ) : null}

      {isLoadingWithoutData ? <LoadingState /> : null}

      {data ? (
        <div className="dashboard-grid">
          <DecisionPanel
            decision={data.decision}
            transitionStatus={transitionStatus}
            transitionError={transitionError}
            onApprove={() => transitionDecision('approve')}
            onReject={() => transitionDecision('reject')}
          />
          <InferenceSection inference={data.inference} />
          <EvidenceSection evidence={data.evidence} />
          <TraceSection trace={data.trace} />
          <LearningPanel learning={data.learning} />
        </div>
      ) : null}
    </main>
  )
}

export default App

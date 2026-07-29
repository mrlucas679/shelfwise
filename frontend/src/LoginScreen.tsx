import { type FormEvent, useEffect, useState } from 'react'

type AuthView = 'signin' | 'activate' | 'forgot' | 'reset' | 'bootstrap' | 'change'
type ApiRequest = <T>(
  path: string,
  init: RequestInit,
  signal: AbortSignal,
) => Promise<T>
type AuthForm = {
  email: string
  password: string
  confirmation: string
  currentPassword: string
  givenName: string
  surname: string
  position: string
  companyName: string
  bootstrapKey: string
}
type LoginScreenProps = {
  request: ApiRequest
  onLogin: (email: string, password: string) => void
  onAuthenticated: () => void
  error: string | null
  busy: boolean
  forcePasswordChange?: boolean
}
type RequestSpec = { path: string; init: RequestInit }

const EMPTY_FORM: AuthForm = {
  email: '',
  password: '',
  confirmation: '',
  currentPassword: '',
  givenName: '',
  surname: '',
  position: '',
  companyName: '',
  bootstrapKey: '',
}

export function LoginScreen(props: LoginScreenProps) {
  const tokens = lifecycleTokens()
  const [view, setView] = useState<AuthView>(
    initialView(Boolean(props.forcePasswordChange), tokens),
  )
  const [form, setForm] = useState<AuthForm>(EMPTY_FORM)
  const bootstrapRequired = useBootstrapRequired(props.request)
  const lifecycle = useLifecycleSubmit({
    request: props.request,
    view,
    form,
    tokens,
    onAuthenticated: props.onAuthenticated,
  })
  const submitLogin = (event: FormEvent) => {
    event.preventDefault()
    if (!form.email.trim() || !form.password || props.busy) return
    props.onLogin(form.email.trim(), form.password)
  }
  return (
    <div className="login-screen">
      <form
        className="login-card"
        onSubmit={view === 'signin' ? submitLogin : lifecycle.submit}
      >
        <div className="login-mark" aria-hidden><span className="avatar-mark" /></div>
        <h1>{authTitle(view)}</h1>
        <AuthIntro view={view} />
        <AuthFields
          view={view}
          form={form}
          disabled={props.busy || lifecycle.busy}
          onChange={(name, value) => setForm((current) => ({ ...current, [name]: value }))}
        />
        {props.error && view === 'signin'
          ? <p className="login-error">{props.error}</p>
          : null}
        {lifecycle.error ? <p className="login-error" role="alert">{lifecycle.error}</p> : null}
        {lifecycle.notice ? <p className="muted" role="status">{lifecycle.notice}</p> : null}
        <AuthActions
          view={view}
          busy={props.busy || lifecycle.busy}
          loginReady={Boolean(form.email.trim() && form.password)}
          bootstrapRequired={bootstrapRequired}
          onViewChange={setView}
        />
      </form>
    </div>
  )
}

function AuthFields({
  view,
  form,
  disabled,
  onChange,
}: {
  view: AuthView
  form: AuthForm
  disabled: boolean
  onChange: (name: keyof AuthForm, value: string) => void
}) {
  const identityRequired = view === 'activate' || view === 'bootstrap'
  return <>
    {view === 'bootstrap' ? <>
      <AuthField label="Company name" value={form.companyName} autoFocus onChange={(value) => onChange('companyName', value)} />
      <AuthField label="Platform setup key" type="password" value={form.bootstrapKey} onChange={(value) => onChange('bootstrapKey', value)} />
    </> : null}
    {identityRequired ? <>
      <AuthField label="First name" value={form.givenName} autoFocus={view === 'activate'} onChange={(value) => onChange('givenName', value)} />
      <AuthField label="Surname" value={form.surname} onChange={(value) => onChange('surname', value)} />
      <AuthField label="Work position" value={form.position} onChange={(value) => onChange('position', value)} />
    </> : null}
    {view !== 'reset' && view !== 'change'
      ? <AuthField label="Work email" type="email" value={form.email} disabled={disabled} autoFocus={view === 'signin' || view === 'forgot'} onChange={(value) => onChange('email', value)} />
      : null}
    {view === 'change'
      ? <AuthField label="Current password" type="password" value={form.currentPassword} autoFocus onChange={(value) => onChange('currentPassword', value)} />
      : null}
    {view !== 'forgot' ? <>
      <AuthField label={view === 'signin' ? 'Password' : 'New password'} type="password" value={form.password} disabled={disabled} minLength={view === 'signin' ? undefined : 12} onChange={(value) => onChange('password', value)} />
      {view !== 'signin' ? <AuthField label="Confirm new password" type="password" value={form.confirmation} minLength={12} onChange={(value) => onChange('confirmation', value)} /> : null}
    </> : null}
  </>
}

function AuthField({
  label,
  value,
  onChange,
  type = 'text',
  autoFocus = false,
  disabled = false,
  minLength,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  autoFocus?: boolean
  disabled?: boolean
  minLength?: number
}) {
  return <label className="login-field">
    <span>{label}</span>
    <input
      type={type}
      autoFocus={autoFocus}
      disabled={disabled}
      minLength={minLength}
      autoComplete={passwordAutocomplete(label)}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      required
    />
  </label>
}

function AuthActions({
  view,
  busy,
  loginReady,
  bootstrapRequired,
  onViewChange,
}: {
  view: AuthView
  busy: boolean
  loginReady: boolean
  bootstrapRequired: boolean
  onViewChange: (view: AuthView) => void
}) {
  return <>
    <button
      className="btn btn-primary"
      type="submit"
      disabled={busy || (view === 'signin' && !loginReady)}
    >
      {busy ? 'Working…' : authSubmitLabel(view)}
    </button>
    {view === 'signin'
      ? <button className="btn btn-secondary" type="button" onClick={() => onViewChange('forgot')}>Forgot password</button>
      : null}
    {view === 'signin' && bootstrapRequired
      ? <button className="btn btn-secondary" type="button" onClick={() => onViewChange('bootstrap')}>Set up first company owner</button>
      : null}
    {view === 'forgot'
      ? <button className="btn btn-secondary" type="button" onClick={() => onViewChange('signin')}>Back to sign in</button>
      : null}
  </>
}

function AuthIntro({ view }: { view: AuthView }) {
  if (view === 'signin') {
    return <>
      <p className="muted">Sign in with your company account to open this store&apos;s operations.</p>
      <p className="muted">Need access? Ask your store owner to invite you from People &amp; access.</p>
    </>
  }
  const copy = {
    activate: 'Confirm the work details from your invitation, then choose your private password.',
    bootstrap: 'Create the first company owner for this dedicated ShelfWise workspace.',
    change: 'Your owner created a temporary credential. Replace it before continuing.',
    forgot: 'Enter your work email. The response will not reveal whether an account exists.',
    reset: 'Choose a new password. This single-use link will invalidate earlier sessions.',
  }[view]
  return <p className="muted">{copy}</p>
}

function useBootstrapRequired(request: ApiRequest): boolean {
  const [required, setRequired] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    request<{ bootstrap_required?: boolean }>(
      '/auth/setup-status',
      { method: 'GET' },
      controller.signal,
    )
      .then((payload) => setRequired(Boolean(payload.bootstrap_required)))
      .catch(() => setRequired(false))
    return () => controller.abort()
  }, [request])
  return required
}

function useLifecycleSubmit({
  request,
  view,
  form,
  tokens,
  onAuthenticated,
}: {
  request: ApiRequest
  view: AuthView
  form: AuthForm
  tokens: { activation: string; reset: string }
  onAuthenticated: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (view !== 'forgot' && form.password !== form.confirmation) {
      setError('The password confirmation does not match.')
      return
    }
    setBusy(true); setError(null); setNotice(null)
    try {
      const spec = lifecycleRequest(view, form, tokens)
      await request<unknown>(spec.path, spec.init, new AbortController().signal)
      if (view === 'forgot') {
        setNotice('If that active work account exists, a reset link has been sent.')
        return
      }
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
      onAuthenticated()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError))
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, notice, submit }
}

function lifecycleRequest(
  view: AuthView,
  form: AuthForm,
  tokens: { activation: string; reset: string },
): RequestSpec {
  const json = (body: object, headers: Record<string, string> = {}): RequestInit => ({
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
  const passwords = {
    password: form.password,
    password_confirmation: form.confirmation,
  }
  if (view === 'activate') return {
    path: '/auth/activate',
    init: json({
      token: tokens.activation,
      email: form.email,
      given_name: form.givenName,
      surname: form.surname,
      position: form.position,
      ...passwords,
    }),
  }
  if (view === 'forgot') return {
    path: '/auth/password-reset/request',
    init: json({ email: form.email }),
  }
  if (view === 'reset') return {
    path: '/auth/password-reset/consume',
    init: json({ token: tokens.reset, ...passwords }),
  }
  if (view === 'bootstrap') return {
    path: '/platform/bootstrap',
    init: json({
      company_name: form.companyName,
      email: form.email,
      given_name: form.givenName,
      surname: form.surname,
      position: form.position,
      ...passwords,
    }, { 'x-bootstrap-key': form.bootstrapKey }),
  }
  return {
    path: '/auth/change-password',
    init: json({ current_password: form.currentPassword, ...passwords }),
  }
}

function lifecycleTokens() {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  return {
    activation: params.get('activate') ?? '',
    reset: params.get('reset-password') ?? '',
  }
}

function initialView(
  forcePasswordChange: boolean,
  tokens: { activation: string; reset: string },
): AuthView {
  if (forcePasswordChange) return 'change'
  if (tokens.activation) return 'activate'
  if (tokens.reset) return 'reset'
  return 'signin'
}

function authTitle(view: AuthView): string {
  return {
    signin: 'ShelfWise',
    activate: 'Activate your work account',
    forgot: 'Reset your password',
    reset: 'Choose a new password',
    bootstrap: 'Set up your company',
    change: 'Change your temporary password',
  }[view]
}

function authSubmitLabel(view: AuthView): string {
  return {
    signin: 'Sign in',
    activate: 'Activate account',
    forgot: 'Send reset link',
    reset: 'Save new password',
    bootstrap: 'Create company owner',
    change: 'Change password',
  }[view]
}

function passwordAutocomplete(label: string): string | undefined {
  if (label === 'Work email') return 'username'
  if (label === 'Current password' || label === 'Password') return 'current-password'
  if (label.includes('password')) return 'new-password'
  return undefined
}

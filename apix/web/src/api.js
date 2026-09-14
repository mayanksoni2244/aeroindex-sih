/**
 * The one place the browser talks to the API.
 *
 * ON THE API KEY
 * A key shipped in a browser bundle is not access control — anyone with
 * devtools can read it. It is here because the API requires a header and this
 * dashboard is a demonstration client, not because it protects anything. The
 * README says the same thing in the same words. A real deployment puts this
 * behind a server-side session and never lets the key reach the page.
 */

const BASE = import.meta.env.VITE_APIX_API_URL || ''
// Matches the API's own development default (see apix/.env.example) so a fresh
// checkout works with no configuration. Override with VITE_APIX_API_KEY.
const KEY = import.meta.env.VITE_APIX_API_KEY || 'dev-nso-rbi-key'

export class ApiError extends Error {
  constructor(message, { status, url } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.url = url
  }
}

async function request(path, params = {}) {
  const url = new URL(`${BASE}${path}`, window.location.origin)
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
  }

  let resp
  try {
    resp = await fetch(url, { headers: { 'X-API-Key': KEY } })
  } catch (cause) {
    // A failed fetch is a failed fetch. It must not fall through to cached or
    // placeholder numbers — the UI shows the error instead.
    throw new ApiError(
      'Could not reach the API. Is `apix serve` running?',
      { url: url.pathname },
    )
  }

  if (resp.status === 401) {
    throw new ApiError(
      'The API rejected this key. Set VITE_APIX_API_KEY to match APIX_API_KEY.',
      { status: 401, url: url.pathname },
    )
  }
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const body = await resp.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* a non-JSON error body is not worth a second failure */
    }
    throw new ApiError(detail, { status: resp.status, url: url.pathname })
  }
  return resp.json()
}

export const api = {
  health: () => request('/v1/health'),
  index: (params) => request('/v1/index', params),
  routes: (params) => request('/v1/routes', params),
  coverage: (params) => request('/v1/coverage', params),
  backtest: (params) => request('/v1/backtest', params),
  heatmap: (params) => request('/v1/heatmap', params),
  curve: (route, params) => request(`/v1/routes/${route}/curve`, params),
  referenceValidation: (params) => request('/v1/reference-validation', params),
}

/**
 * Download the CSV export.
 *
 * Fetched with the key in a header and handed to the browser as a blob, rather
 * than linked with an `<a href>`. The API accepts the key only in `X-API-Key`,
 * and a plain link cannot set a header — it would 401. Putting the key in the
 * query string instead would work but would write it into server logs and
 * browser history, which is a worse trade for a demo convenience.
 */
export async function downloadCsv({ series, measure, start, end, mode = 'live' }) {
  const url = new URL(`${BASE}/v1/index.csv`, window.location.origin)
  for (const [k, v] of Object.entries({ series, measure, start, end, mode })) {
    if (v) url.searchParams.set(k, v)
  }

  const resp = await fetch(url, { headers: { 'X-API-Key': KEY } })
  if (!resp.ok) {
    throw new ApiError(`CSV export failed (${resp.status})`, {
      status: resp.status,
      url: url.pathname,
    })
  }

  const blob = await resp.blob()
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  // Prefer the filename the API chose, so the download identifies its series.
  const disposition = resp.headers.get('content-disposition') || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  link.download = match ? match[1] : `apix-${series}-${measure}-${mode}.csv`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(objectUrl)
}

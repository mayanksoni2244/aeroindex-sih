/**
 * AeroIndex dashboard.
 *
 * Hard separation:
 *   1. Live Index (Headline): 100% real Akasa Air data only, 0% simulated data,
 *      4 non-stop routes with renormalised DGCA weights, and empirical Reference
 *      Point Validation against published Ixigo Dec 2024 averages.
 *   2. Methodology Demonstration Sandbox: 30-day simulated stress-test demonstrating
 *      festival surge calibration (1.15x-1.30x), IQR outlier trimming, and dual-series
 *      Headline vs Core dynamics.
 */
import React, { useCallback, useEffect, useState } from 'react'

import { ApiError, api, downloadCsv } from './api.js'
import BacktestPanel from './components/BacktestPanel.jsx'
import { ConfidenceDisclosure, ConfidencePanel } from './components/Confidence.jsx'
import CoveragePanel from './components/CoveragePanel.jsx'
import ElasticityCurve from './components/ElasticityCurve.jsx'
import Heatmap from './components/Heatmap.jsx'
import IndexChart, { ProvenanceLegend } from './components/IndexChart.jsx'
import {
  GlobalProvenanceBanner,
  ProvenanceBadge,
  ProvenanceBar,
} from './components/Provenance.jsx'
import ReferenceValidationPanel from './components/ReferenceValidationPanel.jsx'
import SurgeGap from './components/SurgeGap.jsx'

const SERIES = [
  {
    key: 'headline',
    label: 'Headline',
    audience: 'MoSPI / NSO',
    blurb:
      'Includes festival and demand surges. What a traveller actually pays, which is what a consumer price index must reflect.',
  },
  {
    key: 'core',
    label: 'Core',
    audience: 'RBI',
    blurb:
      'Excludes flagged surge observations. The underlying trend, without the calendar noise a policy read has to see through.',
  },
]

const MEASURES = [
  {
    key: 'total',
    label: 'Total fare',
    blurb: 'Tax-inclusive — comparable with CPI, which measures what is paid.',
  },
  {
    key: 'base',
    label: 'Base fare',
    blurb:
      'Excludes taxes, UDF and fees. Isolates carrier pricing from tax policy changes.',
  },
]

function Card({ title, subtitle, provenance, action, children }) {
  return (
    <section className="card">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="card-title">{title}</h2>
          {subtitle && (
            <p className="mt-1 max-w-2xl text-sm text-slate-600">{subtitle}</p>
          )}
        </div>
        <div className="flex items-center gap-3">
          {provenance && <ProvenanceBadge provenance={provenance} />}
          {action}
        </div>
      </div>
      {children}
    </section>
  )
}

function ErrorBox({ error }) {
  return (
    <div className="caveat border-red-500 bg-red-50 text-red-900">
      <div className="font-semibold">Could not load data</div>
      <p className="mt-1">{error.message}</p>
      {error.url && (
        <p className="mt-1 font-mono text-xs opacity-70">{error.url}</p>
      )}
    </div>
  )
}

export default function App() {
  const [mode, setMode] = useState('live') // 'live' | 'sandbox'
  const [series, setSeries] = useState('headline')
  const [measure, setMeasure] = useState('total')
  const [showCompare, setShowCompare] = useState(true)
  const [selectedRoute, setSelectedRoute] = useState('DEL-BOM')
  // Which day the reader clicked on the chart. Null means "the latest point",
  // so the panel below the chart has something to show before any interaction.
  const [selectedDate, setSelectedDate] = useState(null)

  const [health, setHealth] = useState(null)
  const [index, setIndex] = useState(null)
  const [counterpart, setCounterpart] = useState(null)
  const [coverage, setCoverage] = useState(null)
  const [heatmap, setHeatmap] = useState(null)
  const [curve, setCurve] = useState(null)
  const [backtest, setBacktest] = useState(null)
  const [referenceVal, setReferenceVal] = useState(null)
  const [routes, setRoutes] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const other = series === 'headline' ? 'core' : 'headline'
      if (mode === 'live') {
        const [h, idx, cmp, cov, hm, rts, rv] = await Promise.all([
          api.health(),
          api.index({ series, measure, mode: 'live' }),
          api.index({ series: other, measure, mode: 'live' }),
          api.coverage({ mode: 'live' }),
          api.heatmap({ measure, mode: 'live' }),
          api.routes({ mode: 'live' }),
          api.referenceValidation({ measure }),
        ])
        setHealth(h)
        setIndex(idx)
        setCounterpart(cmp)
        setCoverage(cov)
        setHeatmap(hm)
        setReferenceVal(rv)
        const inBasket = rts.routes.filter((r) => r.in_basket)
        setRoutes(inBasket)
        if (!inBasket.some((r) => r.route === selectedRoute)) {
          setSelectedRoute(inBasket[0]?.route || 'DEL-BOM')
        }
      } else {
        const [h, idx, cmp, cov, hm, bt, rts] = await Promise.all([
          api.health(),
          api.index({ series, measure, mode: 'sandbox' }),
          api.index({ series: other, measure, mode: 'sandbox' }),
          api.coverage({ mode: 'sandbox' }),
          api.heatmap({ measure, mode: 'sandbox' }),
          api.backtest({ series, measure }),
          api.routes({ mode: 'sandbox' }),
        ])
        setHealth(h)
        setIndex(idx)
        setCounterpart(cmp)
        setCoverage(cov)
        setHeatmap(hm)
        setBacktest(bt)
        const inBasket = rts.routes.filter((r) => r.in_basket)
        setRoutes(inBasket)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError(String(err)))
    } finally {
      setLoading(false)
    }
  }, [mode, series, measure])

  useEffect(() => {
    load()
  }, [load])

  // Switching series, measure or mode swaps the dataset underneath the
  // selection. Keeping a date from the previous dataset would leave the panel
  // describing a day the reader did not click, which is worse than resetting.
  useEffect(() => {
    setSelectedDate(null)
  }, [mode, series, measure])

  useEffect(() => {
    let cancelled = false
    if (selectedRoute) {
      api
        .curve(selectedRoute, { measure, mode })
        .then((c) => !cancelled && setCurve(c))
        .catch(() => !cancelled && setCurve(null))
    }
    return () => {
      cancelled = true
    }
  }, [selectedRoute, measure, mode])

  const latest = index?.points?.[index.points.length - 1]
  const first = index?.points?.[0]
  const change =
    latest && first && first.index_value
      ? ((latest.index_value / first.index_value - 1) * 100).toFixed(2)
      : null

  // The point the confidence panel describes. A date carried over from another
  // series or mode may not exist in this one, so it falls back to the latest
  // point rather than rendering an empty panel or silently keeping a stale one.
  const focused =
    (selectedDate && index?.points?.find((p) => p.index_date === selectedDate)) ||
    latest
  const focusedIsSelected = Boolean(
    selectedDate && index?.points?.some((p) => p.index_date === selectedDate),
  )

  const activeSeries = SERIES.find((s) => s.key === series)
  const activeMeasure = MEASURES.find((m) => m.key === measure)

  return (
    <div className="min-h-screen bg-slate-50">
      <GlobalProvenanceBanner health={health} coverage={coverage} mode={mode} />

      {/* ── Main Header ─────────────────────────────────────────────── */}
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto max-w-[1600px] px-4 py-6">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-bold tracking-tight text-slate-900">
                  AeroIndex <span className="font-mono text-slate-400">(APIx)</span>
                </h1>
                {mode === 'live' ? (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 px-3 py-0.5 text-xs font-semibold text-emerald-800 ring-1 ring-inset ring-emerald-600/20">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-600"></span>
                    100% Real Live Data
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-100 px-3 py-0.5 text-xs font-semibold text-amber-800 ring-1 ring-inset ring-amber-600/20">
                    <span className="h-1.5 w-1.5 rounded-full bg-amber-600"></span>
                    Methodology Sandbox
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-slate-600">
                A real-time airfare price index for India, built to augment the
                CPI transport basket. SIH26056 · MoSPI.
              </p>
            </div>
            <div className="text-right text-xs text-slate-500">
              <div>
                basket{' '}
                <span className="font-mono font-medium text-slate-700">
                  {mode === 'live' ? 'v1-akasa-live-4route' : 'v1-dgca-fy2022-23'}
                </span>
              </div>
              <div>
                live scraping{' '}
                <span className="font-mono text-emerald-700 font-medium">
                  Akasa Air (QP) production API
                </span>
              </div>
              <div>
                routes in view{' '}
                <span className="font-mono font-medium text-slate-700">
                  {mode === 'live' ? '4 non-stop routes' : '6 DGCA routes'}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* ── Mode Navigation Tabs ────────────────────────────────────────── */}
        <div className="mx-auto max-w-[1600px] px-4 pt-2">
          <nav className="flex space-x-2 border-t border-slate-100 pt-2">
            <button
              onClick={() => setMode('live')}
              className={`flex items-center gap-2.5 border-b-2 px-5 py-3 text-sm font-semibold transition-all ${
                mode === 'live'
                  ? 'border-emerald-600 text-emerald-800 bg-emerald-50/70 rounded-t-md'
                  : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800'
              }`}
            >
              <span className="h-2 w-2 rounded-full bg-emerald-500 ring-2 ring-emerald-200"></span>
              Live Index (Headline)
              <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-800">
                100% Real Akasa Data
              </span>
            </button>

            <button
              onClick={() => setMode('sandbox')}
              className={`flex items-center gap-2.5 border-b-2 px-5 py-3 text-sm font-semibold transition-all ${
                mode === 'sandbox'
                  ? 'border-amber-600 text-amber-900 bg-amber-50/70 rounded-t-md'
                  : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-800'
              }`}
            >
              <span className="h-2 w-2 rounded-full bg-amber-500 ring-2 ring-amber-200"></span>
              Methodology Demonstration Sandbox
              <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
                Synthetic Stress-Test
              </span>
            </button>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] space-y-6 px-4 py-6">
        {error && <ErrorBox error={error} />}

        {/* ── Sandbox Notice (Only shown in Sandbox Mode) ─────────────── */}
        {mode === 'sandbox' && (
          <div className="rounded-lg border-2 border-amber-400 bg-amber-50/90 p-5 text-amber-950 shadow-sm">
            <div className="flex items-center gap-2 text-base font-bold uppercase tracking-wide text-amber-900">
              <span className="flex h-2.5 w-2.5 rounded-full bg-amber-600"></span>
              Synthetic Stress-Test — Demonstrates Index Methodology, Not Live Data
            </div>
            <p className="mt-2 text-sm leading-relaxed text-amber-900">
              This sandbox demonstrates how the APIx index engine handles multi-month operations, seasonal calendar events, and outlier anomalies across all 6 DGCA basket routes before multi-month live history is accumulated:
            </p>
            <div className="mt-3 grid grid-cols-1 gap-3 text-xs sm:grid-cols-3">
              <div className="rounded-md border border-amber-200 bg-white/70 p-3 shadow-xs">
                <strong className="block font-semibold text-amber-950">1. Calibrated Festival Surges</strong>
                Multipliers calibrated to realistic 1.15x–1.30x ranges for the
                six windows in config/festivals.yaml (Holi, Eid al-Fitr, summer
                vacation, Dussehra, Diwali, Christmas/New Year), replacing
                unrealistic 2x assumptions.
              </div>
              <div className="rounded-md border border-amber-200 bg-white/70 p-3 shadow-xs">
                <strong className="block font-semibold text-amber-950">2. Headline vs Core Separation</strong>
                Demonstrates how Core APIx filters out temporary festival surge volatility for RBI monetary policy while Headline measures consumer expenditure for MoSPI CPI.
              </div>
              <div className="rounded-md border border-amber-200 bg-white/70 p-3 shadow-xs">
                <strong className="block font-semibold text-amber-950">3. Tukey Outlier Flags</strong>
                Statistical outliers are flagged, never deleted: they stay in
                Headline and are excluded from Core, so a data error and a
                genuine surge are both visible rather than silently trimmed.
              </div>
            </div>
          </div>
        )}

        {/* ── Series & Measure Controls ─────────────────────────────────── */}
        <section className="card">
          <div className="grid gap-6 md:grid-cols-2">
            <div>
              <div className="card-title mb-2">Series</div>
              <div className="flex gap-2">
                {SERIES.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => setSeries(s.key)}
                    className={`flex-1 rounded-md border px-3 py-2 text-left transition ${
                      series === s.key
                        ? 'border-slate-900 bg-slate-900 text-white shadow-xs'
                        : 'border-slate-300 bg-white hover:border-slate-400'
                    }`}
                  >
                    <div className="text-sm font-semibold">{s.label}</div>
                    <div
                      className={`text-xs ${series === s.key ? 'text-slate-300' : 'text-slate-500'}`}
                    >
                      {s.audience}
                    </div>
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-slate-600">{activeSeries.blurb}</p>
            </div>

            <div>
              <div className="card-title mb-2">Measure</div>
              <div className="flex gap-2">
                {MEASURES.map((m) => (
                  <button
                    key={m.key}
                    onClick={() => setMeasure(m.key)}
                    className={`flex-1 rounded-md border px-3 py-2 text-left transition ${
                      measure === m.key
                        ? 'border-slate-900 bg-slate-900 text-white shadow-xs'
                        : 'border-slate-300 bg-white hover:border-slate-400'
                    }`}
                  >
                    <div className="text-sm font-semibold">{m.label}</div>
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-slate-600">{activeMeasure.blurb}</p>
            </div>
          </div>
        </section>

        {/* ── The Index Chart Card ─────────────────────────────────────── */}
        <Card
          title={
            mode === 'live'
              ? `${activeSeries.label} APIx (Live Index) · ${activeMeasure.label}`
              : `${activeSeries.label} APIx (Methodology Sandbox) · ${activeMeasure.label}`
          }
          subtitle={
            mode === 'live'
              ? '100% Real Akasa Air observations. Base period 2026-09-13 = 100. Jevons within routes, Laspeyres across them using renormalised DGCA passenger weights (v1-akasa-live-4route).'
              : '30-day simulated stress-test demonstrating festival demand spikes, advance-purchase elasticity, and Headline vs Core separation across the 6 DGCA basket routes.'
          }
          provenance={index?.provenance}
          action={
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-slate-600">
                <input
                  type="checkbox"
                  checked={showCompare}
                  onChange={(e) => setShowCompare(e.target.checked)}
                  className="rounded border-slate-300 text-slate-900 focus:ring-slate-900"
                />
                show {series === 'headline' ? 'Core' : 'Headline'}
              </label>
              <button
                onClick={() => downloadCsv({ series, measure, mode }).catch(setError)}
                className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium hover:bg-slate-50 transition"
              >
                CSV Export
              </button>
            </div>
          }
        >
          {loading && !index ? (
            <p className="py-12 text-center text-sm text-slate-500">Loading…</p>
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-end gap-8">
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-500">
                    Latest
                  </div>
                  <div className="stat text-3xl">
                    {latest ? latest.index_value.toFixed(2) : '—'}
                  </div>
                  <div className="text-xs text-slate-500">
                    {latest?.index_date || 'no data'}
                  </div>
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-500">
                    Change over window
                  </div>
                  <div
                    className={`stat text-3xl ${
                      change > 0
                        ? 'text-red-600'
                        : change < 0
                          ? 'text-blue-600'
                          : ''
                    }`}
                  >
                    {change === null ? '—' : `${change > 0 ? '+' : ''}${change}%`}
                  </div>
                  <div className="text-xs text-slate-500">
                    {index?.count ?? 0} points
                  </div>
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wide text-slate-500">
                    Window confidence
                  </div>
                  <div className="mt-1">
                    <ConfidenceDisclosure
                      confidence={index?.confidence}
                      title="Confidence in this window"
                      scope={`weakest single day: ${index?.weakest_grade || '—'}`}
                    />
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    weakest day{' '}
                    <span className="font-mono font-medium text-slate-700">
                      {index?.weakest_grade || '—'}
                    </span>
                  </div>
                </div>
              </div>

              <IndexChart
                points={index?.points || []}
                compare={showCompare ? counterpart?.points : null}
                onSelectDate={setSelectedDate}
                selectedDate={focusedIsSelected ? selectedDate : null}
              />
              <div className="mt-3 space-y-3">
                <ProvenanceLegend
                  hasCompare={showCompare && counterpart?.points?.length > 0}
                  hasGrades={(index?.points || []).some((p) => p.confidence)}
                  compareLabel={`${series === 'headline' ? 'Core' : 'Headline'} APIx, for comparison`}
                />
                <ProvenanceBar provenance={index?.provenance} />
              </div>

              {/* ── Per-day confidence ────────────────────────────────────
                  Clicking a dot on the chart focuses it here. The caveat is
                  the reason this panel is a first-class card rather than a
                  tooltip: it has to survive a screenshot of the page, and a
                  hover-only explanation does not. */}
              {focused?.confidence && (
                <div className="mt-4">
                  <ConfidencePanel
                    confidence={focused.confidence}
                    title={
                      focusedIsSelected
                        ? `Confidence for ${focused.index_date}`
                        : 'Confidence for the latest day'
                    }
                    scope={`index ${focused.index_value.toFixed(2)}`}
                  />
                  <p className="mt-2 text-xs text-slate-500">
                    {focusedIsSelected ? (
                      <>
                        Showing the day you selected.{' '}
                        <button
                          type="button"
                          onClick={() => setSelectedDate(null)}
                          className="font-medium text-slate-700 underline decoration-dotted underline-offset-2 hover:text-slate-900"
                        >
                          Back to the latest day
                        </button>
                      </>
                    ) : (
                      'Click any dot on the chart to inspect that day.'
                    )}
                  </p>
                </div>
              )}
            </>
          )}
        </Card>

        {/* ── Surge Gap: Headline − Core ─────────────────────────────────
            The project's stated differentiator, plotted. Both series are
            already loaded above, so this card makes no extra request and
            cannot touch the Live/Sandbox separation. */}
        <Card
          title="Headline − Core Gap — The Surge Measure"
          subtitle={
            mode === 'live'
              ? 'The part of the all-in index that Core drops. The live window is currently a single day, and that day is its own base period — both series are therefore pinned to 100.0 and the gap is zero by construction, not because the surge measure found nothing. It becomes informative once a second day lands.'
              : 'The dual-audience claim, drawn instead of asserted: Headline keeps every observation, Core drops the festival and statistically-flagged ones, and the difference between them is a direct read on demand-driven surge.'
          }
        >
          <SurgeGap
            points={index?.points || []}
            counterpart={counterpart?.points || []}
            series={series}
            measure={measure}
          />
        </Card>

        {/* ── Mode-Specific Validation Card ─────────────────────────────── */}
        {mode === 'live' ? (
          <Card
            title="Reference Point Validation — External Market Benchmark Comparison"
            subtitle="Comparing live observed Akasa fares against independent published market averages (Ixigo Dec 2024 via The Indian Express). Replaces circular synthetic backtesting with empirical industry validation."
          >
            <ReferenceValidationPanel validation={referenceVal} />
          </Card>
        ) : (
          <Card
            title="Methodology Backtest — Long-Horizon DGCA Trend Comparison"
            subtitle="Demonstrating historical alignment methodology against DGCA monthly averages. Clearly flagged as illustrative demonstration data only."
          >
            <BacktestPanel backtest={backtest} />
          </Card>
        )}

        {/* ── Coverage Panel ───────────────────────────────────────────── */}
        <Card
          title={
            mode === 'live'
              ? 'Collection Coverage — 100% Real Live Data Audit'
              : 'Collection Coverage — Simulated Methodology Runs'
          }
          subtitle={
            mode === 'live'
              ? 'Live scraping status from Akasa Air (QP) production availability endpoint. Every cell is verified real.'
              : 'What the collector managed across simulated historical cycles, tracking data availability and imputation flags.'
          }
        >
          <CoveragePanel
            coverage={coverage}
            liveSource={health?.live_source || 'Akasa Air (QP)'}
          />
        </Card>

        {/* ── Route × Advance Heatmap ──────────────────────────────────── */}
        <Card
          title="Route × Advance-Purchase Heatmap — Price Movement Per Cell"
          subtitle="Each square is one route bought a given number of days ahead, coloured by how far its median fare has moved since the base period. Every square is tagged with its provenance."
          provenance={heatmap?.provenance}
        >
          <Heatmap heatmap={heatmap} />
        </Card>

        {/* ── Elasticity Curve ─────────────────────────────────────────── */}
        <Card
          title="Advance-Purchase Elasticity — Cost of Booking Late"
          subtitle="How much the same seat costs by how far ahead it is bought. This is the reading the current CPI cannot produce at all."
          provenance={curve?.provenance}
          action={
            <select
              value={selectedRoute}
              onChange={(e) => setSelectedRoute(e.target.value)}
              className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800"
            >
              {routes.map((r) => (
                <option key={r.route} value={r.route}>
                  {r.route} — {r.name}
                </option>
              ))}
            </select>
          }
        >
          <ElasticityCurve curve={curve} />
        </Card>

        <footer className="pb-8 pt-4 text-xs leading-relaxed text-slate-500 border-t border-slate-200">
          <p>
            Prototype for Smart India Hackathon 2026, problem statement SIH26056.
            Tier-A collection runs only against sources whose robots.txt permits
            it; a block stops that source and is recorded, never worked around.
          </p>
          <p className="mt-1">
            Every number on this page carries its provenance. If a figure is
            quoted without one, it has been taken out of context.
          </p>
        </footer>
      </main>
    </div>
  )
}

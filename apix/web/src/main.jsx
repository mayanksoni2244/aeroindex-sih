/**
 * Entry point.
 *
 * StrictMode is on deliberately. It double-invokes effects in development,
 * which surfaces fetch effects that are not safe to run twice — exactly the
 * class of bug that would otherwise show up as a doubled quote count in a demo.
 */
import React from 'react'
import { createRoot } from 'react-dom/client'

import App from './App.jsx'
import './index.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

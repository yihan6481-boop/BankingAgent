import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import R2App from './R2App'
import './index.css'
import './mobile.css'
import './mobile-details.css'
import './next-mobile.css'
import './visual-refresh.css'
import { SeatWorkspace } from './components/SeatWorkspace'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {new URLSearchParams(window.location.search).get('workspace')==='seat'?<SeatWorkspace/>:new URLSearchParams(window.location.search).get('engine') === 'r2-standalone' ? <R2App /> : <App />}
  </React.StrictMode>,
)

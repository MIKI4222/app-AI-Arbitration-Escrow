import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CONTRACT_ADDRESS,
  connectWallet,
  explorerTxUrl,
  fetchAllDeals,
  fetchTotalDeals,
  hasWallet,
  makeClient,
  makeReadClient,
  shortenAddress,
  writeAndWait,
} from './genlayerClient'
import './App.css'

function App() {
  const [account, setAccount] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [status, setStatus] = useState({ kind: 'info', text: 'Not connected to a wallet.' })

  const [deals, setDeals] = useState([])
  const [totalDeals, setTotalDeals] = useState(0)
  const [loadingDeals, setLoadingDeals] = useState(true)

  const [pendingAction, setPendingAction] = useState(null)
  const [lastTx, setLastTx] = useState(null)
  const clientRef = useRef(null)

  const updateStatus = useCallback((kind, text) => setStatus({ kind, text }), [])

  const refreshDeals = useCallback(async (silent = false) => {
    if (!silent) setLoadingDeals(true)
    try {
      const readClient = makeReadClient()
      const [list, total] = await Promise.all([
        fetchAllDeals(readClient),
        fetchTotalDeals(readClient),
      ])
      setDeals(list)
      setTotalDeals(total)
      if (!silent) updateStatus('success', `Loaded ${list.length} deal${list.length === 1 ? '' : 's'}.`)
    } catch (e) {
      updateStatus('error', `Failed to load deals: ${humanError(e)}`)
    } finally {
      setLoadingDeals(false)
    }
  }, [updateStatus])

  useEffect(() => {
    refreshDeals(true)
  }, [refreshDeals])

  const handleConnect = useCallback(async () => {
    setConnecting(true)
    try {
      const addr = await connectWallet()
      setAccount(addr)
      clientRef.current = makeClient(addr)
      updateStatus('success', `Connected as ${shortenAddress(addr)}.`)
    } catch (e) {
      updateStatus('error', humanError(e))
    } finally {
      setConnecting(false)
    }
  }, [updateStatus])

  const runWrite = useCallback(
    async (key, label, fn) => {
      if (!clientRef.current) {
        updateStatus('error', 'Connect a wallet first.')
        return
      }
      setPendingAction(key)
      updateStatus('pending', `${label} — waiting for wallet and blockchain consensus...`)
      setLastTx(null)
      try {
        const hash = await fn(clientRef.current)
        setLastTx(hash)
        updateStatus('success', `${label} succeeded.`)
        await refreshDeals(true)
      } catch (e) {
        updateStatus('error', `${label} failed: ${humanError(e)}`)
      } finally {
        setPendingAction(null)
      }
    },
    [refreshDeals, updateStatus],
  )

  return (
    <div className="app">
      <Header account={account} connecting={connecting} onConnect={handleConnect} />

      <StatusBar
        status={status}
        pendingAction={pendingAction}
        lastTx={lastTx}
      />

      <div className="grid-two section">
        <CreateDealCard disabled={!account} pending={pendingAction === 'create_deal'} onRun={runWrite} />
        <SubmitEvidenceCard disabled={!account} pending={pendingAction === 'submit_evidence'} onRun={runWrite} />
      </div>

      <DealsSection
        deals={deals}
        totalDeals={totalDeals}
        loading={loadingDeals}
        account={account}
        pendingAction={pendingAction}
        onRun={runWrite}
        onRefresh={() => refreshDeals(false)}
      />

      <Footer />
    </div>
  )
}

function Header({ account, connecting, onConnect }) {
  return (
    <header className="app-header">
      <div>
        <h1 className="app-header-title">AI Arbitration Escrow</h1>
        <p className="app-header-subtitle">
          Trust-minimized escrow for client–freelancer agreements. Disputes are
          resolved by AI validators reaching consensus on the GenLayer blockchain.
        </p>
      </div>
      <div className="header-right">
        {account ? (
          <span className="wallet-badge">
            <span className="dot" />
            {shortenAddress(account)}
          </span>
        ) : (
          <button className="btn" onClick={onConnect} disabled={connecting || !hasWallet()}>
            {connecting ? (
              <>
                <span className="spinner" /> Connecting...
              </>
            ) : !hasWallet() ? (
              'No wallet detected'
            ) : (
              'Connect Wallet'
            )}
          </button>
        )}
      </div>
    </header>
  )
}

function StatusBar({ status, pendingAction, lastTx }) {
  const kind = pendingAction ? 'pending' : status.kind
  return (
    <div className="status-bar">
      <div className="status-inner">
        <span className={`status-pill ${kind}`}>
          <span className="dot" />
          <span>{pendingAction ? status.text : status.text}</span>
        </span>
        {lastTx && (
          <a
            className="tx-link"
            href={explorerTxUrl(lastTx)}
            target="_blank"
            rel="noreferrer"
          >
            View transaction ↗
          </a>
        )}
      </div>
    </div>
  )
}

function CreateDealCard({ disabled, pending, onRun }) {
  const [freelancer, setFreelancer] = useState('')
  const [description, setDescription] = useState('')
  const [amount, setAmount] = useState('')

  const canSubmit =
    !disabled &&
    !pending &&
    freelancer.trim().length > 0 &&
    description.trim().length > 0 &&
    Number(amount) > 0

  const submit = (e) => {
    e.preventDefault()
    onRun('create_deal', 'Create deal', (client) =>
      writeAndWait(client, {
        functionName: 'create_deal',
        args: [freelancer.trim(), description.trim(), Number(amount)],
        value: 0n,
      }),
    )
    setFreelancer('')
    setDescription('')
    setAmount('')
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2 className="card-title">Create Deal</h2>
      </div>
      <form onSubmit={submit}>
        <div className="form-field">
          <label className="form-label">Freelancer address</label>
          <input
            className="form-input"
            placeholder="0x..."
            value={freelancer}
            onChange={(e) => setFreelancer(e.target.value)}
            disabled={disabled || pending}
          />
        </div>
        <div className="form-field">
          <label className="form-label">Work description</label>
          <input
            className="form-input"
            placeholder="e.g. Landing page redesign"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={disabled || pending}
          />
        </div>
        <div className="form-field">
          <label className="form-label">Amount (GEN)</label>
          <input
            className="form-input"
            type="number"
            min="1"
            placeholder="500"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            disabled={disabled || pending}
          />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn" disabled={!canSubmit}>
            {pending ? <><span className="spinner" /> Creating...</> : 'Create Deal'}
          </button>
          {disabled && <span className="form-hint">Connect a wallet to create a deal.</span>}
        </div>
      </form>
    </section>
  )
}

function SubmitEvidenceCard({ disabled, pending, onRun }) {
  const [dealId, setDealId] = useState('')
  const [role, setRole] = useState('client')
  const [evidenceUrl, setEvidenceUrl] = useState('')
  const [claim, setClaim] = useState('')

  const canSubmit =
    !disabled &&
    !pending &&
    dealId !== '' &&
    evidenceUrl.trim().length > 0 &&
    claim.trim().length > 0

  const submit = (e) => {
    e.preventDefault()
    const fnName = role === 'client' ? 'submit_client_evidence' : 'submit_freelancer_evidence'
    const label = role === 'client' ? 'Submit client evidence' : 'Submit freelancer evidence'
    onRun('submit_evidence', label, (client) =>
      writeAndWait(client, {
        functionName: fnName,
        args: [Number(dealId), evidenceUrl.trim(), claim.trim()],
        value: 0n,
      }),
    )
    setEvidenceUrl('')
    setClaim('')
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2 className="card-title">Submit Evidence</h2>
      </div>
      <form onSubmit={submit}>
        <div className="form-row">
          <div className="form-field">
            <label className="form-label">Deal ID</label>
            <input
              className="form-input"
              type="number"
              min="0"
              placeholder="0"
              value={dealId}
              onChange={(e) => setDealId(e.target.value)}
              disabled={disabled || pending}
            />
          </div>
          <div className="form-field">
            <label className="form-label">Your role</label>
            <div className="role-toggle">
              <button
                type="button"
                className={`role-toggle-btn ${role === 'client' ? 'active' : ''}`}
                onClick={() => setRole('client')}
                disabled={disabled || pending}
              >
                Client
              </button>
              <button
                type="button"
                className={`role-toggle-btn ${role === 'freelancer' ? 'active' : ''}`}
                onClick={() => setRole('freelancer')}
                disabled={disabled || pending}
              >
                Freelancer
              </button>
            </div>
          </div>
        </div>
        <div className="form-field">
          <label className="form-label">Evidence URL</label>
          <input
            className="form-input"
            type="url"
            placeholder="https://..."
            value={evidenceUrl}
            onChange={(e) => setEvidenceUrl(e.target.value)}
            disabled={disabled || pending}
          />
        </div>
        <div className="form-field">
          <label className="form-label">Claim — what does this URL prove?</label>
          <textarea
            className="form-textarea"
            placeholder="e.g. This Figma file shows the redesigned landing page delivered on Aug 20."
            value={claim}
            onChange={(e) => setClaim(e.target.value)}
            disabled={disabled || pending}
          />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn" disabled={!canSubmit}>
            {pending ? <><span className="spinner" /> Submitting...</> : 'Submit Evidence'}
          </button>
          {disabled && <span className="form-hint">Connect a wallet to submit evidence.</span>}
        </div>
      </form>
    </section>
  )
}

function DealsSection({ deals, totalDeals, loading, account, pendingAction, onRun, onRefresh }) {
  return (
    <section className="section">
      <div className="deals-toolbar">
        <div>
          <h2 className="section-title">Deals</h2>
          <p className="section-sub">
            {loading
              ? 'Loading deals from the contract...'
              : `${deals.length} shown · ${totalDeals} total on-chain`}
          </p>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onRefresh} disabled={loading}>
          {loading ? <span className="spinner muted" /> : 'Refresh'}
        </button>
      </div>

      {loading ? (
        <div className="card empty-state">
          <span className="spinner muted" />
          <p>Loading deals...</p>
        </div>
      ) : deals.length === 0 ? (
        <div className="card empty-state">
          <div className="emoji">⚖</div>
          <p>No deals yet. Create one above to get started.</p>
        </div>
      ) : (
        <div className="deals-list">
          {deals.map((deal) => (
            <DealCard
              key={deal.id}
              deal={deal}
              account={account}
              pendingAction={pendingAction}
              onRun={onRun}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function DealCard({ deal, account, pendingAction, onRun }) {
  const isFunded = deal.status === 'FUNDED'
  const isDisputed = deal.status === 'DISPUTED'
  const freelancerEvidenceSubmitted =
    isDisputed && deal.freelancer_evidence_url && deal.freelancer_evidence_url.trim() !== ''
  const resolving = pendingAction === `resolve_${deal.id}`
  const releasing = pendingAction === `release_${deal.id}`

  return (
    <article className="deal-card">
      <div className="deal-card-top">
        <div>
          <div className="deal-id">DEAL #{deal.id}</div>
          <h3 className="deal-desc">{deal.description || 'Untitled deal'}</h3>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span className={`status-badge ${deal.status}`}>{prettyStatus(deal.status)}</span>
          <span className="deal-amount">
            {deal.amount}
            <span className="unit"> GEN</span>
          </span>
        </div>
      </div>

      <div className="deal-meta">
        <div>
          <span className="label">Client</span>
          <span className="value">{shortenAddress(deal.client)}</span>
        </div>
        <div>
          <span className="label">Freelancer</span>
          <span className="value">{shortenAddress(deal.freelancer)}</span>
        </div>
      </div>

      {isDisputed && (deal.client_evidence_url || deal.freelancer_evidence_url) && (
        <div className="evidence-row">
          {deal.client_claim && (
            <div style={{ marginBottom: 6 }}>
              <span className="ev-label">Client claim:</span>
              {deal.client_claim}
            </div>
          )}
          {deal.client_evidence_url && (
            <div style={{ marginBottom: 6 }}>
              <span className="ev-label">Client evidence:</span>
              <a href={deal.client_evidence_url} target="_blank" rel="noreferrer">
                {deal.client_evidence_url}
              </a>
            </div>
          )}
          {deal.freelancer_claim && (
            <div style={{ marginBottom: 6 }}>
              <span className="ev-label">Freelancer claim:</span>
              {deal.freelancer_claim}
            </div>
          )}
          {deal.freelancer_evidence_url && (
            <div>
              <span className="ev-label">Freelancer evidence:</span>
              <a href={deal.freelancer_evidence_url} target="_blank" rel="noreferrer">
                {deal.freelancer_evidence_url}
              </a>
            </div>
          )}
        </div>
      )}

      {deal.resolution_reasoning && deal.resolution_reasoning.trim() !== '' && (
        <div className="verdict">
          <div className="verdict-label">AI verdict reasoning</div>
          <p className="verdict-text">{deal.resolution_reasoning}</p>
        </div>
      )}

      {(isFunded || freelancerEvidenceSubmitted) && account && (
        <div className="deal-actions">
          {isFunded && (
            <button
              className="btn btn-sm"
              disabled={releasing}
              onClick={() =>
                onRun(`release_${deal.id}`, 'Release funds', (client) =>
                  writeAndWait(client, {
                    functionName: 'release_funds',
                    args: [Number(deal.id)],
                    value: 0n,
                  }),
                )
              }
            >
              {releasing ? <><span className="spinner" /> Releasing...</> : 'Release funds'}
            </button>
          )}
          {freelancerEvidenceSubmitted && (
            <button
              className="btn btn-sm btn-danger"
              disabled={resolving}
              onClick={() =>
                onRun(`resolve_${deal.id}`, 'Resolve dispute (AI arbitration)', (client) =>
                  writeAndWait(client, {
                    functionName: 'resolve_dispute',
                    args: [Number(deal.id)],
                    value: 0n,
                  }),
                )
              }
            >
              {resolving ? <><span className="spinner" /> Resolving...</> : 'Resolve dispute'}
            </button>
          )}
        </div>
      )}

      {freelancerEvidenceSubmitted && (
        <div className="resolve-warning">
          This can take 1–3 minutes — AI validators are reviewing both sides' evidence.
        </div>
      )}
    </article>
  )
}

function Footer() {
  return (
    <footer className="app-footer">
      <span>AI Arbitration Escrow · built on GenLayer</span>
      <span className="footer-chain">
        <a href="https://github.com/MIKI4222/ai-arbitration-escrow" target="_blank" rel="noreferrer">
          GitHub repository ↗
        </a>
      </span>
    </footer>
  )
}

function prettyStatus(s) {
  return (s || '').replace(/_/g, ' ')
}

function humanError(e) {
  if (!e) return 'Unknown error'
  const msg = e?.shortMessage || e?.message || String(e)
  if (/user rejected/i.test(msg)) return 'Transaction rejected in wallet.'
  if (/not installed/i.test(msg)) return 'No wallet detected. Install OKX Wallet, MetaMask, or Rabby.'
  return msg
}

export default App

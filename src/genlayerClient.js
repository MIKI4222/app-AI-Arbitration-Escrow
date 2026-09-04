import { createClient } from 'genlayer-js'
import { testnetBradbury } from 'genlayer-js/chains'  // ← было testnetAsimov!
import { TransactionStatus } from 'genlayer-js/types'
import { custom } from 'viem'

export const CONTRACT_ADDRESS = '0xe9Ab6F82D60AcAAbb0232B99Ee1EB7c868A3Df41'

export const explorerTxUrl = (hash) =>
  `https://explorer-bradbury.genlayer.com/tx/${hash}`

export function shortenAddress(addr) {
  if (!addr || typeof addr !== 'string') return ''
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

export function hasWallet() {
  return typeof window !== 'undefined' && !!window.ethereum
}

export async function connectWallet() {
  if (!hasWallet()) throw new Error('No wallet detected.')
  const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' })
  const address = accounts?.[0]
  if (!address) throw new Error('No account returned by wallet.')
  return address
}

export function makeClient(account) {
  return createClient({
    chain: testnetBradbury,  // ← правильный chain с правильными consensus контрактами!
    account,
    transport: custom(window.ethereum),
  })
}

export function makeReadClient() {
  return createClient({ chain: testnetBradbury })
}

function parseDeal(raw) {
  if (raw == null) return null
  if (typeof raw === 'string' && raw.trim() === '') return null
  if (typeof raw === 'string') {
    try { return JSON.parse(raw) } catch { return null }
  }
  return raw
}

export async function fetchAllDeals(client) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: 'get_all_deals',
    args: [],
  })
  if (!Array.isArray(raw)) return []
  return raw.map(parseDeal).filter(Boolean)
}

export async function fetchDeal(client, dealId) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: 'get_deal',
    args: [Number(dealId)],
  })
  return parseDeal(raw)
}

export async function fetchTotalDeals(client) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: 'total_deals',
    args: [],
  })
  const n = Number(raw)
  return Number.isFinite(n) ? n : 0
}

export async function writeAndWait(client, { functionName, args, value }) {
  const hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName,
    args,
    value,
  })

  // Wait for the transaction to actually be ACCEPTED by consensus before
  // returning — submission alone doesn't mean the contract state changed.
  const receipt = await client.waitForTransactionReceipt({
    hash,
    status: TransactionStatus.ACCEPTED,
    retries: 100,
    interval: 3000,
  })

  const execResult =
    receipt?.tx_execution_result ??
    receipt?.txExecutionResult ??
    receipt?.result ??
    receipt?.status
  const failed =
    typeof execResult === 'string' && /error|revert|fail/i.test(execResult)
  if (failed) {
    throw new Error(`Transaction executed but reverted: ${execResult}`)
  }

  return hash
}

// Polls get_deal until it reflects one of the expected statuses, or gives
// up after the timeout. Used so the UI only reports success once the
// contract state has actually confirmed the transition, not just once the
// transaction hash comes back.
export async function waitForDealStatus(
  readClient,
  dealId,
  expectedStatuses,
  { timeoutMs = 30000, intervalMs = 1500 } = {},
) {
  const wanted = Array.isArray(expectedStatuses) ? expectedStatuses : [expectedStatuses]
  const deadline = Date.now() + timeoutMs
  let lastDeal = null
  while (Date.now() < deadline) {
    lastDeal = await fetchDeal(readClient, dealId)
    if (lastDeal && wanted.includes(lastDeal.status)) {
      return lastDeal
    }
    await new Promise((res) => setTimeout(res, intervalMs))
  }
  throw new Error(
    `Timed out waiting for deal #${dealId} to reach status ${wanted.join(' or ')}` +
      (lastDeal ? ` (currently ${lastDeal.status})` : ''),
  )
}

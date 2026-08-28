import { createClient } from 'genlayer-js'
import { testnetBradbury } from 'genlayer-js/chains'  // ← было testnetAsimov!
import { custom } from 'viem'

export const CONTRACT_ADDRESS = '0x30d00D02ED527cf4a5d227887F7f62Eb3d580bF6'

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
  return hash
}

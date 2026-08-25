import { createClient } from 'genlayer-js'
import { testnetAsimov } from 'genlayer-js/chains'

export const CONTRACT_ADDRESS = '0xE0c2E19d0E90b87dC9C547bD1F89123192b9bB2a'

export const bradburyChain = {
  ...testnetAsimov,
  id: 4221,
  name: 'GenLayer Bradbury Testnet',
  rpcUrls: { default: { http: ['https://rpc-bradbury.genlayer.com'] } },
  blockExplorers: {
    default: {
      name: 'GenLayer Bradbury Explorer',
      url: 'https://explorer-bradbury.genlayer.com',
    },
  },
}

export const explorerTxUrl = (hash) =>
  `${bradburyChain.blockExplorers.default.url}/tx/${hash}`

export function shortenAddress(addr) {
  if (!addr || typeof addr !== 'string') return ''
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

export function hasWallet() {
  return typeof window !== 'undefined' && !!window.ethereum
}

export async function connectWallet() {
  if (!hasWallet()) {
    throw new Error('No wallet detected. Install OKX Wallet, MetaMask, or Rabby.')
  }
  const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' })
  const address = accounts?.[0]
  if (!address) throw new Error('No account returned by wallet.')
  return address
}

export function makeClient(account) {
  return createClient({ chain: bradburyChain, account })
}

export function makeReadClient() {
  return createClient({ chain: bradburyChain })
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
  const n = typeof raw === 'bigint' ? Number(raw) : Number(raw)
  return Number.isFinite(n) ? n : 0
}

export async function writeAndWait(_client, { functionName, args, value }) {
  const accounts = await window.ethereum.request({ method: 'eth_accounts' })
  const from = accounts[0]
  if (!from) throw new Error('No account connected')

  // ✅ ПРАВИЛЬНЫЙ порядок ключей: args ПЕРВЫЙ, method ВТОРОЙ
  // Именно такой формат в успешной транзакции Studio:
  // {"args":[...],"method":"create_deal"}
  const payload = JSON.stringify({ args, method: functionName })
  const encoded = new TextEncoder().encode(payload)
  const hexData = '0x' + Array.from(encoded)
    .map(b => b.toString(16).padStart(2, '0'))
    .join('')

  const valueHex = (value && value > 0n) ? '0x' + value.toString(16) : '0x0'

  const hash = await window.ethereum.request({
    method: 'eth_sendTransaction',
    params: [{
      from,
      to: CONTRACT_ADDRESS,
      data: hexData,
      value: valueHex,
      gas: '0x1E84800', // 32_000_000
    }],
  })

  // Не ждём receipt — GenLayer consensus занимает 1-3 мин
  // Deal появится после нажатия Refresh
  return hash
}
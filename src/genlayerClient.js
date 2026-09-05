import { createClient } from "genlayer-js";
import { testnetBradbury } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import { custom } from "viem";

export const CONTRACT_ADDRESS = "0xE6f098D1B49d67390bFec8C8169aeFd99868878f";

// Time locks mirror the contract constants (seconds).
export const DISPUTE_CANCEL_DELAY_SECONDS = 7 * 24 * 60 * 60;
export const ARBITRATION_RECOVERY_DELAY_SECONDS = 3 * 24 * 60 * 60;

export const explorerTxUrl = (hash) =>
  `https://explorer-bradbury.genlayer.com/tx/${hash}`;

export function shortenAddress(addr) {
  if (!addr || typeof addr !== "string") return "";
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

export function sameAddress(a, b) {
  if (!a || !b) return false;
  return String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
}

export function hasWallet() {
  return typeof window !== "undefined" && !!window.ethereum;
}

export async function connectWallet() {
  if (!hasWallet()) throw new Error("No wallet detected.");
  const accounts = await window.ethereum.request({
    method: "eth_requestAccounts",
  });
  const address = accounts?.[0];
  if (!address) throw new Error("No account returned by wallet.");
  return address;
}

export function makeClient(account) {
  return createClient({
    chain: testnetBradbury,
    account,
    transport: custom(window.ethereum),
  });
}

export function makeReadClient() {
  return createClient({ chain: testnetBradbury });
}

function parseDeal(raw) {
  if (raw == null) return null;
  if (typeof raw === "string") {
    if (raw.trim() === "") return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }
  return raw;
}

export async function fetchAllDeals(client) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_all_deals",
    args: [],
  });
  if (!Array.isArray(raw)) return [];
  return raw.map(parseDeal).filter(Boolean);
}

export async function fetchDeal(client, dealId) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_deal",
    args: [Number(dealId)],
  });
  return parseDeal(raw);
}

export async function fetchTotalDeals(client) {
  const raw = await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "total_deals",
    args: [],
  });
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

// --- transaction result inspection ----------------------------------------
//
// A GenLayer receipt can be ACCEPTED while the contract call itself reverted
// (the "ACCEPTED (ERROR)" rows in the explorer). The execution result lives in
// different places depending on node version, so collect every candidate and
// also dig into consensus_data.leader_receipt.

function collectExecutionResults(receipt) {
  const out = [];
  const push = (v) => {
    if (typeof v === "string" && v.trim() !== "") out.push(v);
  };
  if (!receipt || typeof receipt !== "object") return out;

  push(receipt.tx_execution_result);
  push(receipt.txExecutionResult);
  push(receipt.execution_result);
  push(receipt.result);
  push(receipt.status);

  const consensus = receipt.consensus_data ?? receipt.consensusData;
  const leader = consensus?.leader_receipt ?? consensus?.leaderReceipt;
  const leaders = Array.isArray(leader) ? leader : leader ? [leader] : [];
  for (const entry of leaders) {
    push(entry?.execution_result);
    push(entry?.executionResult);
    push(entry?.genvm_result?.stderr ? "ERROR" : undefined);
  }
  return out;
}

function extractRevertReason(receipt) {
  const consensus = receipt?.consensus_data ?? receipt?.consensusData;
  const leader = consensus?.leader_receipt ?? consensus?.leaderReceipt;
  const entry = Array.isArray(leader) ? leader[0] : leader;
  const raw =
    entry?.genvm_result?.stderr ||
    entry?.genvm_result?.stdout ||
    entry?.result ||
    receipt?.error ||
    "";
  const text = typeof raw === "string" ? raw : JSON.stringify(raw);
  if (!text) return "";
  // Surface the assert message the contract raised, if present.
  const assertion = text.match(/AssertionError:?\s*([^\n]+)/);
  if (assertion) return assertion[1].trim();
  return text.slice(0, 240);
}

export function receiptFailed(receipt) {
  const results = collectExecutionResults(receipt);
  return results.some((value) => /error|revert|fail/i.test(value));
}

export async function writeAndWait(client, { functionName, args, value }) {
  const hash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName,
    args,
    value,
  });

  const receipt = await client.waitForTransactionReceipt({
    hash,
    status: TransactionStatus.ACCEPTED,
    retries: 100,
    interval: 3000,
  });

  if (receiptFailed(receipt)) {
    const reason = extractRevertReason(receipt);
    throw new Error(
      `Contract rejected ${functionName}${reason ? `: ${reason}` : " (transaction reverted)"}`,
    );
  }

  return hash;
}

// --- confirmed-state polling ----------------------------------------------
//
// Success is only reported once the contract state itself satisfies `predicate`.
// Transient read failures (node lag, brand-new deal id not yet visible) are
// swallowed until the deadline instead of aborting the wait.

export async function waitForDealState(
  readClient,
  dealId,
  predicate,
  {
    timeoutMs = 180000,
    intervalMs = 2000,
    describe = "the expected state",
  } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastDeal = null;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      lastDeal = await fetchDeal(readClient, dealId);
      if (lastDeal && predicate(lastDeal)) return lastDeal;
    } catch (e) {
      lastError = e;
    }
    await new Promise((res) => setTimeout(res, intervalMs));
  }

  const detail = lastDeal
    ? ` (currently ${lastDeal.status})`
    : lastError
      ? ` (last read error: ${lastError.message || lastError})`
      : "";
  throw new Error(
    `Timed out waiting for deal #${dealId} to reach ${describe}${detail}`,
  );
}

export function waitForDealStatus(
  readClient,
  dealId,
  expectedStatuses,
  options = {},
) {
  const wanted = Array.isArray(expectedStatuses)
    ? expectedStatuses
    : [expectedStatuses];
  return waitForDealState(
    readClient,
    dealId,
    (deal) => wanted.includes(deal.status),
    {
      ...options,
      describe: `status ${wanted.join(" or ")}`,
    },
  );
}

// Freelancer evidence keeps the deal DISPUTED, so status alone cannot confirm
// it. Wait for the evidence field to actually appear on-chain.
export function waitForFreelancerEvidence(readClient, dealId, options = {}) {
  return waitForDealState(
    readClient,
    dealId,
    (deal) =>
      deal.status === "DISPUTED" &&
      typeof deal.freelancer_evidence_url === "string" &&
      deal.freelancer_evidence_url.trim() !== "",
    { ...options, describe: "recorded freelancer evidence" },
  );
}

// --- time-lock helpers -----------------------------------------------------

export function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

export function secondsUntilCancelEligible(deal) {
  const start = Number(deal?.dispute_start_time || 0);
  if (!start) return null;
  return Math.max(0, start + DISPUTE_CANCEL_DELAY_SECONDS - nowSeconds());
}

export function secondsUntilRecoveryEligible(deal) {
  const start = Number(deal?.freelancer_evidence_time || 0);
  if (!start) return null;
  return Math.max(0, start + ARBITRATION_RECOVERY_DELAY_SECONDS - nowSeconds());
}

export function formatDuration(seconds) {
  if (seconds == null) return "unknown";
  if (seconds <= 0) return "now";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${Math.max(1, minutes)}m`;
}

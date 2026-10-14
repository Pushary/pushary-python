import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fork } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createApprovalGrant, createProviderGate, createMemoryGrantStore, digestObject } from './independent-recovery-pack/node_modules/@trust-graduation/core/src/index.js';
import { createFileTurnStore } from './independent-recovery-pack/node_modules/@trust-graduation/core/examples/turn-boundary/file-store.mjs';
import { createTurnWorkflow } from './independent-recovery-pack/node_modules/@trust-graduation/core/examples/turn-boundary/workflow.mjs';

const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, 'independent-results');
fs.mkdirSync(output);
const worker = path.join(root, 'independent-recovery-pack/recovery-worker.mjs');
const fixedNow = new Date('2026-10-14T10:00:00Z');

function run(directory, command, mode = 'normal', killAt = '') {
  return new Promise((resolve, reject) => {
    const child = fork(worker, [directory, command, mode], { stdio: ['ignore', 'pipe', 'pipe', 'ipc'], env: { PATH: process.env.PATH } });
    let result, checkpoint, stderr = '';
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(Error('worker timeout')); }, 10000);
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('message', message => {
      if (message.result) result = message.result;
      if (killAt && message.checkpoint === killAt) { checkpoint = killAt; child.kill('SIGKILL'); }
    });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      fs.appendFileSync(path.join(directory, 'independent-workers.jsonl'), JSON.stringify({ command, mode, pid: child.pid, code, signal, checkpoint, result, stderr }) + '\n');
      if (killAt ? checkpoint === killAt && signal === 'SIGKILL' : code === 0 && result) resolve(result);
      else reject(Error(stderr || `Unexpected worker exit ${code}/${signal}`));
    });
  });
}
async function approved(name, crash = false) {
  const directory = path.join(output, name);
  fs.mkdirSync(directory);
  assert.equal((await run(directory, 'request')).status, 'pending_approval');
  assert.equal((await run(directory, 'approve')).status, 'approved');
  if (crash) await run(directory, 'resume', 'pause-after-commit', 'effect_committed');
  return directory;
}
function counts(directory) {
  const trace = fs.readFileSync(path.join(directory, 'trace.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  return { dispatches: trace.filter(row => row.kind === 'dispatch_attempt').length, effects: trace.filter(row => row.kind === 'effect_committed').length };
}
function latest(directory) {
  const name = fs.readdirSync(directory).filter(name => /^\d{10}\.json$/.test(name)).sort().at(-1);
  assert.ok(name);
  return path.join(directory, name);
}
function providerFile(directory) {
  const base = path.join(directory, 'provider/requests');
  const keys = fs.readdirSync(base);
  assert.equal(keys.length, 1);
  return latest(path.join(base, keys[0]));
}

test('twelve fresh workers race to resume: exactly one dispatch and durable effect', async () => {
  const directory = await approved('concurrent-resume');
  await Promise.all(Array.from({ length: 12 }, () => run(directory, 'resume')));
  assert.equal((await run(directory, 'resume')).status, 'completed');
  assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
  const ledger = JSON.parse(fs.readFileSync(providerFile(directory))).value;
  const receipt = JSON.parse(fs.readFileSync(latest(path.join(directory, 'host/receipts/operation-1')))).value;
  assert.equal(receipt.providerResultHash, digestObject(ledger.result));
});

test('wrong action/input evidence and corrupt provider JSON never confirm or resend; restoration recovers', async () => {
  const directory = await approved('bad-provider-evidence', true);
  const file = providerFile(directory);
  const original = fs.readFileSync(file, 'utf8');
  for (const field of ['actionHash', 'inputHash']) {
    const changed = JSON.parse(original);
    changed.value[field] = 'sha256:' + '0'.repeat(64);
    fs.writeFileSync(file, JSON.stringify(changed));
    const result = await run(directory, 'resume');
    assert.equal(result.status, 'outcome_unknown');
    assert.equal(fs.existsSync(path.join(directory, 'host/receipts/operation-1')), false);
    assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
  }
  fs.writeFileSync(file, '{broken');
  assert.equal((await run(directory, 'resume')).status, 'outcome_unknown');
  assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
  fs.writeFileSync(file, original);
  assert.equal((await run(directory, 'resume')).status, 'completed');
  assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
});

test('mismatched and corrupt receipts do not report completion; restored receipt recovers without dispatch', async () => {
  const directory = await approved('bad-receipt');
  assert.equal((await run(directory, 'resume')).status, 'completed');
  const file = latest(path.join(directory, 'host/receipts/operation-1'));
  const original = fs.readFileSync(file, 'utf8');
  const changed = JSON.parse(original);
  changed.value.providerResultHash = 'sha256:' + '0'.repeat(64);
  fs.writeFileSync(file, JSON.stringify(changed));
  assert.equal((await run(directory, 'resume')).status, 'outcome_unknown');
  fs.writeFileSync(file, '{broken');
  assert.equal((await run(directory, 'resume')).status, 'outcome_unknown');
  fs.writeFileSync(file, original);
  assert.equal((await run(directory, 'resume')).status, 'completed');
  assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
});

test('revocation after a committed effect does not prevent read-only recovery or authorize a new dispatch', async () => {
  const directory = await approved('revoke-after-commit', true);
  await run(directory, 'revoke');
  assert.equal((await run(directory, 'resume')).status, 'completed');
  assert.equal((await run(directory, 'resume')).status, 'completed');
  assert.deepEqual(counts(directory), { dispatches: 1, effects: 1 });
  const trace = fs.readFileSync(path.join(directory, 'trace.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(trace.filter(row => row.kind === 'consume_attempt').length, 1);
});

const action = { actionClass: 'email.send.external', workspace: 'synthetic-workspace', principal: 'synthetic-human', tenant: 'synthetic-tenant', target: 'recipient@example.invalid', input: { body: 'Synthetic reviewed payload' } };

test('approval expiring during atomic consumption prevents provider dispatch', async () => {
  let currentTime = fixedNow, dispatches = 0, receipts = 0, consumes = 0;
  const memory = createMemoryGrantStore();
  const gate = createProviderGate({ now: () => currentTime, authenticateGrant: async () => true,
    store: { consume: async identity => { consumes++; const result = await memory.consume(identity); currentTime = new Date('2026-10-14T10:11:00Z'); return result; } },
    provider: async () => { dispatches++; }, writeReceipt: async () => { receipts++; } });
  const binding = gate.prepare(action);
  const approval = createApprovalGrant({ binding, grantId: 'expiry-grant', issuer: 'synthetic-host', issuedAt: fixedNow.toISOString() });
  const result = await gate.execute({ binding, approval, action });
  assert.equal(result.reason, 'grant_expired');
  assert.equal(consumes, 1);
  assert.equal(dispatches, 0);
  assert.equal(receipts, 0);
});

async function racingFixture(name) {
  const directory = path.join(output, name);
  fs.mkdirSync(directory);
  const store = createFileTurnStore(path.join(directory, 'host'));
  let evidence, rejectProvider, announceCommit;
  const committed = new Promise(resolve => { announceCommit = resolve; });
  let dispatches = 0;
  const workflow = createTurnWorkflow({ store, now: () => fixedNow, authenticateDecision: async () => true,
    provider: async (input, { binding }) => { dispatches++; evidence = { actionHash: binding.actionHash, inputHash: binding.inputHash, result: { id: 'synthetic-result', accepted: input } }; announceCommit(); return new Promise((resolve, reject) => { rejectProvider = reject; }); },
    lookupProviderEvidence: async () => evidence });
  const request = workflow.request({ requestId: name, policyId: 'synthetic-policy', action });
  const approval = createApprovalGrant({ binding: request.binding, grantId: name, issuer: 'synthetic-host', issuedAt: fixedNow.toISOString() });
  await workflow.decide({ requestId: name, decision: { requestId: name, policyId: request.policyId, actionHash: request.binding.actionHash, state: 'approved', approval } });
  const first = workflow.resume(name);
  await committed;
  return { directory, store, workflow, first, evidence, rejectProvider, dispatches: () => dispatches };
}

test('late provider failure cannot overwrite a newer verified completion or receipt', async () => {
  const f = await racingFixture('late-error');
  assert.equal((await f.workflow.resume('late-error')).status, 'completed');
  const receipt = f.store.readReceipt('late-error');
  f.rejectProvider(Error('synthetic delayed error'));
  assert.equal((await f.first).status, 'completed');
  assert.equal((await f.workflow.resume('late-error')).status, 'completed');
  assert.equal(f.store.readReceipt('late-error').receiptId, receipt.receiptId);
  assert.equal(f.store.readReceipt('late-error').outcome, 'provider_confirmed');
  assert.equal(f.dispatches(), 1);
});

test('stale missing-evidence lookup cannot overwrite a newer completion', async () => {
  const f = await racingFixture('stale-lookup');
  let resolveLookup, announceLookup;
  const lookupStarted = new Promise(resolve => { announceLookup = resolve; });
  const stale = createTurnWorkflow({ store: f.store, now: () => fixedNow, authenticateDecision: async () => true,
    provider: async () => assert.fail('reconciliation must not dispatch'),
    lookupProviderEvidence: async () => { announceLookup(); return new Promise(resolve => { resolveLookup = resolve; }); } });
  const oldLookup = stale.resume('stale-lookup');
  await lookupStarted;
  assert.equal((await f.workflow.resume('stale-lookup')).status, 'completed');
  resolveLookup(null);
  assert.equal((await oldLookup).status, 'completed');
  f.rejectProvider(Error('synthetic delayed error'));
  await f.first;
  assert.equal(f.store.get('stale-lookup').status, 'completed');
  assert.equal(f.dispatches(), 1);
});

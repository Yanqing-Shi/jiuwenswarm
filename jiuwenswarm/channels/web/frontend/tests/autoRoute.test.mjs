import assert from 'node:assert/strict';
import test from 'node:test';

import {
  afterRouteDecision,
  decideAutoRoute,
  isRouterFailure,
  parseAutoRouteSettings,
  requestModeRoute,
  routeModeOf,
  serializeAutoRouteSettings,
} from '../node_modules/.cache/session-mode-commit/features/sessionMode/autoRoute.js';

function codedError(code) {
  return Object.assign(new Error(code ?? 'lost'), code ? { code } : {});
}

/** A `request` that records its calls and answers `mode.route` with `answer` (or throws it). */
function router(answer) {
  const calls = [];
  const request = async (method, params, options) => {
    calls.push({ method, params, options });
    if (answer instanceof Error) throw answer;
    return answer;
  };
  return { request, calls };
}

function deps(request, { alert = true, confirm } = {}) {
  const asked = [];
  return {
    asked,
    request,
    alert: typeof alert === 'function' ? alert : () => alert,
    confirm: async (confirmation) => {
      asked.push(confirmation);
      return confirm ?? false;
    },
  };
}

test('"auto" is off and "alert" is on until the user chooses otherwise', () => {
  const defaults = { auto: false, alert: true };
  for (const raw of [null, undefined, '', 'not json', '3', 'null', '[]', '{"auto":"yes","alert":1}']) {
    assert.deepEqual(parseAutoRouteSettings(raw), defaults, String(raw));
  }
  assert.deepEqual(parseAutoRouteSettings('{"auto":true}'), { auto: true, alert: true });
  assert.deepEqual(parseAutoRouteSettings('{"auto":true,"alert":false,"other":1}'), { auto: true, alert: false });
  const chosen = { auto: true, alert: false };
  assert.deepEqual(parseAutoRouteSettings(serializeAutoRouteSettings(chosen)), chosen);
});

test('the route of a mode is its Agent or Team part', () => {
  assert.equal(routeModeOf('agent.work.normal'), 'agent');
  assert.equal(routeModeOf('team.code.plan'), 'team');
  assert.equal(routeModeOf(' Team '), 'team');
  for (const other of ['auto_harness', '', 'work.team', undefined, null, 3]) {
    assert.equal(routeModeOf(other), undefined, String(other));
  }
});

test('a saved conversation is routed by its id, a new one by the mode it would be created with', async () => {
  const saved = router({ route: 'team', reason: 'needs several workers', confidence: 0.8 });
  assert.deepEqual(
    await requestModeRoute(saved.request, { sessionId: 'web_1', query: 'build it', draftMode: 'agent.work.normal' }),
    { status: 'decided', route: 'team', reason: 'needs several workers', confidence: 0.8 },
  );
  assert.equal(saved.calls.length, 1);
  assert.equal(saved.calls[0].method, 'mode.route');
  assert.deepEqual(saved.calls[0].params, { query: 'build it', session_id: 'web_1' });
  assert.ok(saved.calls[0].options.timeoutMs > 3000, 'the client waits longer than the server waits for the router');

  const draft = router({ route: 'agent' });
  assert.deepEqual(
    await requestModeRoute(draft.request, { query: 'hello', draftMode: 'team.code.plan' }),
    { status: 'decided', route: 'agent', reason: '', confidence: null },
  );
  assert.deepEqual(draft.calls[0].params, { query: 'hello', current_mode: 'team.code.plan' });
});

test('an answer without a usable route, or a refused request, is no decision', async () => {
  for (const payload of [{}, { route: 'auto' }, { route: 3 }, null, undefined]) {
    const outcome = await requestModeRoute(router(payload).request, { sessionId: 'web_1', query: 'q' });
    assert.equal(outcome.status, 'unavailable', JSON.stringify(payload));
    assert.equal(outcome.code, 'ROUTER_BAD_ANSWER');
  }
  const refused = await requestModeRoute(router(codedError('ROUTER_UNAVAILABLE')).request, { sessionId: 'web_1', query: 'q' });
  assert.equal(refused.status, 'unavailable');
  assert.equal(refused.code, 'ROUTER_UNAVAILABLE');
  const lost = await requestModeRoute(router(codedError()).request, { sessionId: 'web_1', query: 'q' });
  assert.deepEqual([lost.status, lost.code], ['unavailable', undefined]);
});

test('only a router that was asked and gave nothing usable is reported to the user', () => {
  const unavailable = (code) => ({ status: 'unavailable', code, error: codedError(code) });
  for (const code of ['ROUTER_UNAVAILABLE', 'ROUTER_BAD_ANSWER', 'REQUEST_TIMEOUT', 'WS_DISCONNECTED', 'INTERNAL_ERROR', undefined]) {
    assert.equal(isRouterFailure(unavailable(code)), true, String(code));
  }
  // The conversation or the server is not routed at all: nothing to report.
  for (const code of ['ROUTER_DISABLED', 'BAD_REQUEST', 'NOT_FOUND', 'MODE_UNKNOWN', 'WS_NOT_READY']) {
    assert.equal(isRouterFailure(unavailable(code)), false, code);
  }
  assert.equal(isRouterFailure({ status: 'decided', route: 'team', reason: '', confidence: null }), false);
});

test('a decision for the current mode sends; another mode asks with "alert" and switches without it', () => {
  for (const alert of [true, false]) {
    assert.equal(afterRouteDecision({ route: 'agent', currentMode: 'agent', alert }), 'send');
    assert.equal(afterRouteDecision({ route: 'team', currentMode: 'team', alert }), 'send');
  }
  assert.equal(afterRouteDecision({ route: 'team', currentMode: 'agent', alert: true }), 'confirm');
  assert.equal(afterRouteDecision({ route: 'agent', currentMode: 'team', alert: true }), 'confirm');
  assert.equal(afterRouteDecision({ route: 'team', currentMode: 'agent', alert: false }), 'switch');
  assert.equal(afterRouteDecision({ route: 'agent', currentMode: 'team', alert: false }), 'switch');
});

test('the router keeping the current mode runs the prompt without asking', async () => {
  const { request } = router({ route: 'agent' });
  const d = deps(request);
  const decision = await decideAutoRoute(d, { sessionId: 'web_1', query: 'q', currentMode: 'agent' });
  assert.equal(decision.target, null);
  assert.equal(decision.outcome.status, 'decided');
  assert.deepEqual(d.asked, []);
});

test('with "alert", Accept switches and Deny keeps the current mode', async () => {
  const { request } = router({ route: 'team', reason: 'needs several workers' });
  const accepted = deps(request, { confirm: true });
  assert.equal(
    (await decideAutoRoute(accepted, { sessionId: 'web_1', query: 'build it', currentMode: 'agent' })).target,
    'team',
  );
  assert.deepEqual(accepted.asked, [
    { currentMode: 'agent', targetMode: 'team', reason: 'needs several workers', query: 'build it' },
  ]);

  const denied = deps(request, { confirm: false });
  const decision = await decideAutoRoute(denied, { sessionId: 'web_1', query: 'build it', currentMode: 'agent' });
  assert.equal(decision.target, null);
  assert.equal(decision.outcome.route, 'team', 'the prompt still runs: the decision is kept for the caller');
  assert.equal(denied.asked.length, 1);
});

test('without "alert" the switch is decided without asking', async () => {
  const { request } = router({ route: 'agent' });
  const d = deps(request, { alert: false });
  assert.equal((await decideAutoRoute(d, { sessionId: 'web_1', query: 'q', currentMode: 'team' })).target, 'agent');
  assert.deepEqual(d.asked, []);
});

test('"alert" is read when the answer arrives, not when the prompt was sent', async () => {
  let alert = true;
  const request = async () => {
    alert = false; // unchecked while the router was answering
    return { route: 'team' };
  };
  const d = deps(request, { alert: () => alert });
  assert.equal((await decideAutoRoute(d, { sessionId: 'web_1', query: 'q', currentMode: 'agent' })).target, 'team');
  assert.deepEqual(d.asked, []);
});

test('no decision keeps the current mode and asks nothing', async () => {
  for (const answer of [codedError('ROUTER_UNAVAILABLE'), codedError('BAD_REQUEST'), codedError(), {}]) {
    const d = deps(router(answer).request);
    const decision = await decideAutoRoute(d, { sessionId: 'web_1', query: 'q', currentMode: 'agent' });
    assert.equal(decision.target, null);
    assert.equal(decision.outcome.status, 'unavailable');
    assert.deepEqual(d.asked, []);
  }
});

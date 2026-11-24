import assert from 'node:assert/strict';
import test from 'node:test';

import {
  WORKFLOW_AUTORUN_MAX_PROMPTS,
  decodeWorkflowAutorun,
  encodeWorkflowAutorun,
  takeWorkflowAutorun,
} from '../node_modules/.cache/session-mode-commit/features/sessionMode/workflowAutorun.js';

/** The value a launcher outside the page writes: URL-safe base64 of UTF-8 JSON, no padding. */
function launcherValue(payload) {
  return Buffer.from(JSON.stringify(payload), 'utf8').toString('base64url');
}

test('a workflow survives the address, with line breaks, formulas and non-Latin text', () => {
  const workflow = {
    name: 'math-two-turn',
    prompts: ['Solve only problem 1.\n\n1. $\\frac{(x - h)^2}{a^2}$ ?>>> 第一题', 'Now solve the rest.'],
  };
  const value = encodeWorkflowAutorun(workflow);
  assert.match(value, /^[A-Za-z0-9_-]+$/);
  assert.equal(value, launcherValue(workflow));
  assert.deepEqual(decodeWorkflowAutorun(value), workflow);
  // Padding and surrounding blanks are accepted.
  assert.deepEqual(decodeWorkflowAutorun(` ${Buffer.from(JSON.stringify(workflow)).toString('base64')} `), workflow);
});

test('the address carries prompts only; anything else in it is dropped', () => {
  const value = launcherValue({ name: ' two ', prompts: ['a', 'b'], modes: ['team', 'agent'], turns: [{ mode: 'team' }] });
  assert.deepEqual(decodeWorkflowAutorun(value), { name: 'two', prompts: ['a', 'b'] });
  assert.deepEqual(decodeWorkflowAutorun(launcherValue({ prompts: ['a'] })), { name: '', prompts: ['a'] });
});

test('an incomplete or oversized workflow is refused whole', () => {
  const tooMany = Array.from({ length: WORKFLOW_AUTORUN_MAX_PROMPTS + 1 }, (_, index) => `turn ${index}`);
  const refused = [
    null,
    undefined,
    '',
    'not base64 !!',
    Buffer.from('not json').toString('base64url'),
    Buffer.from([0xff, 0xfe, 0xfd]).toString('base64url'),
    launcherValue(null),
    launcherValue(['a']),
    launcherValue({ name: 'no prompts' }),
    launcherValue({ prompts: [] }),
    launcherValue({ prompts: 'a' }),
    launcherValue({ prompts: ['a', ''] }),
    launcherValue({ prompts: ['a', '   '] }),
    launcherValue({ prompts: ['a', 3] }),
    launcherValue({ prompts: tooMany }),
  ];
  for (const value of refused) assert.equal(decodeWorkflowAutorun(value), null, String(value));
  assert.equal(decodeWorkflowAutorun(launcherValue({ prompts: tooMany.slice(1) }))?.prompts.length, WORKFLOW_AUTORUN_MAX_PROMPTS);
});

test('the parameter leaves the address, the rest of the query stays', () => {
  const workflow = { name: 'w', prompts: ['one', 'two'] };
  const value = encodeWorkflowAutorun(workflow);
  assert.deepEqual(takeWorkflowAutorun(`?autorun=${value}`), { workflow, present: true, search: '' });
  assert.deepEqual(takeWorkflowAutorun(`?user_id=u1&autorun=${value}&modelSetupGuide=1`), {
    workflow,
    present: true,
    search: '?user_id=u1&modelSetupGuide=1',
  });
  // Unusable: still taken out, so a reload does not report it again.
  assert.deepEqual(takeWorkflowAutorun('?autorun=broken&user_id=u1'), {
    workflow: null,
    present: true,
    search: '?user_id=u1',
  });
  assert.deepEqual(takeWorkflowAutorun('?user_id=u1'), { workflow: null, present: false, search: '?user_id=u1' });
  assert.deepEqual(takeWorkflowAutorun(''), { workflow: null, present: false, search: '' });
});

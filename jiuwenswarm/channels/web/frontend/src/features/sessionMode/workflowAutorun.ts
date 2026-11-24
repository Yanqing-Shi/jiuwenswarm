/**
 * A workflow handed to the page in its address: prompts the page submits by
 * itself, one run after another, in a new conversation.
 *
 * The address carries the prompts only. Which mode a turn runs in is not part
 * of it: every turn goes through "auto" (`autoRoute.ts`), so the mode router
 * decides, exactly as for a typed prompt.
 *
 * This file holds the parts without UI state: reading and writing the
 * `autorun` parameter.
 */

export const WORKFLOW_AUTORUN_PARAM = 'autorun';
/** Longer lists are refused rather than cut: half a workflow is not the workflow. */
export const WORKFLOW_AUTORUN_MAX_PROMPTS = 20;

export interface WorkflowAutorun {
  /** Shown to the user; empty when the address gave none. */
  name: string;
  /** One prompt per turn, in order; never empty. */
  prompts: string[];
}

function fromBase64Url(value: string): string {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4));
  return new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
}

function toBase64Url(text: string): string {
  let binary = '';
  new TextEncoder().encode(text).forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** The value of the `autorun` parameter: URL-safe base64 of `{"name", "prompts"}` in UTF-8. */
export function encodeWorkflowAutorun(workflow: WorkflowAutorun): string {
  return toBase64Url(JSON.stringify({ name: workflow.name, prompts: workflow.prompts }));
}

/** Null for anything that is not a complete workflow. */
export function decodeWorkflowAutorun(value: string | null | undefined): WorkflowAutorun | null {
  if (!value) return null;
  let data: unknown;
  try {
    data = JSON.parse(fromBase64Url(value.trim()));
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const { name, prompts } = data as { name?: unknown; prompts?: unknown };
  if (!Array.isArray(prompts) || prompts.length === 0 || prompts.length > WORKFLOW_AUTORUN_MAX_PROMPTS) return null;
  if (!prompts.every((prompt): prompt is string => typeof prompt === 'string' && prompt.trim() !== '')) return null;
  return { name: typeof name === 'string' ? name.trim() : '', prompts: [...prompts] };
}

/**
 * Take the workflow out of an address's query string.
 *
 * `search` is the query without the parameter, so the caller can put it back
 * into the address: a reload must not run the workflow a second time.
 * `present` is true when the parameter was there, usable or not.
 */
export function takeWorkflowAutorun(search: string): {
  workflow: WorkflowAutorun | null;
  present: boolean;
  search: string;
} {
  const query = new URLSearchParams(search);
  if (!query.has(WORKFLOW_AUTORUN_PARAM)) return { workflow: null, present: false, search };
  const workflow = decodeWorkflowAutorun(query.get(WORKFLOW_AUTORUN_PARAM));
  query.delete(WORKFLOW_AUTORUN_PARAM);
  const rest = query.toString();
  return { workflow, present: true, search: rest ? `?${rest}` : '' };
}

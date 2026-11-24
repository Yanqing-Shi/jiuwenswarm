/**
 * "auto": before a run starts, the mode router answers whether the prompt
 * belongs in Agent or Team mode (`mode.route`). The answer commits nothing; a
 * switch still goes through `mode.set`.
 *
 * This file holds the parts without UI state: the two settings, the request,
 * and what to do with an answer.
 */

// Type-only: the Node tests load this file compiled on its own.
import type { SessionModeRequestFn } from './commitSessionMode';

export type RouteMode = 'agent' | 'team';

export interface AutoRouteSettings {
  /** The router chooses the mode; the Agent/Team control is locked. */
  auto: boolean;
  /** Ask before every switch the router decides. Only read while `auto` is on. */
  alert: boolean;
}

export const DEFAULT_AUTO_ROUTE_SETTINGS: AutoRouteSettings = { auto: false, alert: true };

export function parseAutoRouteSettings(raw: string | null | undefined): AutoRouteSettings {
  if (!raw) return { ...DEFAULT_AUTO_ROUTE_SETTINGS };
  try {
    const stored = JSON.parse(raw) as Partial<Record<keyof AutoRouteSettings, unknown>> | null;
    if (!stored || typeof stored !== 'object') return { ...DEFAULT_AUTO_ROUTE_SETTINGS };
    return {
      auto: typeof stored.auto === 'boolean' ? stored.auto : DEFAULT_AUTO_ROUTE_SETTINGS.auto,
      alert: typeof stored.alert === 'boolean' ? stored.alert : DEFAULT_AUTO_ROUTE_SETTINGS.alert,
    };
  } catch {
    return { ...DEFAULT_AUTO_ROUTE_SETTINGS };
  }
}

export function serializeAutoRouteSettings(settings: AutoRouteSettings): string {
  return JSON.stringify({ auto: settings.auto, alert: settings.alert });
}

/** The Agent/Team part of a mode name such as `team.work.normal`. */
export function routeModeOf(mode: unknown): RouteMode | undefined {
  if (typeof mode !== 'string') return undefined;
  const head = mode.trim().toLowerCase().split('.', 1)[0];
  return head === 'agent' || head === 'team' ? head : undefined;
}

export interface ModeRouteInput {
  /** Omitted for a conversation that is not saved yet. */
  sessionId?: string;
  query: string;
  /** Full mode name of a conversation that is not saved yet, e.g. `agent.work.normal`. */
  draftMode?: string;
}

export type ModeRouteOutcome =
  | { status: 'decided'; route: RouteMode; reason: string; confidence: number | null }
  /** No decision; the prompt keeps its mode. */
  | { status: 'unavailable'; code?: string; error: unknown };

function errorCode(error: unknown): string | undefined {
  const code = error && typeof error === 'object' ? (error as { code?: unknown }).code : undefined;
  return typeof code === 'string' ? code : undefined;
}

/** The server waits about three seconds for the router; this covers the path to it. */
const MODE_ROUTE_TIMEOUT_MS = 15_000;

export async function requestModeRoute(
  request: SessionModeRequestFn,
  input: ModeRouteInput,
): Promise<ModeRouteOutcome> {
  const params: Record<string, unknown> = { query: input.query };
  if (input.sessionId) params.session_id = input.sessionId;
  else params.current_mode = input.draftMode;
  try {
    const payload = await request<{ route?: unknown; reason?: unknown; confidence?: unknown }>(
      'mode.route',
      params,
      { timeoutMs: MODE_ROUTE_TIMEOUT_MS },
    );
    const route = routeModeOf(payload?.route);
    if (!route) return { status: 'unavailable', code: 'ROUTER_BAD_ANSWER', error: new Error('mode.route returned no route') };
    return {
      status: 'decided',
      route,
      reason: typeof payload?.reason === 'string' ? payload.reason : '',
      confidence: typeof payload?.confidence === 'number' ? payload.confidence : null,
    };
  } catch (error) {
    return { status: 'unavailable', code: errorCode(error), error };
  }
}

/**
 * Refusals that say this conversation or this server is not routed at all.
 * Every other failure means the router was asked and gave nothing usable,
 * which is worth telling the user.
 */
const QUIET_CODES = new Set(['ROUTER_DISABLED', 'BAD_REQUEST', 'NOT_FOUND', 'MODE_UNKNOWN', 'WS_NOT_READY']);

export function isRouterFailure(outcome: ModeRouteOutcome): boolean {
  return outcome.status === 'unavailable' && !(outcome.code !== undefined && QUIET_CODES.has(outcome.code));
}

/** What follows a decision: run as is, ask the user, or switch without asking. */
export function afterRouteDecision(input: {
  route: RouteMode;
  currentMode: RouteMode;
  alert: boolean;
}): 'send' | 'confirm' | 'switch' {
  if (input.route === input.currentMode) return 'send';
  return input.alert ? 'confirm' : 'switch';
}

export interface ModeRouteConfirmation {
  currentMode: RouteMode;
  targetMode: RouteMode;
  reason: string;
  query: string;
}

export interface AutoRouteDeps {
  request: SessionModeRequestFn;
  /** Read when the answer arrives, so unchecking "alert" meanwhile counts. */
  alert: () => boolean;
  /** Resolves true for Accept, false for Deny. */
  confirm: (confirmation: ModeRouteConfirmation) => Promise<boolean>;
}

export interface AutoRouteDecision {
  /** The mode to switch to before the run, or null to run in the current mode. */
  target: RouteMode | null;
  outcome: ModeRouteOutcome;
}

/** Ask the router about one prompt and, when "alert" is on, the user about the switch. */
export async function decideAutoRoute(
  deps: AutoRouteDeps,
  input: ModeRouteInput & { currentMode: RouteMode },
): Promise<AutoRouteDecision> {
  const outcome = await requestModeRoute(deps.request, input);
  if (outcome.status !== 'decided') return { target: null, outcome };
  const step = afterRouteDecision({ route: outcome.route, currentMode: input.currentMode, alert: deps.alert() });
  if (step === 'send') return { target: null, outcome };
  if (step === 'switch') return { target: outcome.route, outcome };
  const accepted = await deps.confirm({
    currentMode: input.currentMode,
    targetMode: outcome.route,
    reason: outcome.reason,
    query: input.query,
  });
  return { target: accepted ? outcome.route : null, outcome };
}

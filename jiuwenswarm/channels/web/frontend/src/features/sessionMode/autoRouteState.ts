/**
 * UI state of "auto": the two checkboxes, whether the server has a mode router,
 * the pop-up waiting for an answer, and the step that routes one prompt before
 * its run. The rules themselves are in `autoRoute.ts`.
 */

import { useSyncExternalStore } from 'react';
import { isEnterprise } from '../../edition';
import i18n from '../../i18n';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { webClient } from '../../services/webClient';
import { useSessionStore } from '../../stores/sessionStore';
import { toast } from '../../components/ui/Toast/toastStore';
import { getWebTransport } from '../../utils/env';
import { parseConfigBoolean } from '../settings/services/settingsContract';
import {
  decideAutoRoute,
  isRouterFailure,
  parseAutoRouteSettings,
  serializeAutoRouteSettings,
  type AutoRouteSettings,
  type ModeRouteConfirmation,
  type RouteMode,
} from './autoRoute';
import type { SessionModeRequestFn } from './commitSessionMode';
import { allowsInPlaceModeSwitch } from './inPlaceModeSwitch';

const STORAGE_KEY = 'jiuwenswarm.sessionMode.autoRoute';

const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function loadSettings(): AutoRouteSettings {
  try {
    return parseAutoRouteSettings(typeof localStorage === 'undefined' ? null : localStorage.getItem(STORAGE_KEY));
  } catch {
    return parseAutoRouteSettings(null);
  }
}

// One setting for every conversation, remembered in this browser.
let settings = loadSettings();
/** `config.get` → `mode_router_enabled`: the server has a router address. */
let routerEnabled = false;
/** False until the server has said whether it has a router. */
let routerAnswered = false;

export function getAutoRouteSettings(): AutoRouteSettings {
  return settings;
}

/**
 * `remember: false` changes the boxes for this page only: what the user chose
 * stays stored and is back after a reload. A box the user then clicks is
 * remembered as usual, together with the other box as shown.
 */
export function setAutoRouteSettings(patch: Partial<AutoRouteSettings>, options?: { remember?: boolean }): void {
  const next = { ...settings, ...patch };
  if (next.auto === settings.auto && next.alert === settings.alert) return;
  settings = next;
  if (options?.remember !== false) {
    try {
      localStorage.setItem(STORAGE_KEY, serializeAutoRouteSettings(next));
    } catch {
      // The choice still holds for this page.
    }
  }
  // A pop-up that is open belongs to the setting just switched off: the
  // prompt behind it runs in its current mode.
  if (!next.auto) denyAllModeRouteConfirmations();
  notify();
}

export function setModeRouterEnabled(value: unknown): void {
  const next = parseConfigBoolean(value ?? false);
  if (next === routerEnabled && routerAnswered) return;
  routerAnswered = true;
  routerEnabled = next;
  if (!next) denyAllModeRouteConfirmations();
  notify();
}

/** Whether the checkboxes are offered: a router exists and a mode can be switched in place. */
export function isAutoRouteAvailable(): boolean {
  return routerEnabled && allowsInPlaceModeSwitch({ enterprise: isEnterprise(), transport: getWebTransport() });
}

export type AutoRouteAvailability = 'unknown' | 'available' | 'unavailable';

/** `isAutoRouteAvailable`, told apart from "the server has not answered yet". */
export function getAutoRouteAvailability(): AutoRouteAvailability {
  if (!routerAnswered) return 'unknown';
  return isAutoRouteAvailable() ? 'available' : 'unavailable';
}

/** Whether prompts are routed right now. */
export function isAutoRouteActive(): boolean {
  return settings.auto && isAutoRouteAvailable();
}

export function useAutoRouteSettings(): AutoRouteSettings {
  return useSyncExternalStore(subscribe, getAutoRouteSettings, getAutoRouteSettings);
}

export function useAutoRouteAvailable(): boolean {
  return useSyncExternalStore(subscribe, isAutoRouteAvailable, isAutoRouteAvailable);
}

export function useAutoRouteAvailability(): AutoRouteAvailability {
  return useSyncExternalStore(subscribe, getAutoRouteAvailability, getAutoRouteAvailability);
}

export function useAutoRouteActive(): boolean {
  return useSyncExternalStore(subscribe, isAutoRouteActive, isAutoRouteActive);
}

// ---- The pop-up ----

export interface PendingModeRouteConfirmation extends ModeRouteConfirmation {
  sessionId: string;
  resolve: (accepted: boolean) => void;
}

// At most one per conversation: a conversation routes one prompt at a time.
let confirmations: ReadonlyMap<string, PendingModeRouteConfirmation> = new Map();

function askModeRouteConfirmation(sessionId: string, confirmation: ModeRouteConfirmation): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    confirmations.get(sessionId)?.resolve(false);
    const next = new Map(confirmations);
    next.set(sessionId, { ...confirmation, sessionId, resolve });
    confirmations = next;
    notify();
  });
}

export function answerModeRouteConfirmation(sessionId: string, accepted: boolean): void {
  const pending = confirmations.get(sessionId);
  if (!pending) return;
  const next = new Map(confirmations);
  next.delete(sessionId);
  confirmations = next;
  pending.resolve(accepted);
  notify();
}

function denyAllModeRouteConfirmations(): void {
  if (confirmations.size === 0) return;
  const pending = [...confirmations.values()];
  confirmations = new Map();
  pending.forEach((item) => item.resolve(false));
}

function getConfirmations(): ReadonlyMap<string, PendingModeRouteConfirmation> {
  return confirmations;
}

export function useModeRouteConfirmation(sessionId: string | null | undefined): PendingModeRouteConfirmation | undefined {
  return useSyncExternalStore(subscribe, getConfirmations, getConfirmations).get(sessionId ?? '');
}

// ---- Routing one prompt ----

/** Commits a mode in place and resolves true only when the conversation now runs it. */
type ModeSwitcher = (sessionId: string, currentMode: RouteMode, targetMode: RouteMode) => Promise<boolean>;

let modeSwitcher: ModeSwitcher | null = null;

/** The app registers its `mode.set` commit, so a routed switch is the same switch a user makes. */
export function setAutoRouteModeSwitcher(switcher: ModeSwitcher | null): void {
  modeSwitcher = switcher;
}

const failureNoticeShown = new Set<string>();

function noteRouterFailure(sessionId: string): void {
  if (failureNoticeShown.has(sessionId)) return;
  failureNoticeShown.add(sessionId);
  toast.open({
    content: i18n.t('chat.autoRoute.routerUnavailable'),
    variant: 'warning',
    testId: 'app-auto-route-unavailable-toast',
  });
}

function routeModeOfSession(sessionId: string): RouteMode | undefined {
  const mode = useSessionStore.getState().getRuntime(sessionId)?.mode;
  return mode === 'agent' || mode === 'team' ? mode : undefined;
}

/** Whether a prompt that starts a run in this conversation goes to the router first. */
export function routesPromptBeforeRun(sessionId: string | null | undefined): sessionId is string {
  if (!sessionId || !isAutoRouteActive() || !routeModeOfSession(sessionId)) return false;
  const runtime = useSessionStore.getState().getRuntime(sessionId);
  // An expert team keeps its executor for the conversation's whole life.
  if (runtime?.agentGroupBinding || runtime?.agentGroupBindingPending) return false;
  return !(sessionId === NEW_CONVERSATION_ID && runtime?.agentGroupSelectionIntent.kind === 'select');
}

export interface RoutePromptInput {
  sessionId: string;
  query: string;
  /** Full mode name the conversation would be created with; only for one not saved yet. */
  draftMode?: string;
  /** False once the prompt is gone (edited, removed, steered); a pending switch is then dropped. */
  stillWanted?: () => boolean;
}

/**
 * Route one prompt that is about to start a run, switching the mode when the
 * router (and, with "alert", the user) says so.
 *
 * Resolves `'run'` when the prompt may run now, in whatever mode the
 * conversation has, and `'hold'` when a needed switch did not commit: the
 * prompt must not run in the old mode.
 */
export async function routePromptBeforeRun(input: RoutePromptInput): Promise<'run' | 'hold'> {
  const { sessionId, query } = input;
  const currentMode = routeModeOfSession(sessionId);
  if (!currentMode) return 'run';
  const saved = sessionId !== NEW_CONVERSATION_ID;
  const request: SessionModeRequestFn = <T,>(
    method: string,
    params?: Record<string, unknown>,
    options?: { timeoutMs?: number },
  ) => webClient.request<T>(method, params, options);
  const { target, outcome } = await decideAutoRoute(
    {
      request,
      alert: () => getAutoRouteSettings().alert,
      confirm: (confirmation) =>
        // Switched off, or the prompt left, while the router was answering.
        isAutoRouteActive() && input.stillWanted?.() !== false
          ? askModeRouteConfirmation(sessionId, confirmation)
          : Promise.resolve(false),
    },
    saved ? { sessionId, query, currentMode } : { query, currentMode, draftMode: input.draftMode },
  );
  if (isRouterFailure(outcome)) noteRouterFailure(sessionId);
  if (!target || !isAutoRouteActive() || input.stillWanted?.() === false) return 'run';
  // The mode changed while the router or the user was deciding; that choice stands.
  if (routeModeOfSession(sessionId) !== currentMode) return 'run';
  if (!saved) {
    useSessionStore.getState().setMode(sessionId, target);
    return 'run';
  }
  if (!modeSwitcher) return 'run';
  return (await modeSwitcher(sessionId, currentMode, target)) ? 'run' : 'hold';
}

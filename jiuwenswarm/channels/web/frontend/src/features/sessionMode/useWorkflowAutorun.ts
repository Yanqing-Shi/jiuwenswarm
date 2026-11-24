/**
 * Runs the workflow the address handed to the page (`workflowAutorun.ts`).
 *
 * The page does what a user would do with "auto" checked and "alert"
 * unchecked: it submits the first prompt in a new conversation and puts the
 * others into the prompt queue. Each one is therefore routed right before its
 * run, by the same two routing points as typed prompts (the direct send path
 * and the queue drain), and the queue sends a turn only after the run before
 * it has ended.
 */

import { useEffect, useRef } from 'react';
import i18n from '../../i18n';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { toast } from '../../components/ui/Toast/toastStore';
import { setAutoRouteSettings, useAutoRouteAvailability } from './autoRouteState';
import { takeWorkflowAutorun, type WorkflowAutorun } from './workflowAutorun';

/** How long the first turn may take to start its run before the other turns are given up. */
const FIRST_RUN_START_TIMEOUT_MS = 120_000;

/** The page was opened for the workflow, possibly in a background tab: keep its notices up longer than the default. */
const NOTICE_SECONDS = 10;

let unreadable = false;

/** Read once, when the page loads; the parameter leaves the address so a reload starts nothing. */
function takeFromAddress(): WorkflowAutorun | null {
  if (typeof window === 'undefined') return null;
  const taken = takeWorkflowAutorun(window.location.search);
  if (!taken.present) return null;
  window.history.replaceState(
    window.history.state,
    '',
    `${window.location.pathname}${taken.search}${window.location.hash}`,
  );
  unreadable = !taken.workflow;
  return taken.workflow;
}

let waiting: WorkflowAutorun | null = takeFromAddress();

function warn(key: string, testId: string): void {
  toast.open({ content: i18n.t(key), variant: 'warning', duration: NOTICE_SECONDS, testId });
}

/**
 * Put the later turns into the queue of the conversation the first turn
 * created, once its run has started: from then on they wait behind that run.
 */
function queueWhenFirstRunStarts(prompts: string[]): void {
  if (prompts.length === 0) return;
  const started = (state: ReturnType<typeof useChatStore.getState>): string | null => {
    const sessionId = state.activeSessionId;
    if (!sessionId || sessionId === NEW_CONVERSATION_ID) return null;
    return state.runtimes[sessionId]?.isProcessing ? sessionId : null;
  };
  let unsubscribe = (): void => {};
  const timer = setTimeout(() => {
    unsubscribe();
    warn('chat.autoRoute.workflowNotStarted', 'app-workflow-autorun-not-started-toast');
  }, FIRST_RUN_START_TIMEOUT_MS);
  unsubscribe = useChatStore.subscribe((state) => {
    const sessionId = started(state);
    if (!sessionId) return;
    clearTimeout(timer);
    unsubscribe();
    // Not from inside the store's own notification.
    queueMicrotask(() => {
      const chat = useChatStore.getState();
      prompts.forEach((prompt) => chat.addToTaskQueue(sessionId, prompt));
    });
  });
}

export interface WorkflowAutorunInput {
  isConnected: boolean;
  /** The composer's submit: the direct send path, which routes the prompt first. */
  send: (content: string) => unknown;
}

/**
 * Start the waiting workflow once the page can route a prompt: connected, the
 * server has a mode router, and a new conversation in Agent or Team mode is on
 * screen. A workflow starts at most once per page load.
 */
export function useWorkflowAutorun({ isConnected, send }: WorkflowAutorunInput): void {
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const mode = useSessionStore((state) => state.runtimes[activeSessionId ?? '']?.mode);
  const availability = useAutoRouteAvailability();
  const sendRef = useRef(send);
  sendRef.current = send;

  useEffect(() => {
    if (unreadable) {
      unreadable = false;
      warn('chat.autoRoute.workflowUnreadable', 'app-workflow-autorun-unreadable-toast');
    }
    if (!waiting || !isConnected || availability === 'unknown') return;
    if (availability === 'unavailable') {
      // Without a router the turns would all run in one mode, which is not the workflow.
      waiting = null;
      warn('chat.autoRoute.workflowNeedsRouter', 'app-workflow-autorun-needs-router-toast');
      return;
    }
    if (activeSessionId !== NEW_CONVERSATION_ID || (mode !== 'agent' && mode !== 'team')) return;
    const workflow = waiting;
    waiting = null;
    // Nobody is there to answer a pop-up. For this page only: the user's own choice stays stored.
    setAutoRouteSettings({ auto: true, alert: false }, { remember: false });
    toast.open({
      content: i18n.t('chat.autoRoute.workflowStarted', { name: workflow.name || '—', turns: workflow.prompts.length }),
      variant: 'info',
      duration: NOTICE_SECONDS,
      testId: 'app-workflow-autorun-started-toast',
    });
    const [first, ...rest] = workflow.prompts;
    // Subscribed before the send, so the start of the run is not missed.
    queueWhenFirstRunStarts(rest);
    void sendRef.current(first);
  }, [activeSessionId, availability, isConnected, mode]);
}

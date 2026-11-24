import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Dialog } from '../../components/ui';
import type { RouteMode } from './autoRoute';
import { answerModeRouteConfirmation, useModeRouteConfirmation } from './autoRouteState';
import './ModeRouteConfirmDialog.css';

const MODE_LABEL_KEYS: Record<RouteMode, string> = {
  agent: 'chat.config.mode.singleAgent',
  team: 'chat.config.mode.cluster',
};

/**
 * "alert": the mode router decided this conversation's next prompt belongs in
 * the other mode. Accept switches before the prompt runs; Deny (or Esc) keeps
 * the current mode. The prompt runs either way.
 */
export function ModeRouteConfirmDialog({ sessionId }: { sessionId: string | null | undefined }) {
  const { t } = useTranslation();
  const titleId = useId();
  const pending = useModeRouteConfirmation(sessionId);
  if (!pending) return null;
  const answer = (accepted: boolean) => answerModeRouteConfirmation(pending.sessionId, accepted);
  const current = t(MODE_LABEL_KEYS[pending.currentMode]);
  const target = t(MODE_LABEL_KEYS[pending.targetMode]);
  return (
    <Dialog open titleId={titleId} className="chat-mode-route-dialog" onCancel={() => answer(false)}>
      <div data-testid="chat-panel-mode-route-confirm" data-variant={pending.targetMode}>
        <h2 id={titleId} className="chat-mode-route-dialog__title" data-testid="chat-panel-mode-route-confirm-title">
          {t('chat.autoRoute.confirmTitle', { target })}
        </h2>
        <p className="chat-mode-route-dialog__message" data-testid="chat-panel-mode-route-confirm-message">
          {t('chat.autoRoute.confirmMessage', { current, target })}
        </p>
        {pending.reason && (
          <p className="chat-mode-route-dialog__message" data-testid="chat-panel-mode-route-confirm-reason">
            {t('chat.autoRoute.confirmReason', { reason: pending.reason })}
          </p>
        )}
        <p className="chat-mode-route-dialog__prompt" data-testid="chat-panel-mode-route-confirm-prompt">
          {pending.query}
        </p>
        <div className="chat-mode-route-dialog__actions" data-testid="chat-panel-mode-route-confirm-actions">
          <Button variant="secondary" data-testid="chat-panel-mode-route-confirm-deny" onClick={() => answer(false)}>
            {t('chat.autoRoute.deny', { current })}
          </Button>
          <Button variant="primary" data-testid="chat-panel-mode-route-confirm-accept" onClick={() => answer(true)}>
            {t('chat.autoRoute.accept', { target })}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

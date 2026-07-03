import { ToastStack, Toast } from '@vigilant/ui';

export const Default = () => (
  <ToastStack>
    <Toast tone="ok">Fit saved</Toast>
    <Toast tone="info">ESI sync complete</Toast>
    <Toast tone="danger" onDismiss={() => {}}>Structure timer expired</Toast>
  </ToastStack>
);

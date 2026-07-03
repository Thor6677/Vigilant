import { render, screen, fireEvent } from '@testing-library/react';
import { ButtonGroup } from '../ButtonGroup';
import { Button } from '../Button';
import { Banner } from '../Banner';
import { Toast, ToastStack } from '../Toast';
import { Modal } from '../Modal';
import { Skeleton } from '../Skeleton';

test('ButtonGroup renders b-actions strip', () => {
  const { container } = render(
    <ButtonGroup><Button>View</Button><Button danger>Delete</Button></ButtonGroup>
  );
  expect(container.querySelector('.b-actions')).toBeTruthy();
});

test('Banner tone + dismiss', () => {
  const onDismiss = vi.fn();
  render(<Banner tone="danger" onDismiss={onDismiss}>Structure under attack</Banner>);
  const banner = screen.getByText('Structure under attack').closest('.b-banner') as HTMLElement;
  expect(banner.className).toContain('is-danger');
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
  expect(onDismiss).toHaveBeenCalled();
});

test('ToastStack positions toasts with tones', () => {
  render(
    <ToastStack>
      <Toast tone="ok">Saved</Toast>
      <Toast tone="danger">Failed</Toast>
    </ToastStack>
  );
  expect(document.querySelector('.b-toast-stack')).toBeTruthy();
  expect(screen.getByText('Failed').closest('.b-toast')!.className).toContain('is-danger');
});

test('Modal hidden when closed, interactive when open', () => {
  const onClose = vi.fn();
  const { rerender } = render(<Modal open={false} title="Confirm" onClose={onClose}>body</Modal>);
  expect(document.querySelector('.b-modal')).toBeNull();
  rerender(<Modal open title="Confirm" onClose={onClose}>body</Modal>);
  expect(document.querySelector('.b-modal')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledTimes(2);
});

test('Skeleton renders n lines', () => {
  const { container } = render(<Skeleton lines={4} />);
  expect(container.querySelectorAll('.b-skeleton')).toHaveLength(4);
});

test('Modal overlay click closes; content click does not', () => {
  const onClose = vi.fn();
  render(<Modal open title="T" onClose={onClose}>body</Modal>);
  fireEvent.click(document.querySelector('.b-modal-body')!);
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(document.querySelector('.b-modal-overlay')!);
  expect(onClose).toHaveBeenCalledTimes(1);
});

test('Modal focuses dialog on open and sets aria-modal', () => {
  render(<Modal open title="T" onClose={() => {}}>body</Modal>);
  const dialog = document.querySelector('.b-modal') as HTMLElement;
  expect(dialog.getAttribute('aria-modal')).toBe('true');
  expect(document.activeElement).toBe(dialog);
});

test('Toast optional dismiss and status role', () => {
  const onDismiss = vi.fn();
  render(<Toast tone="ok" onDismiss={onDismiss}>Saved</Toast>);
  const toast = screen.getByText('Saved').closest('.b-toast')!;
  expect(toast.getAttribute('role')).toBe('status');
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
  expect(onDismiss).toHaveBeenCalled();
});

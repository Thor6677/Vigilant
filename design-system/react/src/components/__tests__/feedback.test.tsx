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
  const { container } = render(
    <ToastStack>
      <Toast tone="ok">Saved</Toast>
      <Toast tone="danger">Failed</Toast>
    </ToastStack>
  );
  expect(container.querySelector('.b-toast-stack')).toBeTruthy();
  expect(screen.getByText('Failed').closest('.b-toast')!.className).toContain('is-danger');
});

test('Modal hidden when closed, interactive when open', () => {
  const onClose = vi.fn();
  const { rerender, container } = render(<Modal open={false} title="Confirm" onClose={onClose}>body</Modal>);
  expect(container.querySelector('.b-modal')).toBeNull();
  rerender(<Modal open title="Confirm" onClose={onClose}>body</Modal>);
  expect(container.querySelector('.b-modal')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledTimes(2);
});

test('Skeleton renders n lines', () => {
  const { container } = render(<Skeleton lines={4} />);
  expect(container.querySelectorAll('.b-skeleton')).toHaveLength(4);
});

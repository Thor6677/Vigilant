import { render, screen } from '@testing-library/react';
import { Button } from '../Button';

test('renders primary variant with shine classes', () => {
  render(<Button variant="primary">Scan</Button>);
  const btn = screen.getByRole('button', { name: 'Scan' });
  expect(btn.className).toContain('b-btn');
  expect(btn.className).toContain('is-primary');
});

test('renders ghost and danger variants', () => {
  render(<Button variant="ghost" danger>Delete</Button>);
  const btn = screen.getByRole('button', { name: 'Delete' });
  expect(btn.className).toContain('is-ghost');
  expect(btn.className).toContain('is-danger');
});

test('defaults to strip variant (bare b-btn)', () => {
  render(<Button>Refresh</Button>);
  expect(screen.getByRole('button', { name: 'Refresh' }).className.trim()).toBe('b-btn');
});

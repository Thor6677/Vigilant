import { StrictMode } from 'react';
import { render, cleanup } from '@testing-library/react';
import type { AmbientOptions } from '../../../../ambient/vigilant-ambient.js';

const { mount, destroy } = vi.hoisted(() => {
  const destroy = vi.fn();
  const mount = vi.fn((_el: HTMLElement, _options?: AmbientOptions) => ({ destroy }));
  return { mount, destroy };
});
vi.mock('../../../../ambient/vigilant-ambient.js', () => ({ mount }));

import { AmbientBackground } from '../AmbientBackground';

test('mounts ambient module and destroys on unmount', () => {
  render(<AmbientBackground systemsUrl="/data/systems.json" killSource={{ type: 'simulate' }} />);
  expect(mount).toHaveBeenCalledTimes(1);
  expect(mount.mock.calls[0][1]).toMatchObject({ systemsUrl: '/data/systems.json' });
  cleanup();
  expect(destroy).toHaveBeenCalledTimes(1);
});

test('host div passes through className and is aria-hidden', () => {
  const { container } = render(<AmbientBackground className="my-ambient" />);
  const host = container.firstChild as HTMLElement;
  expect(host.className).toBe('my-ambient');
  expect(host.getAttribute('aria-hidden')).toBe('true');
  cleanup();
});

test('StrictMode double-invokes mount and still destroys cleanly', () => {
  mount.mockClear();
  destroy.mockClear();
  render(
    <StrictMode>
      <AmbientBackground systemsUrl="/data/systems.json" killSource={{ type: 'simulate' }} />
    </StrictMode>
  );
  expect(mount).toHaveBeenCalledTimes(2);
  cleanup();
  expect(destroy.mock.calls.length).toBeGreaterThanOrEqual(1);
});

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

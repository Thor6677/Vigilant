// Preview-only surface: Vigilant is a dark-first design system (page bg #080808).
// Preview cards render on white card chrome, so /design-sync wraps every story
// in this provider to show components in their true context. Not part of the
// public component API of the Jinja site rollout.
import React from 'react';

export function DarkSurface({ children }) {
  return React.createElement(
    'div',
    {
      style: {
        background: 'var(--bg, #080808)',
        padding: '20px',
        fontFamily: "'JetBrains Mono', monospace",
        minHeight: '100%',
        boxSizing: 'border-box',
      },
    },
    children
  );
}

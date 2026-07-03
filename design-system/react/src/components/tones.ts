export type Tone = 'default' | 'accent' | 'ok' | 'warn' | 'danger' | 'muted';

export function toneClass(tone: Tone | undefined): string {
  switch (tone) {
    case 'accent': return 'is-accent';
    case 'ok': return 'is-ok';
    case 'warn': return 'is-warn';
    case 'danger': return 'is-danger';
    case 'muted': return 'is-muted';
    default: return '';
  }
}

export interface NavMenuItem {
  label: string;
  href: string;
  active?: boolean;
}

export interface NavMenuProps {
  label: string;
  items: NavMenuItem[];
  active?: boolean;
  /** trigger link target; defaults to the first item's href */
  href?: string;
}

export function NavMenu({ label, items, active = false, href }: NavMenuProps) {
  return (
    <div className="b-nav-dropdown">
      {/* Pure-CSS dropdown: aria-expanded can't be toggled without JS state; accepted limitation. */}
      <a
        className={`b-nav-link${active ? ' is-active' : ''}`}
        href={href ?? items[0]?.href ?? '#'}
        aria-haspopup="true"
      >
        {label}
        <span aria-hidden="true"> ▾</span>
      </a>
      <div className="b-nav-dropdown-menu">
        {items.map((it, i) => (
          <a key={i} className={`b-nav-dropdown-item${it.active ? ' is-active' : ''}`} href={it.href}>
            {it.label}
          </a>
        ))}
      </div>
    </div>
  );
}

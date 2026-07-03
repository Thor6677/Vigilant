export interface NavMenuItem {
  label: string;
  href: string;
  active?: boolean;
}

export interface NavMenuProps {
  label: string;
  items: NavMenuItem[];
  active?: boolean;
}

export function NavMenu({ label, items, active = false }: NavMenuProps) {
  return (
    <div className="b-nav-dropdown">
      <a className={`b-nav-link${active ? ' is-active' : ''}`} href={items[0]?.href ?? '#'}>
        {label} ▾
      </a>
      <div className="b-nav-dropdown-menu">
        {items.map((it) => (
          <a key={it.href} className={`b-nav-dropdown-item${it.active ? ' is-active' : ''}`} href={it.href}>
            {it.label}
          </a>
        ))}
      </div>
    </div>
  );
}

export interface Tab {
  label: string;
  /** presence makes this tab a link; onSelect will not fire for it */
  href?: string;
  active?: boolean;
}

export interface TabStripProps {
  tabs: Tab[];
  /** called with the tab index when a non-link tab is clicked */
  onSelect?: (index: number) => void;
}

export function TabStrip({ tabs, onSelect }: TabStripProps) {
  return (
    <div className="b-tab-strip">
      {tabs.map((tab, i) =>
        tab.href ? (
          <a key={i} href={tab.href} className={tab.active ? 'is-active' : ''}>{tab.label}</a>
        ) : (
          <button key={i} type="button" className={tab.active ? 'is-active' : ''} onClick={() => onSelect?.(i)}>
            {tab.label}
          </button>
        )
      )}
    </div>
  );
}

import '../src/styles.css';
import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import {
  AmbientBackground, NavBar, NavMenu, Breadcrumbs, PageHeader, Section, Panel,
  Grid, TabStrip, Footer, StatStrip, StatBlock, KeyValueRow, Table, TableRow,
  Badge, ProgressBar, EmptyState, Eyebrow, Button, ButtonGroup, Banner,
  Toast, ToastStack, Modal, Skeleton,
} from '../src/index';

function Demo() {
  const [modalOpen, setModalOpen] = useState(false);
  const [bannerVisible, setBannerVisible] = useState(true);
  const [view, setView] = useState<'app' | 'login'>('app');

  if (view === 'login') {
    return (
      <>
        <AmbientBackground systemsUrl="/data/systems.json" killSource={{ type: 'simulate' }} minWidth={0} />
        <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: '2rem' }}>
          <Panel glass brackets>
            <div style={{ padding: '2.5rem 3rem', textAlign: 'center', display: 'flex', flexDirection: 'column', gap: '1.25rem', alignItems: 'center' }}>
              <span className="b-nav-logo" style={{ fontSize: '18px' }}>VIGILANT</span>
              <span className="b-muted-sm">EVE ONLINE COMPANION DASHBOARD</span>
              <Button variant="primary" onClick={() => setView('app')}>LOG IN WITH EVE ONLINE</Button>
              <span className="b-muted-sm" style={{ fontSize: '9px' }}>SSO · CCP AUTHORIZED THIRD-PARTY</span>
            </div>
          </Panel>
          <button className="b-link" style={{ background: 'none', border: 'none', cursor: 'pointer' }} onClick={() => setView('app')}>← BACK TO COMPONENTS</button>
        </div>
      </>
    );
  }

  return (
    <>
      <NavBar logo="VIGILANT" right={<>
        <button className="b-nav-link" style={{ background: 'none', border: 'none', cursor: 'pointer', font: 'inherit' }} onClick={() => setView('login')}>LOGIN PREVIEW</button>
        <a className="b-nav-link" href="#">LOGOUT</a>
      </>}>
        <NavMenu label="Intel" active items={[
          { label: 'Kill Feed', href: '#kills', active: true },
          { label: 'D-Scan', href: '#dscan' },
          { label: 'Local Watch', href: '#local' },
        ]} />
        <NavMenu label="Industry" items={[
          { label: 'Jobs', href: '#jobs' },
          { label: 'Blueprints', href: '#bp' },
        ]} />
        <a className="b-nav-link" href="#map">Map</a>
      </NavBar>
      <Breadcrumbs crumbs={[{ label: 'Home', href: '#' }, { label: 'Intel', href: '#' }, { label: 'Demo' }]} />
      <main className="b-main">
        <PageHeader title="Component Demo" actions={<Button variant="primary" onClick={() => setModalOpen(true)}>Open Modal</Button>} />
        {bannerVisible && (
          <Banner tone="danger" onDismiss={() => setBannerVisible(false)}>
            Structure ALERT — Thunderborn HQ armor timer in 3h 12m
          </Banner>
        )}
        <StatStrip>
          <StatBlock label="Wallet" value="4.2B ISK" />
          <StatBlock label="Skill Queue" value="3D 14H" tone="accent" />
          <StatBlock label="Alerts" value="2" tone="danger" />
          <StatBlock label="Fleet" value="ONLINE" tone="ok" />
        </StatStrip>
        <Section title="Recent Kills" actions={<Button variant="ghost">Refresh</Button>}>
          <Table stagger>
            <TableRow><span>Loki — J121406</span><Badge tone="ok">+412M</Badge></TableRow>
            <TableRow><span>Drake — Jita</span><Badge tone="danger">−86M</Badge></TableRow>
            <TableRow><span>Ishtar — Tama</span><Badge tone="ok">+204M</Badge></TableRow>
          </Table>
        </Section>
        <Grid cols={2}>
          <Panel title="Fleet Status" glass brackets>
            <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
            <KeyValueRow label="Fuel" value="42 days" tone="warn" />
            <KeyValueRow label="Reinforced" value="—" tone="muted" />
            <div className="b-pad-md"><ProgressBar value={72} tone="warn" /></div>
          </Panel>
          <Panel title="Loading States">
            <div className="b-pad-md"><Skeleton lines={3} /></div>
            <EmptyState>No contracts found</EmptyState>
          </Panel>
        </Grid>
        <Section title="Tabs & Actions">
          <TabStrip tabs={[{ label: 'Overview', active: true }, { label: 'Assets' }, { label: 'Journal' }]} onSelect={() => {}} />
          <Eyebrow>Card actions</Eyebrow>
          <Panel>
            <div className="b-pad-md">Hangar contents…</div>
            <ButtonGroup>
              <Button>View</Button>
              <Button>Appraise</Button>
              <Button danger>Trash</Button>
            </ButtonGroup>
          </Panel>
        </Section>
      </main>
      <ToastStack>
        <Toast tone="ok">Fit saved</Toast>
        <Toast tone="info">ESI sync complete</Toast>
      </ToastStack>
      <Modal open={modalOpen} title="Confirm Jump" onClose={() => setModalOpen(false)}>
        <KeyValueRow label="Destination" value="J121406" />
        <KeyValueRow label="Topology" value="C5 → C3 → LS" tone="accent" />
        <div style={{ marginTop: '1rem', display: 'flex', gap: '8px' }}>
          <Button variant="primary" onClick={() => setModalOpen(false)}>Jump</Button>
          <Button variant="ghost" onClick={() => setModalOpen(false)}>Cancel</Button>
        </div>
      </Modal>
      <Footer links={[{ label: 'GitHub', href: '#' }, { label: 'Status', href: '#' }]} brand="THUNDERBORN" />
    </>
  );
}

createRoot(document.getElementById('root')!).render(<Demo />);

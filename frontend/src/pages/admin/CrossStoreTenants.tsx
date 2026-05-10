import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { crossStoreApi } from '../../services/api';
import { useAuthStore } from '../../stores';
import LinearCopyButton from '../../components/LinearCopyButton';

// L4 S2: cross-store tenant list. Bearer + platform_admin (component-level
// guard). Calls /api/admin/cross-store/tenants which joins BFF
// tenant_registry with local Clawith tenants.

type Row = {
    bff: {
        tenantId: string;
        businessName?: string | null;
        status?: string;
        whatsappStatus?: string;
        paperclipCompanyId?: string | null;
        clawithTenantId?: string | null;
        deferredOperatorActions?: Record<string, unknown> | null;
        createdAt?: string;
        ownerEmail?: string | null;
    };
    local: {
        id: string;
        name: string;
        agent_count?: number;
        // S7 Phase 2: surfaced from backend _local_tenant_view for retire-state filter
        retired_at?: string | null;
        retired_by?: string | null;
    } | null;
};

function StatusPill({ value, kind }: { value: string | undefined; kind: 'status' | 'wa' }) {
    if (!value) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
    const palette: Record<string, string> = kind === 'status'
        ? { active: '#22c55e', test: '#94a3b8', archived: '#64748b', provisioning: '#eab308', failed: '#ef4444' }
        : { active: '#22c55e', not_connected: '#94a3b8', allocating: '#eab308', pending_otp: '#3b82f6', failed: '#ef4444' };
    const color = palette[value] || '#94a3b8';
    return (
        <span style={{
            display: 'inline-block', padding: '2px 8px', borderRadius: 12,
            fontSize: 11, fontWeight: 600, color,
            background: `${color}1a`, border: `1px solid ${color}33`,
        }}>{value}</span>
    );
}

function deferredCount(d: Record<string, unknown> | null | undefined): number {
    if (!d) return 0;
    return Object.entries(d).filter(([, v]) => v !== false && v !== null && v !== undefined).length;
}

export default function CrossStoreTenants() {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const user = useAuthStore((s) => s.user);

    const [rows, setRows] = useState<Row[]>([]);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState<string | null>(null);
    const [statusFilter, setStatusFilter] = useState<string>('all');
    const [search, setSearch] = useState<string>('');
    const [showTest, setShowTest] = useState<boolean>(false);
    // S7 Phase 2 (R39): retire-state filter. Default 'active' matches the
    // h1_s7_retire_lifecycle partial-index pattern (idx_tenants_active WHERE
    // retired_at IS NULL) so operator default view matches the indexed path.
    const [retiredFilter, setRetiredFilter] = useState<'active' | 'retired' | 'all'>('active');

    // Drift retreat S3: view switcher + provision form
    const [view, setView] = useState<'list' | 'provision'>('list');
    const AGENT_CHARACTERS = [
        { value: 'rex',   label: 'Rex — Receptionist',  tagline: 'Front desk, bookings, FAQs',   disabled: false },
        { value: 'mara',  label: 'Mara — Marketer',     tagline: 'Pack coming soon',              disabled: true  },
        { value: 'joey',  label: 'Joey — Sales',        tagline: 'Pack coming soon',              disabled: true  },
        { value: 'cash',  label: 'Cash — Collections',  tagline: 'Pack coming soon',              disabled: true  },
        { value: 'brief', label: 'Brief — Briefer',     tagline: 'Pack coming soon',              disabled: true  },
        { value: 'tech',  label: 'Tech — Setup helper', tagline: 'Pack coming soon',              disabled: true  },
    ];
    const [provForm, setProvForm] = useState({
        businessName: '',
        ownerEmail: '',
        plan: 'base',
        didSource: 'saga',
        agentTemplate: 'rex',
        tone: 'direct-curious-helpful',
    });
    const [provSubmitting, setProvSubmitting] = useState(false);
    const [provResult, setProvResult] = useState<{ tenantId: string; businessName: string } | null>(null);
    const [provError, setProvError] = useState<string | null>(null);

    const provEmailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(provForm.ownerEmail);
    const provReady = provForm.businessName.trim().length > 0 && provEmailValid && !provSubmitting;

    async function handleProvision(e: React.FormEvent) {
        e.preventDefault();
        if (!provReady) return;
        setProvSubmitting(true);
        setProvError(null);
        try {
            const res = await crossStoreApi.provision({
                businessName: provForm.businessName.trim(),
                ownerEmail: provForm.ownerEmail.trim(),
                plan: provForm.plan,
                didSource: provForm.didSource,
                agentTemplate: provForm.agentTemplate,
                tone: provForm.tone,
            });
            setProvResult({ tenantId: res.tenantId, businessName: res.businessName });
            crossStoreApi.listTenants(showTest)
                .then((d) => setRows(d.tenants || []))
                .catch(() => {});
        } catch (err: any) {
            setProvError(err?.message || 'Provision request failed');
        } finally {
            setProvSubmitting(false);
        }
    }

    function resetProvForm() {
        setProvForm({ businessName: '', ownerEmail: '', plan: 'base', didSource: 'saga', agentTemplate: 'rex', tone: 'direct-curious-helpful' });
        setProvResult(null);
        setProvError(null);
    }

    useEffect(() => {
        setLoading(true);
        crossStoreApi.listTenants(showTest)
            .then((d) => setRows(d.tenants || []))
            .catch((e: Error) => setErr(e.message))
            .finally(() => setLoading(false));
    }, [showTest]);

    const filtered = useMemo(() => {
        const s = search.trim().toLowerCase();
        // Server-side already filters status in {test, archived} when showTest=false
        // (see /api/admin/cross-store/tenants ?includeTest). Client-side just
        // narrows the returned set by status dropdown + name search.
        return rows.filter((r) => {
            if (statusFilter !== 'all' && r.bff.status !== statusFilter) return false;
            if (s && !(r.bff.businessName || '').toLowerCase().includes(s)) return false;
            // S7 Phase 2: retire-state filter on Clawith local.retired_at
            if (retiredFilter === 'active' && r.local?.retired_at) return false;
            if (retiredFilter === 'retired' && !r.local?.retired_at) return false;
            return true;
        });
    }, [rows, statusFilter, search, retiredFilter]);

    if (user?.role !== 'platform_admin') {
        return (
            <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                {t('common.noPermission', 'You do not have permission to access this page.')}
            </div>
        );
    }

    const inputStyle: React.CSSProperties = {
        width: '100%', padding: '8px 10px',
        border: '1px solid var(--border)', borderRadius: 6,
        background: 'var(--bg-secondary)', color: 'var(--text-primary)',
        fontSize: 13,
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)' }}>
            <div className="page-header">
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', width: '100%' }}>
                    <div>
                        <h1 className="page-title">
                            {t('admin.crossStore.title', 'Cross-store tenants')}
                        </h1>
                        <p className="page-subtitle">
                            {t('admin.crossStore.subtitle', 'All tenants across BFF, Paperclip, and Clawith with their cross-namespace IDs and deferred operator actions.')}
                        </p>
                    </div>
                    <div style={{ display: 'flex', gap: 4, padding: 2, background: 'var(--bg-tertiary, #1a1a1a)', borderRadius: 6, marginTop: 4 }}>
                        {(['list', 'provision'] as const).map((v) => (
                            <button
                                key={v}
                                onClick={() => { setView(v); if (v === 'list') resetProvForm(); }}
                                style={{
                                    padding: '6px 14px', fontSize: 12, fontWeight: 600,
                                    background: view === v ? 'var(--accent, #3b82f6)' : 'transparent',
                                    color: view === v ? '#fff' : 'var(--text-tertiary)',
                                    border: 'none', borderRadius: 4, cursor: 'pointer',
                                }}
                            >
                                {v === 'list'
                                    ? t('admin.crossStore.tabList', 'Tenant list')
                                    : t('admin.crossStore.tabProvision', 'Provision new')}
                            </button>
                        ))}
                    </div>
                </div>
            </div>

            {view === 'provision' ? (
                <div style={{ flex: 1, overflowY: 'auto', padding: '32px 24px', maxWidth: 520 }}>
                    {provResult ? (
                        <div style={{ padding: 24, borderRadius: 8, background: 'var(--bg-secondary)', border: '1px solid #22c55e33' }}>
                            <div style={{ fontSize: 16, fontWeight: 600, color: '#22c55e', marginBottom: 8 }}>
                                {t('admin.provision.success', 'Tenant queued for provisioning')}
                            </div>
                            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 4 }}>
                                <strong>{provResult.businessName}</strong>
                            </div>
                            <div style={{ fontFamily: 'monospace', fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 20 }}>
                                {provResult.tenantId}
                            </div>
                            <div style={{ display: 'flex', gap: 10 }}>
                                <button
                                    onClick={() => navigate(`/admin/companies/${provResult.tenantId}`)}
                                    style={{
                                        padding: '8px 16px', borderRadius: 6, border: 'none',
                                        background: 'var(--accent, #3b82f6)', color: '#fff',
                                        fontSize: 13, fontWeight: 600, cursor: 'pointer',
                                    }}
                                >
                                    {t('admin.provision.viewTenant', 'View tenant →')}
                                </button>
                                <button
                                    onClick={resetProvForm}
                                    style={{
                                        padding: '8px 16px', borderRadius: 6,
                                        border: '1px solid var(--border)',
                                        background: 'transparent', color: 'var(--text-secondary)',
                                        fontSize: 13, cursor: 'pointer',
                                    }}
                                >
                                    {t('admin.provision.another', 'Provision another')}
                                </button>
                            </div>
                        </div>
                    ) : (
                        <form onSubmit={handleProvision} style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
                            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                                {t('admin.provision.title', 'Provision new tenant')}
                            </h2>

                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                    {t('admin.provision.businessName', 'Business name')} <span style={{ color: '#ef4444' }}>*</span>
                                </label>
                                <input
                                    type="text"
                                    required
                                    value={provForm.businessName}
                                    onChange={(e) => setProvForm((f) => ({ ...f, businessName: e.target.value }))}
                                    placeholder="Acme Corp"
                                    style={inputStyle}
                                />
                            </div>

                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                    {t('admin.provision.ownerEmail', 'Owner email')} <span style={{ color: '#ef4444' }}>*</span>
                                </label>
                                <input
                                    type="email"
                                    required
                                    value={provForm.ownerEmail}
                                    onChange={(e) => setProvForm((f) => ({ ...f, ownerEmail: e.target.value }))}
                                    placeholder="owner@acmecorp.com"
                                    style={{
                                        ...inputStyle,
                                        borderColor: provForm.ownerEmail && !provEmailValid ? '#ef4444' : undefined,
                                    }}
                                />
                                {provForm.ownerEmail && !provEmailValid && (
                                    <span style={{ fontSize: 11, color: '#ef4444' }}>
                                        {t('admin.provision.invalidEmail', 'Enter a valid email address')}
                                    </span>
                                )}
                            </div>

                            <div style={{ display: 'flex', gap: 12 }}>
                                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
                                    <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                        {t('admin.provision.plan', 'Plan')}
                                    </label>
                                    <select
                                        value={provForm.plan}
                                        onChange={(e) => setProvForm((f) => ({ ...f, plan: e.target.value }))}
                                        style={inputStyle}
                                    >
                                        <option value="base">base</option>
                                        <option value="pro">pro</option>
                                        <option value="enterprise">enterprise</option>
                                    </select>
                                </div>
                                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 6 }}>
                                    <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                        {t('admin.provision.didSource', 'DID source')}
                                    </label>
                                    <select
                                        value={provForm.didSource}
                                        onChange={(e) => setProvForm((f) => ({ ...f, didSource: e.target.value }))}
                                        style={inputStyle}
                                    >
                                        <option value="saga">saga</option>
                                        <option value="manual">manual</option>
                                    </select>
                                </div>
                            </div>

                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                    {t('admin.provision.agentTemplate', 'Agent character')}
                                </label>
                                <select
                                    value={provForm.agentTemplate}
                                    onChange={(e) => setProvForm((f) => ({ ...f, agentTemplate: e.target.value }))}
                                    style={inputStyle}
                                >
                                    {AGENT_CHARACTERS.map((c) => (
                                        <option key={c.value} value={c.value} title={c.tagline} disabled={c.disabled}>
                                            {c.label}{c.disabled ? ' (pack coming soon)' : ''}
                                        </option>
                                    ))}
                                </select>
                                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                                    {AGENT_CHARACTERS.find((c) => c.value === provForm.agentTemplate)?.tagline}
                                </span>
                            </div>

                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-secondary)' }}>
                                    {t('admin.provision.tone', 'Agent tone')}
                                </label>
                                <input
                                    type="text"
                                    value={provForm.tone}
                                    onChange={(e) => setProvForm((f) => ({ ...f, tone: e.target.value }))}
                                    placeholder="direct-curious-helpful"
                                    style={inputStyle}
                                />
                                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                                    {t('admin.provision.toneHint', 'Personality descriptor passed to the agent')}
                                </span>
                            </div>

                            {provError && (
                                <div style={{ padding: '10px 14px', borderRadius: 6, background: '#ef444418', border: '1px solid #ef444433', color: '#ef4444', fontSize: 13 }}>
                                    {provError}
                                </div>
                            )}

                            <button
                                type="submit"
                                disabled={!provReady}
                                style={{
                                    padding: '10px 20px', borderRadius: 6, border: 'none',
                                    background: provReady ? 'var(--accent, #3b82f6)' : 'var(--bg-tertiary, #333)',
                                    color: provReady ? '#fff' : 'var(--text-tertiary)',
                                    fontSize: 14, fontWeight: 600,
                                    cursor: provReady ? 'pointer' : 'not-allowed',
                                    transition: 'background 0.15s',
                                    alignSelf: 'flex-start',
                                }}
                            >
                                {provSubmitting
                                    ? t('admin.provision.submitting', 'Queuing…')
                                    : t('admin.provision.submit', 'Provision tenant')}
                            </button>
                        </form>
                    )}
                </div>
            ) : (
                <>
                    <div style={{ padding: '12px 24px', display: 'flex', gap: 12, alignItems: 'center', borderBottom: '1px solid var(--border)' }}>
                        <input
                            type="text"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            placeholder={t('admin.crossStore.search', 'Search business name…')}
                            style={{ flex: '0 0 280px', padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
                        />
                        <select
                            value={statusFilter}
                            onChange={(e) => setStatusFilter(e.target.value)}
                            style={{ padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-secondary)', color: 'var(--text-primary)' }}
                        >
                            <option value="all">{t('admin.crossStore.statusAll', 'All statuses')}</option>
                            <option value="active">active</option>
                            <option value="provisioning">provisioning</option>
                            <option value="test">test</option>
                            <option value="archived">archived</option>
                            <option value="failed">failed</option>
                        </select>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                            <input
                                type="checkbox"
                                checked={showTest}
                                onChange={(e) => setShowTest(e.target.checked)}
                            />
                            {t('admin.crossStore.showTest', 'Show test/archived tenants')}
                        </label>
                        {/* S7 Phase 2 R39: Active/Retired/All chip filter (Clawith retired_at). */}
                        <div style={{ display: 'flex', gap: 4, padding: 2, background: 'var(--bg-tertiary, #1a1a1a)', borderRadius: 6 }}>
                            {(['active', 'retired', 'all'] as const).map((opt) => (
                                <button
                                    key={opt}
                                    onClick={() => setRetiredFilter(opt)}
                                    style={{
                                        padding: '4px 10px', fontSize: 11, fontWeight: 600,
                                        background: retiredFilter === opt ? 'var(--accent, #3b82f6)' : 'transparent',
                                        color: retiredFilter === opt ? '#fff' : 'var(--text-tertiary)',
                                        border: 'none', borderRadius: 4, cursor: 'pointer',
                                    }}
                                >
                                    {opt === 'active'
                                        ? t('admin.tenants.activeFilter', 'Active')
                                        : opt === 'retired'
                                        ? t('admin.tenants.retiredFilter', 'Retired')
                                        : t('admin.tenants.allFilter', 'All')}
                                </button>
                            ))}
                        </div>
                        <span style={{ color: 'var(--text-tertiary)', fontSize: 12, marginLeft: 'auto' }}>
                            {filtered.length} / {rows.length}
                        </span>
                    </div>

                    <div style={{ flex: 1, overflowY: 'auto', padding: '0 24px 24px' }}>
                        {loading && <div style={{ padding: 40, color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</div>}
                        {err && <div style={{ padding: 40, color: '#ef4444' }}>{err}</div>}
                        {!loading && !err && (
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                                <thead>
                                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.name', 'Business name')}
                                        </th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.status', 'Status')}
                                        </th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.wa', 'WhatsApp')}
                                        </th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.paperclip', 'Paperclip ID')}
                                        </th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.clawith', 'Clawith ID')}
                                        </th>
                                        <th style={{ textAlign: 'right', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.agents', 'Agents')}
                                        </th>
                                        <th style={{ textAlign: 'right', padding: '10px 8px', fontWeight: 600, color: 'var(--text-tertiary)' }}>
                                            {t('admin.crossStore.col.deferred', 'Pending')}
                                        </th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {filtered.map((r) => {
                                        const def = deferredCount(r.bff.deferredOperatorActions);
                                        const defColor = def === 0 ? 'var(--text-tertiary)' : def < 3 ? '#eab308' : '#ef4444';
                                        return (
                                            <tr
                                                key={r.bff.tenantId}
                                                onClick={() => navigate(`/admin/companies/${r.bff.tenantId}`)}
                                                style={{ borderBottom: '1px solid var(--border)', cursor: 'pointer' }}
                                            >
                                                <td style={{ padding: '10px 8px' }}>
                                                    <div style={{ fontWeight: 500 }}>{r.bff.businessName || <span style={{ color: 'var(--text-tertiary)' }}>—</span>}</div>
                                                    <div style={{ fontSize: 11, color: 'var(--text-tertiary)', fontFamily: 'monospace' }}>{r.bff.tenantId.slice(0, 13)}…</div>
                                                </td>
                                                <td style={{ padding: '10px 8px' }}><StatusPill value={r.bff.status} kind="status" /></td>
                                                <td style={{ padding: '10px 8px' }}><StatusPill value={r.bff.whatsappStatus} kind="wa" /></td>
                                                <td style={{ padding: '10px 8px', fontFamily: 'monospace', fontSize: 11 }}>
                                                    {r.bff.paperclipCompanyId ? (
                                                        <span onClick={(e) => e.stopPropagation()}>
                                                            {r.bff.paperclipCompanyId.slice(0, 8)}…
                                                            <LinearCopyButton textToCopy={r.bff.paperclipCompanyId} iconOnly />
                                                        </span>
                                                    ) : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
                                                </td>
                                                <td style={{ padding: '10px 8px', fontFamily: 'monospace', fontSize: 11 }}>
                                                    {r.bff.clawithTenantId ? (
                                                        <span onClick={(e) => e.stopPropagation()}>
                                                            {r.bff.clawithTenantId.slice(0, 8)}…
                                                            <LinearCopyButton textToCopy={r.bff.clawithTenantId} iconOnly />
                                                        </span>
                                                    ) : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
                                                </td>
                                                <td style={{ padding: '10px 8px', textAlign: 'right', color: 'var(--text-tertiary)' }}>
                                                    {r.local?.agent_count ?? '—'}
                                                </td>
                                                <td style={{ padding: '10px 8px', textAlign: 'right', color: defColor, fontWeight: 600 }}>
                                                    {def}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        )}
                    </div>
                </>
            )}
        </div>
    );
}

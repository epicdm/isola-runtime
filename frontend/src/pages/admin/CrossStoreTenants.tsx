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
    local: { id: string; name: string; agent_count?: number } | null;
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
            return true;
        });
    }, [rows, statusFilter, search]);

    if (user?.role !== 'platform_admin') {
        return (
            <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                {t('common.noPermission', 'You do not have permission to access this page.')}
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)' }}>
            <div className="page-header">
                <div>
                    <h1 className="page-title">
                        {t('admin.crossStore.title', 'Cross-store tenants')}
                    </h1>
                    <p className="page-subtitle">
                        {t('admin.crossStore.subtitle', 'All tenants across BFF, Paperclip, and Clawith with their cross-namespace IDs and deferred operator actions.')}
                    </p>
                </div>
            </div>

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
        </div>
    );
}

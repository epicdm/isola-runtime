import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate, useParams } from 'react-router-dom';
import { crossStoreApi } from '../../services/api';
import { useAuthStore } from '../../stores';
import LinearCopyButton from '../../components/LinearCopyButton';

// L4 S2: per-tenant detail. Bearer + platform_admin. Calls
// /api/admin/cross-store/tenants/{tenantId}. Read-only in S2; CRUD/actions
// land in S3 per Q-S3 ratification.

type DetailResponse = {
    bff: Record<string, any>;
    local: Record<string, any> | null;
    agents: Array<Record<string, any>>;
};

type Tab = 'overview' | 'agents' | 'channels' | 'deferred' | 'audit';

function IdRow({ label, value }: { label: string; value: string | null | undefined }) {
    return (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
            <div style={{ width: 200, color: 'var(--text-tertiary)', fontSize: 12 }}>{label}</div>
            <div style={{ flex: 1, fontFamily: 'monospace', fontSize: 12 }}>
                {value ? value : <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
            </div>
            {value && <LinearCopyButton textToCopy={value} iconOnly />}
        </div>
    );
}

function StatusPill({ value }: { value: string | undefined }) {
    if (!value) return null;
    const palette: Record<string, string> = {
        active: '#22c55e', test: '#94a3b8', archived: '#64748b',
        provisioning: '#eab308', failed: '#ef4444', not_connected: '#94a3b8',
        allocating: '#eab308', pending_otp: '#3b82f6',
    };
    const color = palette[value] || '#94a3b8';
    return (
        <span style={{
            display: 'inline-block', padding: '2px 8px', borderRadius: 12,
            fontSize: 11, fontWeight: 600, color, background: `${color}1a`, border: `1px solid ${color}33`,
        }}>{value}</span>
    );
}

export default function CrossStoreTenantDetail() {
    const { t } = useTranslation();
    const { tenantId } = useParams<{ tenantId: string }>();
    const navigate = useNavigate();
    const user = useAuthStore((s) => s.user);

    const [data, setData] = useState<DetailResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState<string | null>(null);
    const [tab, setTab] = useState<Tab>('overview');

    useEffect(() => {
        if (!tenantId) return;
        crossStoreApi.getTenant(tenantId)
            .then(setData)
            .catch((e: Error) => setErr(e.message))
            .finally(() => setLoading(false));
    }, [tenantId]);

    if (user?.role !== 'platform_admin') {
        return (
            <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                {t('common.noPermission', 'You do not have permission to access this page.')}
            </div>
        );
    }

    if (loading) return <div style={{ padding: 40, color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</div>;
    if (err) return <div style={{ padding: 40, color: '#ef4444' }}>{err}</div>;
    if (!data) return null;

    const { bff, local, agents } = data;
    const tabs: { key: Tab; label: string }[] = [
        { key: 'overview', label: t('admin.crossStore.detail.tab.overview', 'Overview') },
        { key: 'agents', label: t('admin.crossStore.detail.tab.agents', `Agents (${agents.length})`) },
        { key: 'channels', label: t('admin.crossStore.detail.tab.channels', 'Channels') },
        { key: 'deferred', label: t('admin.crossStore.detail.tab.deferred', 'Deferred actions') },
        { key: 'audit', label: t('admin.crossStore.detail.tab.audit', 'Audit log') },
    ];

    return (
        <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)' }}>
            <div className="page-header">
                <div>
                    <button
                        onClick={() => navigate('/admin/companies')}
                        style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer', padding: 0, marginBottom: 8, fontSize: 13 }}
                    >
                        ← {t('admin.crossStore.detail.back', 'Back to all tenants')}
                    </button>
                    <h1 className="page-title" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        {bff.businessName || <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
                        <StatusPill value={bff.status} />
                        <StatusPill value={bff.whatsappStatus} />
                    </h1>
                    <p className="page-subtitle">
                        {t('admin.crossStore.detail.subtitle', 'BFF tenant_registry + Clawith local view')}
                    </p>
                </div>
            </div>

            <div className="tabs">
                {tabs.map((tabDef) => (
                    <div
                        key={tabDef.key}
                        className={`tab ${tab === tabDef.key ? 'active' : ''}`}
                        onClick={() => setTab(tabDef.key)}
                    >
                        {tabDef.label}
                    </div>
                ))}
            </div>

            <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px' }}>
                {tab === 'overview' && (
                    <div style={{ maxWidth: 720 }}>
                        <h3 style={{ marginTop: 0 }}>{t('admin.crossStore.detail.overview.ids', 'Cross-namespace IDs')}</h3>
                        <IdRow label={t('admin.crossStore.detail.overview.bffId', 'BFF tenantId')} value={bff.tenantId} />
                        <IdRow label={t('admin.crossStore.detail.overview.paperclip', 'Paperclip companyId')} value={bff.paperclipCompanyId} />
                        <IdRow label={t('admin.crossStore.detail.overview.clawith', 'Clawith tenantId')} value={bff.clawithTenantId} />
                        <IdRow label={t('admin.crossStore.detail.overview.clawithAgent', 'Clawith agentId')} value={bff.clawithAgentId} />
                        <IdRow label={t('admin.crossStore.detail.overview.shell', 'apps/isola tenantId (shell)')} value={bff.shellTenantId} />
                        <IdRow label={t('admin.crossStore.detail.overview.waPhoneId', 'Meta phone_number_id')} value={bff.waPhoneNumberId} />
                        <IdRow label={t('admin.crossStore.detail.overview.waba', 'WABA id')} value={bff.wabaId} />

                        <h3 style={{ marginTop: 24 }}>{t('admin.crossStore.detail.overview.runtime', 'Runtime')}</h3>
                        <IdRow label={t('admin.crossStore.detail.overview.localId', 'Clawith local id')} value={local?.id ?? null} />
                        <IdRow label={t('admin.crossStore.detail.overview.localName', 'Local name')} value={local?.name ?? null} />
                        <IdRow label={t('admin.crossStore.detail.overview.localSlug', 'Local slug')} value={local?.slug ?? null} />
                        <IdRow label={t('admin.crossStore.detail.overview.runtimeMode', 'Runtime mode')} value={local?.runtime_mode ?? null} />
                        <IdRow label={t('admin.crossStore.detail.overview.containerPort', 'Container port')} value={bff.containerPort != null ? String(bff.containerPort) : null} />

                        <h3 style={{ marginTop: 24 }}>{t('admin.crossStore.detail.overview.owner', 'Owner & plan')}</h3>
                        <IdRow label={t('admin.crossStore.detail.overview.ownerEmail', 'Owner email')} value={bff.ownerEmail} />
                        <IdRow label={t('admin.crossStore.detail.overview.plan', 'Plan')} value={bff.planName ?? bff.template} />
                        <IdRow label={t('admin.crossStore.detail.overview.created', 'Created at')} value={bff.createdAt} />
                        <IdRow label={t('admin.crossStore.detail.overview.updated', 'Updated at')} value={bff.updatedAt} />
                    </div>
                )}

                {tab === 'agents' && (
                    <div>
                        {agents.length === 0 ? (
                            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                                {t('admin.crossStore.detail.agents.empty', 'No agents in Clawith local DB.')}
                            </div>
                        ) : (
                            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                                <thead>
                                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', color: 'var(--text-tertiary)' }}>{t('admin.crossStore.detail.agents.col.name', 'Name')}</th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', color: 'var(--text-tertiary)' }}>{t('admin.crossStore.detail.agents.col.type', 'Type')}</th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', color: 'var(--text-tertiary)' }}>{t('admin.crossStore.detail.agents.col.role', 'Role')}</th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', color: 'var(--text-tertiary)' }}>{t('admin.crossStore.detail.agents.col.owner', 'Owner phone')}</th>
                                        <th style={{ textAlign: 'left', padding: '10px 8px', color: 'var(--text-tertiary)' }}>{t('admin.crossStore.detail.agents.col.paperclip', 'Paperclip agent id')}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {agents.map((a) => (
                                        <tr key={a.id} style={{ borderBottom: '1px solid var(--border)' }}>
                                            <td style={{ padding: '10px 8px', fontWeight: 500 }}>{a.name}</td>
                                            <td style={{ padding: '10px 8px' }}>{a.agent_type}</td>
                                            <td style={{ padding: '10px 8px', color: 'var(--text-tertiary)' }}>{a.role_description || '—'}</td>
                                            <td style={{ padding: '10px 8px', fontFamily: 'monospace', fontSize: 11 }}>{a.owner_phone || '—'}</td>
                                            <td style={{ padding: '10px 8px', fontFamily: 'monospace', fontSize: 11 }}>{a.paperclip_agent_id || '—'}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                )}

                {tab === 'channels' && (
                    <div style={{ maxWidth: 720 }}>
                        <h3 style={{ marginTop: 0 }}>WhatsApp</h3>
                        <IdRow label="status" value={bff.whatsappStatus} />
                        <IdRow label="phone_number_id" value={bff.waPhoneNumberId} />
                        <IdRow label="WABA id" value={bff.wabaId} />
                        <IdRow label="display phone" value={bff.displayPhone} />
                        <IdRow label="activated at" value={bff.whatsappActivatedAt} />

                        <h3 style={{ marginTop: 24 }}>Chatwoot inbox</h3>
                        <IdRow label="status" value={bff.chatwootStatus} />
                        <IdRow label="account id" value={bff.chatwootAccountId} />
                        <IdRow label="provisioned at" value={bff.chatwootProvisionedAt} />
                        <IdRow label="last error" value={bff.chatwootProvisionError} />
                    </div>
                )}

                {tab === 'deferred' && (
                    <div style={{ maxWidth: 720 }}>
                        {bff.deferredOperatorActions ? (
                            <pre style={{
                                background: 'var(--bg-secondary)', border: '1px solid var(--border)',
                                borderRadius: 6, padding: 16, fontSize: 12, lineHeight: 1.5, overflow: 'auto',
                            }}>{JSON.stringify(bff.deferredOperatorActions, null, 2)}</pre>
                        ) : (
                            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                                {t('admin.crossStore.detail.deferred.empty', 'No deferred operator actions.')}
                            </div>
                        )}
                    </div>
                )}

                {tab === 'audit' && (
                    <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                        {t('admin.crossStore.detail.audit.stub', 'Audit log lands in S3 (Inngest events for this tenantId).')}
                    </div>
                )}
            </div>
        </div>
    );
}

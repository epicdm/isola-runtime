import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { crossStoreApi, type QueueItem } from '../services/api';

// L4 S3 shared cell: renders one deferredOperatorAction item with the
// per-kind action button. Used by /admin/operator-queue (grouped) and the
// /admin/companies/:id Deferred Actions tab (filtered to a tenant).

type Props = {
    item: QueueItem;
    showTenantLink?: boolean;
    onResolved?: () => void;
};

const KIND_META: Record<string, { label: string; cta: string | null; description: string }> = {
    pendingOwnerInvitation: {
        label: 'Pending owner invitation',
        cta: 'Mark invited',
        description: 'Owner email captured during signup; operator sends the invite from Paperclip then marks complete.',
    },
    tokenScopingDeferred: {
        label: 'Token scoping deferred',
        cta: 'Mark scoped',
        description: 'Per-tenant token mint not yet implemented; tenant runs on shared scope until Wave-2.',
    },
    pendingChannelBinding: {
        label: 'Pending channel binding',
        cta: null,
        description: 'Auto-resolves on Phase 2 saga success. If stuck >5 min, investigate Inngest dashboard.',
    },
    pendingAppsIsolaStatusSync: {
        label: 'Pending apps/isola status sync',
        cta: 'Mark synced',
        description: 'apps/isola.tenants.status writeback deferred from saga; mark resolved after manual sync or L4 sync endpoint.',
    },
};

export default function OperatorActionItem({ item, showTenantLink = true, onResolved }: Props) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState<string | null>(null);
    const [confirming, setConfirming] = useState(false);

    const meta = KIND_META[item.kind] || { label: item.kind, cta: 'Mark resolved', description: '' };

    const since = (() => {
        if (!item.payload || typeof item.payload !== 'object') return null;
        const s = (item.payload as Record<string, unknown>).since;
        return typeof s === 'string' ? s : null;
    })();

    const ageStr = (() => {
        if (!since) return null;
        const ms = Date.now() - new Date(since).getTime();
        if (Number.isNaN(ms)) return null;
        const h = Math.floor(ms / 3_600_000);
        const d = Math.floor(h / 24);
        if (d > 0) return `${d}d`;
        if (h > 0) return `${h}h`;
        const m = Math.floor(ms / 60_000);
        return `${m}m`;
    })();

    const onConfirm = async () => {
        setBusy(true);
        setErr(null);
        try {
            await crossStoreApi.resolveAction(item.tenantId, item.kind);
            onResolved?.();
        } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
            setConfirming(false);
        }
    };

    return (
        <div style={{
            border: '1px solid var(--border)', borderRadius: 8, padding: 14,
            display: 'flex', flexDirection: 'column', gap: 8, background: 'var(--bg-secondary)',
        }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ fontWeight: 600 }}>{t(`admin.operatorQueue.kind.${item.kind}.label`, meta.label)}</div>
                {ageStr && (
                    <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                        {t('admin.operatorQueue.age', 'open for {{age}}', { age: ageStr })}
                    </span>
                )}
                {showTenantLink && (
                    <button
                        onClick={() => navigate(`/admin/companies/${item.tenantId}`)}
                        style={{
                            marginLeft: 'auto', background: 'none', border: 'none',
                            color: 'var(--accent, #3b82f6)', cursor: 'pointer', fontSize: 13, padding: 0,
                        }}
                    >
                        {item.businessName || item.tenantId.slice(0, 8) + '…'} →
                    </button>
                )}
            </div>

            <div style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>
                {t(`admin.operatorQueue.kind.${item.kind}.description`, meta.description)}
            </div>

            {item.payload && typeof item.payload === 'object' ? (
                <pre style={{
                    margin: 0, padding: 10, background: 'var(--bg-primary, #0b0b0b)',
                    borderRadius: 6, fontSize: 11, lineHeight: 1.4, overflow: 'auto',
                    color: 'var(--text-secondary)',
                }}>{JSON.stringify(item.payload, null, 2)}</pre>
            ) : null}

            {err && <div style={{ color: '#ef4444', fontSize: 12 }}>{err}</div>}

            {meta.cta && (
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                    {confirming ? (
                        <>
                            <button
                                onClick={() => setConfirming(false)}
                                disabled={busy}
                                style={{ padding: '6px 12px', background: 'none', border: '1px solid var(--border)', borderRadius: 6, cursor: 'pointer', color: 'var(--text-primary)' }}
                            >
                                {t('common.cancel', 'Cancel')}
                            </button>
                            <button
                                onClick={onConfirm}
                                disabled={busy}
                                style={{ padding: '6px 12px', background: '#22c55e', border: 'none', borderRadius: 6, cursor: 'pointer', color: '#fff', fontWeight: 600 }}
                            >
                                {busy ? t('common.saving', 'Working…') : t('admin.operatorQueue.confirm', 'Confirm')}
                            </button>
                        </>
                    ) : (
                        <button
                            onClick={() => setConfirming(true)}
                            style={{ padding: '6px 12px', background: 'var(--accent, #3b82f6)', border: 'none', borderRadius: 6, cursor: 'pointer', color: '#fff', fontWeight: 600, fontSize: 13 }}
                        >
                            {t(`admin.operatorQueue.kind.${item.kind}.cta`, meta.cta)}
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

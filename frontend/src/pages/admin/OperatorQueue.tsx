import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { crossStoreApi, type QueueItem } from '../../services/api';
import { useAuthStore } from '../../stores';
import OperatorActionItem from '../../components/OperatorActionItem';

// L4 S3: operator daily-work surface. Lists deferredOperatorActions across
// all tenants, grouped by actionKind. Bearer + platform_admin.

export default function OperatorQueue() {
    const { t } = useTranslation();
    const user = useAuthStore((s) => s.user);

    const [items, setItems] = useState<QueueItem[]>([]);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState<string | null>(null);

    const refetch = () => {
        setLoading(true);
        crossStoreApi.listQueue()
            .then((d) => setItems(d.items || []))
            .catch((e: Error) => setErr(e.message))
            .finally(() => setLoading(false));
    };

    useEffect(refetch, []);

    const groups = useMemo(() => {
        const g: Record<string, QueueItem[]> = {};
        for (const it of items) {
            g[it.kind] = g[it.kind] || [];
            g[it.kind].push(it);
        }
        return g;
    }, [items]);

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
                        {t('admin.operatorQueue.title', 'Operator queue')}
                    </h1>
                    <p className="page-subtitle">
                        {t('admin.operatorQueue.subtitle', 'Pending deferred actions across all tenants. Resolve from here or from per-tenant detail.')}
                    </p>
                </div>
                <div style={{ marginLeft: 'auto', alignSelf: 'center', fontSize: 13, color: 'var(--text-tertiary)' }}>
                    {t('admin.operatorQueue.total', '{{n}} pending', { n: items.length })}
                </div>
            </div>

            <div style={{ flex: 1, overflowY: 'auto', padding: '16px 24px' }}>
                {loading && <div style={{ color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading…')}</div>}
                {err && <div style={{ color: '#ef4444' }}>{err}</div>}
                {!loading && !err && items.length === 0 && (
                    <div style={{ padding: 60, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                        {t('admin.operatorQueue.empty', 'No pending operator actions. The queue is clean.')}
                    </div>
                )}
                {!loading && !err && items.length > 0 && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 28 }}>
                        {Object.entries(groups).map(([kind, list]) => (
                            <section key={kind}>
                                <h3 style={{ marginTop: 0, marginBottom: 12, fontSize: 14, color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                                    {t(`admin.operatorQueue.kind.${kind}.label`, kind)} ({list.length})
                                </h3>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                                    {list.map((it, i) => (
                                        <OperatorActionItem
                                            key={`${it.tenantId}-${it.kind}-${i}`}
                                            item={it}
                                            showTenantLink
                                            onResolved={refetch}
                                        />
                                    ))}
                                </div>
                            </section>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

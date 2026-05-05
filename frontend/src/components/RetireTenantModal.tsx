/**
 * RetireTenantModal — operator surface for cross-store tenant retire (R42).
 *
 * Calls DELETE /api/admin/cross-store/tenants/{tid}, which orchestrates:
 *   1. Paperclip POST /:cid/archive       (memory: archive not DELETE)
 *   2. Clawith soft-retire                (sets tenants.retired_at NOW)
 *   3. BFF NULL clawithTenantId           (preserves billing/audit row)
 *   4. AuditLog platform_admin.tenant_retire
 *
 * Modal pattern mirrors S5 SOULEditorModal (full-screen overlay + click-
 * outside close + inline status + CSS variables).
 *
 * On 200: success state renders succeeded_steps trail with checkmarks.
 * On 409 partial_state: render step-by-step (succeeded ✓ / failed ✗ /
 *   pending ↻) + Retry button (R42 idempotent retry).
 *
 * R36 reminder: this is a destructive-looking action — operator MUST see
 * the IDs they're about to retire (BFF tid + Paperclip cid + Clawith tid)
 * BEFORE the Retire button activates.
 */
import { useEffect, useState } from 'react';
import { crossStoreApi, type RetireSuccess, type RetireError } from '../services/api';

interface Props {
    open: boolean;
    tenantId: string;
    bffTenantId: string;
    paperclipCompanyId: string | null;
    clawithTenantId: string | null;
    businessName: string;
    onClose: () => void;
    onRetired?: () => void;
}

type ModalStage = 'confirm' | 'orchestrating' | 'success' | 'partial_state' | 'error';

const ALL_STEPS = [
    { key: 'paperclip_archive', label: 'Paperclip: archive company' },
    { key: 'clawith_retire', label: 'Clawith: soft-retire tenant' },
    { key: 'bff_unlink_clawith', label: 'BFF: NULL clawithTenantId' },
] as const;

// Backend can return alternate marker values per step (see admin_crossstore.py
// orchestrator). Map them to the canonical step name for UI rendering.
function canonicalStep(s: string): string {
    if (s.startsWith('paperclip')) return 'paperclip_archive';
    if (s.startsWith('clawith')) return 'clawith_retire';
    if (s.startsWith('bff_unlink')) return 'bff_unlink_clawith';
    return s;
}

export default function RetireTenantModal({
    open, tenantId, bffTenantId, paperclipCompanyId, clawithTenantId,
    businessName, onClose, onRetired,
}: Props) {
    const [stage, setStage] = useState<ModalStage>('confirm');
    const [response, setResponse] = useState<RetireSuccess | null>(null);
    const [partial, setPartial] = useState<RetireError | null>(null);
    const [errMsg, setErrMsg] = useState<string | null>(null);

    useEffect(() => {
        if (open) {
            setStage('confirm');
            setResponse(null);
            setPartial(null);
            setErrMsg(null);
        }
    }, [open]);

    if (!open) return null;

    const handleRetire = async () => {
        setStage('orchestrating');
        setPartial(null);
        setErrMsg(null);
        try {
            const r = await crossStoreApi.retireCrossStoreTenant(tenantId);
            setResponse(r);
            setStage('success');
            onRetired?.();
        } catch (e) {
            const status = (e as any)?.status;
            const detail = (e as any)?.detail;
            if (status === 409 && detail && typeof detail === 'object' && detail.error === 'partial_state') {
                setPartial(detail as RetireError);
                setStage('partial_state');
            } else {
                setErrMsg(`Retire failed: ${(e as Error).message || String(e)}`);
                setStage('error');
            }
        }
    };

    const handleRetry = () => {
        // Idempotent retry per R42; orchestrator no-ops succeeded steps.
        handleRetire();
    };

    const renderStepRow = (stepKey: string, label: string) => {
        let icon = '↻';
        let color = 'var(--text-tertiary)';
        if (stage === 'success' && response) {
            const completed = response.succeeded_steps.some((s) => canonicalStep(s) === stepKey);
            if (completed) { icon = '✓'; color = 'var(--success, #22c55e)'; }
        } else if (stage === 'partial_state' && partial) {
            const completed = partial.succeeded.some((s) => canonicalStep(s) === stepKey);
            const failedHere = canonicalStep(partial.failed) === stepKey;
            if (completed) { icon = '✓'; color = 'var(--success, #22c55e)'; }
            else if (failedHere) { icon = '✗'; color = 'var(--danger, #d05050)'; }
        }
        return (
            <div key={stepKey} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0', fontSize: 12 }}>
                <span style={{ color, fontFamily: 'monospace', width: 16, textAlign: 'center' }}>{icon}</span>
                <span style={{ color: stage === 'orchestrating' ? 'var(--text-secondary)' : 'var(--text-primary)' }}>{label}</span>
            </div>
        );
    };

    return (
        <div
            style={{
                position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 10000,
            }}
            onClick={(e) => { if (e.target === e.currentTarget && stage !== 'orchestrating') onClose(); }}
        >
            <div
                style={{
                    background: 'var(--bg-primary)', borderRadius: 12, padding: 24,
                    width: 580, maxWidth: '94vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                    border: '1px solid var(--border-subtle)', boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
                    overflowY: 'auto',
                }}
            >
                <div style={{ marginBottom: 12 }}>
                    <h4 style={{ fontSize: 15, margin: 0, color: 'var(--danger, #d05050)' }}>
                        Retire tenant — {businessName}
                    </h4>
                    <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 2 }}>
                        Cross-store soft-retire (Paperclip → Clawith → BFF) per R42
                    </div>
                </div>

                {stage === 'confirm' && (
                    <>
                        <div style={{
                            padding: '10px 12px', fontSize: 12, lineHeight: 1.5,
                            background: 'rgba(208,80,80,0.08)', color: 'var(--text-secondary)',
                            borderRadius: 6, border: '1px solid rgba(208,80,80,0.30)', marginBottom: 14,
                        }}>
                            <strong>Retiring this tenant will:</strong>
                            <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                                <li>Archive Paperclip company (if linked)</li>
                                <li>Soft-retire Clawith tenant (sets <code>retired_at</code>)</li>
                                <li>Unlink BFF FK to Clawith</li>
                                <li>Write <code>platform_admin.tenant_retire</code> audit row</li>
                            </ul>
                            <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-tertiary)' }}>
                                The customer record (BFF tenant_registry row) is preserved for billing/audit.
                                Action is reversible only by manual SQL.
                            </div>
                        </div>

                        <div style={{ marginBottom: 14, fontSize: 11 }}>
                            <div style={{ color: 'var(--text-tertiary)', marginBottom: 4 }}>Verify identifiers:</div>
                            <div style={{ fontFamily: 'monospace', lineHeight: 1.7 }}>
                                <div>BFF tenant_id&nbsp;&nbsp;&nbsp;&nbsp;{bffTenantId}</div>
                                <div>Paperclip cid&nbsp;&nbsp;&nbsp;&nbsp;{paperclipCompanyId ?? <span style={{ color: 'var(--text-tertiary)' }}>(none — skip step 1)</span>}</div>
                                <div>Clawith tenant_id&nbsp;{clawithTenantId ?? <span style={{ color: 'var(--text-tertiary)' }}>(none)</span>}</div>
                            </div>
                        </div>

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
                            <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
                            <button
                                onClick={handleRetire}
                                style={{
                                    padding: '6px 14px', background: '#ef4444',
                                    border: 'none', borderRadius: 6, cursor: 'pointer',
                                    color: '#fff', fontSize: 12, fontWeight: 600,
                                }}
                            >
                                Retire tenant
                            </button>
                        </div>
                    </>
                )}

                {stage === 'orchestrating' && (
                    <div>
                        <div style={{ color: 'var(--text-secondary)', fontSize: 12, marginBottom: 12 }}>
                            Orchestrating cross-store retire…
                        </div>
                        {ALL_STEPS.map((s) => renderStepRow(s.key, s.label))}
                    </div>
                )}

                {stage === 'success' && response && (
                    <>
                        <div style={{
                            padding: '10px 12px', fontSize: 12,
                            background: 'rgba(34,197,94,0.10)', color: 'var(--success, #22c55e)',
                            borderRadius: 6, border: '1px solid rgba(34,197,94,0.40)', marginBottom: 14,
                        }}>
                            ✓ Tenant retired successfully across {response.succeeded_steps.length} steps.
                        </div>
                        {ALL_STEPS.map((s) => renderStepRow(s.key, s.label))}
                        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-tertiary)' }}>
                            <em>succeeded_steps:</em> {response.succeeded_steps.join(', ')}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
                            <button className="btn btn-primary" onClick={onClose}>Close</button>
                        </div>
                    </>
                )}

                {stage === 'partial_state' && partial && (
                    <>
                        <div style={{
                            padding: '10px 12px', fontSize: 12,
                            background: 'rgba(234,179,8,0.10)', color: 'var(--text-secondary)',
                            borderRadius: 6, border: '1px solid rgba(234,179,8,0.40)', marginBottom: 14,
                        }}>
                            <strong style={{ color: 'var(--danger, #d05050)' }}>Partial state.</strong> {partial.succeeded.length} step(s)
                            succeeded; failed at <code>{partial.failed}</code>.
                        </div>
                        {ALL_STEPS.map((s) => renderStepRow(s.key, s.label))}
                        <div style={{
                            marginTop: 10, padding: '8px 10px', fontSize: 11,
                            background: 'var(--bg-secondary)', color: 'var(--text-secondary)',
                            border: '1px solid var(--border-subtle)', borderRadius: 6,
                            fontFamily: 'monospace', maxHeight: 120, overflowY: 'auto',
                        }}>
                            {partial.details ?? '(no details)'}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
                            <button className="btn btn-secondary" onClick={onClose}>Close</button>
                            <button
                                onClick={handleRetry}
                                style={{
                                    padding: '6px 14px', background: '#ef4444',
                                    border: 'none', borderRadius: 6, cursor: 'pointer',
                                    color: '#fff', fontSize: 12, fontWeight: 600,
                                }}
                            >
                                Retry (idempotent)
                            </button>
                        </div>
                    </>
                )}

                {stage === 'error' && (
                    <>
                        <div style={{
                            padding: '10px 12px', fontSize: 12,
                            background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                            borderRadius: 6, border: '1px solid var(--danger, #d05050)', marginBottom: 14,
                        }}>
                            {errMsg}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
                            <button className="btn btn-secondary" onClick={onClose}>Close</button>
                            <button onClick={handleRetire} className="btn btn-primary">Retry</button>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

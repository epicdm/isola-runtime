/**
 * SkillsAttachmentPanel — operator surface for attaching/detaching skills to an agent.
 *
 * S5 R18-R25 (2026-05-04):
 *   R18 — ambassador to Paperclip (writes via /admin/cross-store/.../skills)
 *   R19 — platform_admin bypasses L3 autonomy gate; audit row written server-side
 *   R20a — no Clawith write-through (Clawith doesn't cache desiredSkills canonically;
 *          dispatch pulls from Paperclip on next invocation per ADR-0070)
 *   R23 — last-write-wins; no etag/revision guard at Wave-1
 *   R25 — 409 'agent_not_paperclip_managed' if agent lacks paperclip_*_id
 *
 * Confirm-modal pattern from S3 OperatorActionItem (mutation requires explicit confirm).
 */
import { useEffect, useState } from 'react';
import { crossStoreApi, type SkillCatalogEntry } from '../services/api';
import ConfirmModal from './ConfirmModal';

interface Props {
    open: boolean;
    tenantId: string;
    agentId: string;
    agentName: string;
    onClose: () => void;
    onSaved?: () => void;
}

export default function SkillsAttachmentPanel({ open, tenantId, agentId, agentName, onClose, onSaved }: Props) {
    const [available, setAvailable] = useState<SkillCatalogEntry[]>([]);
    const [attached, setAttached] = useState<string[]>([]);
    const [loading, setLoading] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [pendingToggle, setPendingToggle] = useState<{ key: string; nextAttached: boolean } | null>(null);

    const refetch = () => {
        setLoading(true);
        setError(null);
        return crossStoreApi
            .getAgentSkills(tenantId, agentId)
            .then((r) => { setAvailable(r.available || []); setAttached(r.attached || []); })
            .catch((e) => {
                const status = (e as any)?.status;
                if (status === 409) {
                    setError("This agent isn't Paperclip-managed and can't have its skills edited via this surface. Contact operator support.");
                } else {
                    setError(`Failed to load skills: ${(e as Error).message || String(e)}`);
                }
            })
            .finally(() => setLoading(false));
    };

    useEffect(() => { if (open) refetch(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [open, tenantId, agentId]);

    if (!open) return null;

    const requestToggle = (entry: SkillCatalogEntry) => {
        const isAttached = attached.includes(entry.key);
        setPendingToggle({ key: entry.key, nextAttached: !isAttached });
    };

    const confirmToggle = async () => {
        if (!pendingToggle) return;
        const next = pendingToggle.nextAttached
            ? Array.from(new Set([...attached, pendingToggle.key]))
            : attached.filter((k) => k !== pendingToggle.key);
        setBusy(true);
        setError(null);
        try {
            const r = await crossStoreApi.putAgentSkills(tenantId, agentId, next);
            setAttached(r.desired || next);
            onSaved?.();
        } catch (e) {
            const status = (e as any)?.status;
            if (status === 422) {
                setError(`Catalog rejected the change: ${(e as Error).message || 'unknown skill key'}`);
                refetch();
            } else if (status === 409) {
                setError("Agent is not Paperclip-managed; cannot save.");
            } else if (status === 502) {
                setError("Paperclip unreachable; try again in a moment.");
            } else {
                setError(`Save failed: ${(e as Error).message || String(e)}`);
            }
        } finally {
            setBusy(false);
            setPendingToggle(null);
        }
    };

    const attachedSet = new Set(attached);
    const customerFacing = available.filter((s) => !s.key.startsWith('paperclipai/'));

    return (
        <>
            <div
                style={{
                    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                    background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    zIndex: 9999,
                }}
                onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
            >
                <div
                    style={{
                        background: 'var(--bg-primary)', borderRadius: 12, padding: 24,
                        width: 760, maxWidth: '94vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                        border: '1px solid var(--border-subtle)', boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
                    }}
                >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 }}>
                        <h4 style={{ fontSize: 15, margin: 0 }}>Manage Skills — {agentName}</h4>
                        <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                            Source: Paperclip / company catalog · Bundled paperclipai/* hidden
                        </span>
                    </div>

                    {loading ? (
                        <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>Loading…</div>
                    ) : (
                        <div style={{ flex: 1, overflowY: 'auto', border: '1px solid var(--border-subtle)', borderRadius: 8 }}>
                            {customerFacing.length === 0 ? (
                                <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                                    No customer-facing skills in this company's catalog.
                                </div>
                            ) : (
                                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                                    <thead style={{ position: 'sticky', top: 0, background: 'var(--bg-secondary)' }}>
                                        <tr style={{ borderBottom: '1px solid var(--border)' }}>
                                            <th style={{ textAlign: 'left', padding: '10px 12px', color: 'var(--text-tertiary)', fontWeight: 500 }}>Skill</th>
                                            <th style={{ textAlign: 'left', padding: '10px 12px', color: 'var(--text-tertiary)', fontWeight: 500 }}>Description</th>
                                            <th style={{ textAlign: 'right', padding: '10px 12px', color: 'var(--text-tertiary)', fontWeight: 500, width: 110 }}>Attached</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {customerFacing.map((s) => {
                                            const isAttached = attachedSet.has(s.key);
                                            return (
                                                <tr key={s.key} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                                                    <td style={{ padding: '10px 12px', fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>{s.slug || s.key}</td>
                                                    <td style={{ padding: '10px 12px', color: 'var(--text-secondary)' }}>{s.description || '—'}</td>
                                                    <td style={{ padding: '10px 12px', textAlign: 'right' }}>
                                                        <button
                                                            className={isAttached ? 'btn btn-secondary' : 'btn btn-primary'}
                                                            style={{ fontSize: 11, padding: '4px 10px' }}
                                                            disabled={busy}
                                                            onClick={() => requestToggle(s)}
                                                        >
                                                            {isAttached ? 'Detach' : 'Attach'}
                                                        </button>
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            )}
                        </div>
                    )}

                    {error && (
                        <div style={{
                            marginTop: 12, padding: 10, fontSize: 12,
                            background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                            borderRadius: 6, border: '1px solid var(--danger, #d05050)',
                        }}>
                            {error}
                        </div>
                    )}

                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
                        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>Close</button>
                    </div>
                </div>
            </div>

            {pendingToggle && (
                <ConfirmModal
                    open={!!pendingToggle}
                    title={pendingToggle.nextAttached ? 'Attach skill?' : 'Detach skill?'}
                    message={`${pendingToggle.nextAttached ? 'Attach' : 'Detach'} "${pendingToggle.key}" ${pendingToggle.nextAttached ? 'to' : 'from'} ${agentName}? Takes effect on next message.`}
                    confirmLabel={pendingToggle.nextAttached ? 'Attach' : 'Detach'}
                    cancelLabel="Cancel"
                    danger={!pendingToggle.nextAttached}
                    onConfirm={confirmToggle}
                    onCancel={() => setPendingToggle(null)}
                />
            )}
        </>
    );
}

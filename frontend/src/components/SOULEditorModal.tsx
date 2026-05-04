/**
 * SOULEditorModal — operator surface for editing an agent's SOUL.md persona.
 *
 * S5 R18-R25 (2026-05-04):
 *   R18 — ambassador to Paperclip (writes via /admin/cross-store/.../soul)
 *   R19 — platform_admin bypasses L3 autonomy gate; audit row written server-side
 *   R20a — write-through: server updates Clawith Agent.soul cache from request body
 *   R21 — plain <textarea> + char count (no editor library)
 *   R23 — last-write-wins; no etag/revision guard at Wave-1
 *   R25 — 409 'agent_not_paperclip_managed' if agent lacks paperclip_*_id
 */
import { useEffect, useState } from 'react';
import { crossStoreApi } from '../services/api';

const MAX_LEN = 50000;

interface Props {
    open: boolean;
    tenantId: string;
    agentId: string;
    agentName: string;
    onClose: () => void;
    onSaved?: () => void;
}

export default function SOULEditorModal({ open, tenantId, agentId, agentName, onClose, onSaved }: Props) {
    const [content, setContent] = useState('');
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [savedOk, setSavedOk] = useState(false);

    useEffect(() => {
        if (!open) return;
        setError(null);
        setSavedOk(false);
        setLoading(true);
        crossStoreApi
            .getAgentSoul(tenantId, agentId)
            .then((r) => setContent(r.content || ''))
            .catch((e) => {
                const status = (e as any)?.status;
                if (status === 409) {
                    setError("This agent isn't Paperclip-managed and can't have its SOUL edited via this surface. Contact operator support.");
                } else {
                    setError(`Failed to load SOUL: ${(e as Error).message || String(e)}`);
                }
            })
            .finally(() => setLoading(false));
    }, [open, tenantId, agentId]);

    if (!open) return null;

    const tooEmpty = !content || !content.trim();
    const tooLong = content.length > MAX_LEN;
    const canSave = !loading && !saving && !tooEmpty && !tooLong && !error;

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        setSavedOk(false);
        try {
            await crossStoreApi.putAgentSoul(tenantId, agentId, content);
            setSavedOk(true);
            onSaved?.();
            setTimeout(() => onClose(), 700);
        } catch (e) {
            const status = (e as any)?.status;
            if (status === 422) {
                setError(`Validation error: ${(e as Error).message || 'invalid content'}`);
            } else if (status === 409) {
                setError("Agent is not Paperclip-managed; cannot save.");
            } else if (status === 502) {
                setError("Paperclip unreachable; try again in a moment.");
            } else {
                setError(`Save failed: ${(e as Error).message || String(e)}`);
            }
        } finally {
            setSaving(false);
        }
    };

    return (
        <div
            style={{
                position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center',
                zIndex: 10000,
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
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
                    <h4 style={{ fontSize: 15, margin: 0 }}>Edit SOUL — {agentName}</h4>
                    <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                        Source: Paperclip / instructions-bundle/SOUL.md
                    </span>
                </div>

                {loading ? (
                    <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>Loading…</div>
                ) : (
                    <textarea
                        value={content}
                        onChange={(e) => { setContent(e.target.value); setSavedOk(false); }}
                        spellCheck={false}
                        style={{
                            flex: 1, minHeight: 360, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                            fontSize: 13, lineHeight: 1.5, padding: 12,
                            background: 'var(--bg-secondary)', color: 'var(--text-primary)',
                            border: `1px solid ${tooLong ? 'var(--danger)' : 'var(--border-subtle)'}`,
                            borderRadius: 8, resize: 'vertical', outline: 'none',
                        }}
                    />
                )}

                <div style={{
                    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                    marginTop: 8, fontSize: 11,
                    color: tooLong ? 'var(--danger)' : tooEmpty ? 'var(--text-tertiary)' : 'var(--text-secondary)',
                }}>
                    <span>
                        {content.length.toLocaleString()} / {MAX_LEN.toLocaleString()} chars
                        {tooEmpty && ' · empty content not allowed'}
                        {tooLong && ' · exceeds limit'}
                    </span>
                    {savedOk && <span style={{ color: 'var(--success)' }}>✓ Saved + cache refreshed</span>}
                </div>

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
                    <button className="btn btn-secondary" onClick={onClose} disabled={saving}>Cancel</button>
                    <button className="btn btn-primary" onClick={handleSave} disabled={!canSave}>
                        {saving ? 'Saving…' : 'Save SOUL'}
                    </button>
                </div>
            </div>
        </div>
    );
}

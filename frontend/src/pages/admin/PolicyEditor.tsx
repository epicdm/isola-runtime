/**
 * PolicyEditor — operator surface for per-agent policy management.
 * Route: /admin/companies/:tenantId/agents/:agentId/policy
 *
 * R28-revised + R29 + R31 + R32 + R34 + R35 + R36 + R37 (2026-05-04 night).
 *
 * Single page (R32, no tabs) with 4 sub-sections:
 *   A. Channel Binding — RO WA + Voice from BFF tenant_registry, plus
 *                        ChannelConfig CRUD list (R34 admin endpoints)
 *   B. Autonomy Policy — 9 keys editable (R35); per-key enforcement badge
 *                        ✅ Enforced Wave-1 vs 🟡 Scaffolded W2; map-only
 *                        orphans NEVER exposed (filtered server-side)
 *   C. Escalation Keywords — chip-list (R31); inline-built (no existing
 *                            chip-list component per Phase 2 pre-build probe)
 *   D. Business Hours — RO from SOUL.md frontmatter (R29); structured
 *                       editing deferred to W2-HW
 *
 * Save semantics: autonomy section + escalation section have INDEPENDENT
 * Save buttons. Channel CRUD operates per-row via OperatorChannelConfigModal.
 *
 * On 409 from PATCH /policy (agent.tenant_id IS NULL): inline error.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { crossStoreApi, type AgentChannelRow, type AgentPolicy } from '../../services/api';
import OperatorChannelConfigModal from '../../components/OperatorChannelConfigModal';

// User-facing labels for the 9 autonomy keys (R35). Server enforces the
// whitelist; this map is just for display.
const AUTONOMY_LABELS: Record<string, string> = {
    write_workspace_files: 'Write workspace files',
    delete_files: 'Delete files',
    read_files: 'Read files',
    send_external_message: 'Send external message',
    modify_soul: 'Modify SOUL.md',
    access_business_system_read: 'Access business system (read)',
    access_business_system_write: 'Access business system (write)',
    create_calendar_event: 'Create calendar event',
    financial_operations: 'Financial operations',
};

const LEVEL_LABELS: Record<'L1' | 'L2' | 'L3', string> = {
    L1: 'L1 — Autonomous',
    L2: 'L2 — Notify owner after',
    L3: 'L3 — Owner WA confirm before',
};

const ESCAL_MAX_KEYWORDS = 50;
const ESCAL_MAX_CHAR = 100;

function StatusPill({ value }: { value: string | null | undefined }) {
    if (!value) return <span style={{ color: 'var(--text-tertiary)' }}>—</span>;
    const palette: Record<string, string> = {
        active: '#22c55e', not_connected: '#94a3b8',
        provisioning: '#eab308', failed: '#ef4444',
    };
    const color = palette[value] || '#94a3b8';
    return (
        <span style={{
            display: 'inline-block', padding: '2px 8px', borderRadius: 12,
            fontSize: 11, fontWeight: 600, color,
            background: `${color}1a`, border: `1px solid ${color}33`,
        }}>{value}</span>
    );
}

function EnforcementBadge({ status }: { status: 'enforced' | 'scaffolded' }) {
    if (status === 'enforced') {
        return (
            <span title="Wave-1 enforcement active" style={{
                display: 'inline-block', padding: '1px 7px', borderRadius: 10,
                fontSize: 10, fontWeight: 600, color: '#22c55e',
                background: '#22c55e1a', border: '1px solid #22c55e33',
            }}>✅ Enforced</span>
        );
    }
    return (
        <span title="Saved but no tool enforces this gate yet — will activate when the corresponding tool deploys (Wave-2)" style={{
            display: 'inline-block', padding: '1px 7px', borderRadius: 10,
            fontSize: 10, fontWeight: 600, color: '#eab308',
            background: '#eab3081a', border: '1px solid #eab30833',
        }}>🟡 Scaffolded</span>
    );
}

function IdRow({ label, value }: { label: string; value: string | null | undefined }) {
    return (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0', borderBottom: '1px solid var(--border)' }}>
            <div style={{ width: 200, color: 'var(--text-tertiary)', fontSize: 12 }}>{label}</div>
            <div style={{ flex: 1, fontFamily: 'monospace', fontSize: 12 }}>
                {value ?? <span style={{ color: 'var(--text-tertiary)' }}>—</span>}
            </div>
        </div>
    );
}

export default function PolicyEditor() {
    const { tenantId = '', agentId = '' } = useParams<{ tenantId: string; agentId: string }>();
    const navigate = useNavigate();

    const [policy, setPolicy] = useState<AgentPolicy | null>(null);
    const [channels, setChannels] = useState<AgentChannelRow[] | null>(null);
    const [supportedTypes, setSupportedTypes] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [loadErr, setLoadErr] = useState<string | null>(null);

    // Local edit state (mirrors fetched data; section-level Save commits)
    const [autonomy, setAutonomy] = useState<Record<string, 'L1' | 'L2' | 'L3'>>({});
    const [autonomyDirty, setAutonomyDirty] = useState(false);
    const [autonomySaving, setAutonomySaving] = useState(false);
    const [autonomyErr, setAutonomyErr] = useState<string | null>(null);
    const [autonomyOk, setAutonomyOk] = useState(false);

    const [keywords, setKeywords] = useState<string[]>([]);
    const [keywordInput, setKeywordInput] = useState('');
    const [keywordsDirty, setKeywordsDirty] = useState(false);
    const [keywordsSaving, setKeywordsSaving] = useState(false);
    const [keywordsErr, setKeywordsErr] = useState<string | null>(null);
    const [keywordsOk, setKeywordsOk] = useState(false);

    const [channelModal, setChannelModal] = useState<
        | null
        | { mode: 'create' }
        | { mode: 'edit'; existing: AgentChannelRow }
    >(null);

    const [confirmDeleteCid, setConfirmDeleteCid] = useState<string | null>(null);
    const [deletingCid, setDeletingCid] = useState<string | null>(null);

    const refetchPolicy = async () => {
        if (!tenantId || !agentId) return;
        setLoading(true);
        setLoadErr(null);
        try {
            const [p, ch] = await Promise.all([
                crossStoreApi.getAgentPolicy(tenantId, agentId),
                crossStoreApi.listAgentChannels(tenantId, agentId),
            ]);
            setPolicy(p);
            setChannels(ch.channels);
            setSupportedTypes(ch.supported_channel_types ?? []);
            // Hydrate local edit state from server response
            const am: Record<string, 'L1' | 'L2' | 'L3'> = {};
            for (const k of p.autonomy_policy.keys) {
                am[k.key] = k.value;
            }
            setAutonomy(am);
            setAutonomyDirty(false);
            setKeywords(p.escalation_keywords);
            setKeywordsDirty(false);
        } catch (e) {
            const status = (e as any)?.status;
            const detail = (e as any)?.detail;
            if (status === 409 && detail?.error === 'agent_not_clawith_managed') {
                setLoadErr("This agent isn't Clawith-managed; policies cannot be edited.");
            } else if (status === 404) {
                setLoadErr('Agent not found under this tenant.');
            } else {
                setLoadErr(`Failed to load policy: ${(e as Error).message || String(e)}`);
            }
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        refetchPolicy();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tenantId, agentId]);

    const refetchChannels = async () => {
        if (!tenantId || !agentId) return;
        try {
            const ch = await crossStoreApi.listAgentChannels(tenantId, agentId);
            setChannels(ch.channels);
            setSupportedTypes(ch.supported_channel_types ?? []);
        } catch {
            // non-fatal — UI shows stale list until next full refetch
        }
    };

    const setAutonomyValue = (key: string, value: 'L1' | 'L2' | 'L3') => {
        setAutonomy((prev) => ({ ...prev, [key]: value }));
        setAutonomyDirty(true);
        setAutonomyOk(false);
    };

    const handleSaveAutonomy = async () => {
        setAutonomySaving(true);
        setAutonomyErr(null);
        setAutonomyOk(false);
        try {
            const body = await crossStoreApi.patchAgentPolicy(tenantId, agentId, { autonomy_policy: autonomy });
            setPolicy((prev) => prev ? { ...prev, autonomy_policy: body.autonomy_policy } : prev);
            setAutonomyDirty(false);
            setAutonomyOk(true);
            setTimeout(() => setAutonomyOk(false), 2000);
        } catch (e) {
            const status = (e as any)?.status;
            const detail = (e as any)?.detail;
            if (status === 409) {
                setAutonomyErr("Agent isn't Clawith-managed; cannot save autonomy.");
            } else if (status === 422) {
                setAutonomyErr(`Validation failed: ${(e as Error).message || JSON.stringify(detail)}`);
            } else {
                setAutonomyErr(`Save failed: ${(e as Error).message || String(e)}`);
            }
        } finally {
            setAutonomySaving(false);
        }
    };

    // Chip-list interactions (R31)
    const addKeyword = (raw: string) => {
        const stripped = raw.trim();
        if (!stripped) return;
        if (stripped.length > ESCAL_MAX_CHAR) {
            setKeywordsErr(`Keyword exceeds ${ESCAL_MAX_CHAR} chars`);
            return;
        }
        if (keywords.length >= ESCAL_MAX_KEYWORDS) {
            setKeywordsErr(`Maximum ${ESCAL_MAX_KEYWORDS} keywords`);
            return;
        }
        const lower = stripped.toLowerCase();
        if (keywords.some((k) => k.toLowerCase() === lower)) {
            setKeywordsErr('Duplicate keyword (case-insensitive)');
            return;
        }
        setKeywords((prev) => [...prev, stripped]);
        setKeywordInput('');
        setKeywordsDirty(true);
        setKeywordsOk(false);
        setKeywordsErr(null);
    };

    const removeKeyword = (idx: number) => {
        setKeywords((prev) => prev.filter((_, i) => i !== idx));
        setKeywordsDirty(true);
        setKeywordsOk(false);
        setKeywordsErr(null);
    };

    const handleKeywordKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            addKeyword(keywordInput);
        } else if (e.key === 'Backspace' && keywordInput === '' && keywords.length > 0) {
            removeKeyword(keywords.length - 1);
        }
    };

    const handleSaveKeywords = async () => {
        setKeywordsSaving(true);
        setKeywordsErr(null);
        setKeywordsOk(false);
        try {
            const body = await crossStoreApi.patchAgentPolicy(tenantId, agentId, { escalation_keywords: keywords });
            setPolicy((prev) => prev ? { ...prev, escalation_keywords: body.escalation_keywords } : prev);
            setKeywords(body.escalation_keywords);
            setKeywordsDirty(false);
            setKeywordsOk(true);
            setTimeout(() => setKeywordsOk(false), 2000);
        } catch (e) {
            const status = (e as any)?.status;
            const detail = (e as any)?.detail;
            if (status === 409) {
                setKeywordsErr("Agent isn't Clawith-managed; cannot save keywords.");
            } else if (status === 422) {
                setKeywordsErr(`Validation failed: ${(e as Error).message || JSON.stringify(detail)}`);
            } else {
                setKeywordsErr(`Save failed: ${(e as Error).message || String(e)}`);
            }
        } finally {
            setKeywordsSaving(false);
        }
    };

    const handleDeleteChannel = async (cid: string) => {
        setDeletingCid(cid);
        try {
            await crossStoreApi.deleteAgentChannel(tenantId, agentId, cid);
            setConfirmDeleteCid(null);
            await refetchChannels();
        } catch (e) {
            // Error surfaces via reload error state
            console.error('Channel delete failed:', e);
        } finally {
            setDeletingCid(null);
        }
    };

    const wa = policy?.channel_binding?.whatsapp ?? null;
    const voice = policy?.channel_binding?.voice ?? null;
    const businessHours = policy?.business_hours_readonly ?? null;

    const sectionStyle: React.CSSProperties = {
        background: 'var(--bg-secondary)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 10,
        padding: 20,
        marginBottom: 20,
    };
    const h3Style: React.CSSProperties = { marginTop: 0, marginBottom: 14, fontSize: 14, color: 'var(--text-primary)' };

    if (loading) {
        return (
            <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-tertiary)' }}>
                Loading agent policy…
            </div>
        );
    }
    if (loadErr || !policy) {
        return (
            <div style={{ padding: 24, maxWidth: 720, margin: '0 auto' }}>
                <button
                    onClick={() => navigate(`/admin/companies/${encodeURIComponent(tenantId)}`)}
                    style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', fontSize: 13, marginBottom: 12 }}
                >
                    ← Back to tenant
                </button>
                <div style={{
                    padding: 16, fontSize: 13,
                    background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                    borderRadius: 8, border: '1px solid var(--danger, #d05050)',
                }}>
                    {loadErr ?? 'Could not load policy.'}
                </div>
            </div>
        );
    }

    return (
        <div style={{ padding: 24, maxWidth: 880, margin: '0 auto' }}>
            <button
                onClick={() => navigate(`/admin/companies/${encodeURIComponent(tenantId)}`)}
                style={{
                    background: 'none', border: 'none', color: 'var(--text-secondary)',
                    cursor: 'pointer', fontSize: 13, marginBottom: 12, padding: 0,
                }}
            >
                ← Back to tenant
            </button>

            <h2 style={{ fontSize: 18, marginTop: 0, marginBottom: 4 }}>
                Agent policy
            </h2>
            <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 20, fontFamily: 'monospace' }}>
                tenant: {tenantId}  ·  agent: {agentId}
            </div>

            {/* ─── Section A: Channel Binding ─────────────────────────────── */}
            <div style={sectionStyle}>
                <h3 style={h3Style}>A · Channel binding</h3>

                <div style={{ marginBottom: 16 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <strong style={{ fontSize: 13 }}>WhatsApp</strong>
                        <StatusPill value={wa?.whatsappStatus} />
                        <span title="WhatsApp channel binding managed via Embedded Signup; cannot edit from operator console." style={{
                            fontSize: 10, color: 'var(--text-tertiary)', fontStyle: 'italic',
                        }}>
                            (read-only — managed via Embedded Signup)
                        </span>
                    </div>
                    <IdRow label="display phone" value={wa?.displayPhone} />
                    <IdRow label="phone_number_id" value={wa?.waPhoneNumberId} />
                    <IdRow label="WABA id" value={wa?.wabaId} />
                    <IdRow label="owner phone" value={wa?.ownerPhone} />
                    <IdRow label="DID number" value={wa?.didNumber} />
                    <IdRow label="Magnus DID id" value={wa?.magnusDidId} />
                    <IdRow label="activated at" value={wa?.whatsappActivatedAt} />
                </div>

                {voice && (
                    <div style={{ marginBottom: 16 }}>
                        <strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>Voice</strong>
                        <IdRow label="DID number" value={voice.didNumber} />
                        <IdRow label="Magnus DID id" value={voice.magnusDidId} />
                    </div>
                )}

                <div style={{ marginTop: 20, paddingTop: 14, borderTop: '1px solid var(--border-subtle)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                        <strong style={{ fontSize: 13 }}>Channel configurations</strong>
                        <button
                            onClick={() => setChannelModal({ mode: 'create' })}
                            style={{
                                padding: '5px 12px', background: 'var(--accent, #3b82f6)',
                                border: 'none', borderRadius: 6, cursor: 'pointer',
                                color: '#fff', fontSize: 12, fontWeight: 600,
                            }}
                        >
                            + Add channel
                        </button>
                    </div>
                    {channels && channels.length === 0 ? (
                        <div style={{ padding: 16, color: 'var(--text-tertiary)', fontSize: 12 }}>
                            No channel configurations yet. Supported in Wave-1: {supportedTypes.join(', ')}.
                        </div>
                    ) : (
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                            <thead>
                                <tr>
                                    <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--text-tertiary)' }}>type</th>
                                    <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--text-tertiary)' }}>display name</th>
                                    <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--text-tertiary)' }}>app_id</th>
                                    <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--text-tertiary)' }}>configured</th>
                                    <th style={{ textAlign: 'left', padding: '6px 4px', color: 'var(--text-tertiary)' }}>connected</th>
                                    <th style={{ textAlign: 'right', padding: '6px 4px' }}></th>
                                </tr>
                            </thead>
                            <tbody>
                                {(channels ?? []).map((c) => (
                                    <tr key={c.channel_id} style={{ borderTop: '1px solid var(--border)' }}>
                                        <td style={{ padding: '8px 4px', fontWeight: 500 }}>{c.channel_type}</td>
                                        <td style={{ padding: '8px 4px', color: c.display_name ? 'var(--text-primary)' : 'var(--text-tertiary)' }}>
                                            {c.display_name ?? '—'}
                                        </td>
                                        <td style={{ padding: '8px 4px', fontFamily: 'monospace', color: 'var(--text-tertiary)' }}>
                                            {c.app_id ?? '—'}
                                        </td>
                                        <td style={{ padding: '8px 4px' }}>{c.is_configured ? '✓' : '—'}</td>
                                        <td style={{ padding: '8px 4px' }}>{c.is_connected ? '✓' : '—'}</td>
                                        <td style={{ padding: '8px 4px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                                            {confirmDeleteCid === c.channel_id ? (
                                                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                                                    <span style={{ color: 'var(--text-tertiary)', marginRight: 4 }}>Delete?</span>
                                                    <button
                                                        onClick={() => setConfirmDeleteCid(null)}
                                                        disabled={deletingCid === c.channel_id}
                                                        style={{ padding: '3px 8px', background: 'none', border: '1px solid var(--border)', borderRadius: 5, cursor: 'pointer', color: 'var(--text-primary)', fontSize: 11 }}
                                                    >
                                                        Cancel
                                                    </button>
                                                    <button
                                                        onClick={() => handleDeleteChannel(c.channel_id)}
                                                        disabled={deletingCid === c.channel_id}
                                                        style={{ padding: '3px 8px', background: '#ef4444', border: 'none', borderRadius: 5, cursor: deletingCid === c.channel_id ? 'wait' : 'pointer', color: '#fff', fontSize: 11, fontWeight: 600 }}
                                                    >
                                                        {deletingCid === c.channel_id ? '…' : 'Yes, delete'}
                                                    </button>
                                                </span>
                                            ) : (
                                                <>
                                                    <button
                                                        onClick={() => setChannelModal({ mode: 'edit', existing: c })}
                                                        style={{ padding: '3px 8px', background: 'none', border: '1px solid var(--border)', borderRadius: 5, cursor: 'pointer', color: 'var(--text-primary)', fontSize: 11, marginRight: 4 }}
                                                    >
                                                        Edit
                                                    </button>
                                                    <button
                                                        onClick={() => setConfirmDeleteCid(c.channel_id)}
                                                        style={{ padding: '3px 8px', background: 'none', border: '1px solid #ef4444', borderRadius: 5, cursor: 'pointer', color: '#ef4444', fontSize: 11 }}
                                                    >
                                                        Delete
                                                    </button>
                                                </>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                    <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-tertiary)' }}>
                        Note: deleting a channel here removes the local config. Webhook subscriptions on
                        the channel side may persist — manual deauth may be required (W2-HW: per-channel
                        deauth hook).
                    </div>
                </div>
            </div>

            {/* ─── Section B: Autonomy Policy ─────────────────────────────── */}
            <div style={sectionStyle}>
                <h3 style={h3Style}>B · Autonomy policy</h3>

                <div style={{
                    marginBottom: 14, padding: '10px 12px', fontSize: 11, lineHeight: 1.5,
                    background: 'rgba(234,179,8,0.10)', color: 'var(--text-secondary)',
                    borderRadius: 6, border: '1px solid rgba(234,179,8,0.40)',
                }}>
                    Some autonomy controls are <strong>scaffolded</strong> for future tools. Settings are
                    saved and will take effect when the corresponding tools deploy.
                    <br />
                    Wave-1 enforcement: <code>write_workspace_files</code> + <code>delete_files</code>.
                    All other gates currently run via the agent's probation level (L1/L2/L3).
                </div>

                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                        <tr style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                            <th style={{ textAlign: 'left', padding: '8px 4px', color: 'var(--text-tertiary)' }}>action</th>
                            <th style={{ textAlign: 'left', padding: '8px 4px', color: 'var(--text-tertiary)' }}>status</th>
                            <th style={{ textAlign: 'left', padding: '8px 4px', color: 'var(--text-tertiary)' }}>autonomy level</th>
                        </tr>
                    </thead>
                    <tbody>
                        {policy.autonomy_policy.keys.map((k) => (
                            <tr key={k.key} style={{ borderBottom: '1px solid var(--border)' }}>
                                <td style={{ padding: '8px 4px', fontWeight: 500 }}>
                                    {AUTONOMY_LABELS[k.key] ?? k.key}
                                </td>
                                <td style={{ padding: '8px 4px' }}>
                                    <EnforcementBadge status={k.enforcement_status} />
                                </td>
                                <td style={{ padding: '8px 4px' }}>
                                    <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'var(--text-secondary)' }}>
                                        {(['L1', 'L2', 'L3'] as const).map((lvl) => (
                                            <label key={lvl} style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
                                                <input
                                                    type="radio"
                                                    name={`autonomy-${k.key}`}
                                                    checked={(autonomy[k.key] ?? k.value) === lvl}
                                                    onChange={() => setAutonomyValue(k.key, lvl)}
                                                />
                                                {LEVEL_LABELS[lvl]}
                                            </label>
                                        ))}
                                    </div>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>

                {autonomyErr && (
                    <div style={{
                        marginTop: 12, padding: 10, fontSize: 12,
                        background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                        borderRadius: 6, border: '1px solid var(--danger, #d05050)',
                    }}>
                        {autonomyErr}
                    </div>
                )}

                <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginTop: 14 }}>
                    {autonomyOk && <span style={{ fontSize: 11, color: 'var(--success, #22c55e)' }}>✓ Saved</span>}
                    <button
                        onClick={handleSaveAutonomy}
                        disabled={!autonomyDirty || autonomySaving}
                        style={{
                            padding: '6px 14px',
                            background: autonomyDirty ? 'var(--accent, #3b82f6)' : 'var(--bg-tertiary, #2a2a2a)',
                            border: 'none', borderRadius: 6, cursor: autonomyDirty ? 'pointer' : 'not-allowed',
                            color: '#fff', fontSize: 12, fontWeight: 600,
                            opacity: autonomyDirty ? 1 : 0.6,
                        }}
                    >
                        {autonomySaving ? 'Saving…' : 'Save autonomy'}
                    </button>
                </div>
            </div>

            {/* ─── Section C: Escalation Keywords ─────────────────────────── */}
            <div style={sectionStyle}>
                <h3 style={h3Style}>C · Escalation keywords</h3>
                <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginBottom: 10 }}>
                    Inbound messages containing any of these keywords escalate to a human operator
                    instead of auto-reply. Max {ESCAL_MAX_KEYWORDS} keywords, {ESCAL_MAX_CHAR} chars each. Case-insensitive deduplication.
                </div>

                <div style={{
                    display: 'flex', flexWrap: 'wrap', gap: 6, padding: 8,
                    minHeight: 40, background: 'var(--bg-tertiary, #1a1a1a)',
                    border: '1px solid var(--border-subtle)', borderRadius: 6,
                }}>
                    {keywords.map((kw, idx) => (
                        <span key={`${kw}-${idx}`} style={{
                            display: 'inline-flex', alignItems: 'center', gap: 4,
                            padding: '3px 8px 3px 10px', borderRadius: 12, fontSize: 11,
                            background: 'var(--accent, #3b82f6)', color: '#fff',
                        }}>
                            {kw}
                            <button
                                onClick={() => removeKeyword(idx)}
                                style={{
                                    background: 'rgba(0,0,0,0.20)', border: 'none', borderRadius: '50%',
                                    width: 16, height: 16, cursor: 'pointer', color: '#fff',
                                    fontSize: 11, lineHeight: 1, padding: 0,
                                }}
                                aria-label={`Remove keyword ${kw}`}
                            >
                                ×
                            </button>
                        </span>
                    ))}
                    <input
                        type="text"
                        value={keywordInput}
                        onChange={(e) => setKeywordInput(e.target.value)}
                        onKeyDown={handleKeywordKey}
                        placeholder={keywords.length === 0 ? 'Type a keyword and press Enter…' : ''}
                        style={{
                            flex: 1, minWidth: 160, padding: '4px 6px',
                            background: 'transparent', color: 'var(--text-primary)',
                            border: 'none', fontSize: 12, outline: 'none',
                        }}
                    />
                </div>
                <div style={{ marginTop: 6, fontSize: 10, color: 'var(--text-tertiary)' }}>
                    {keywords.length} / {ESCAL_MAX_KEYWORDS} keywords
                </div>

                {keywordsErr && (
                    <div style={{
                        marginTop: 10, padding: 10, fontSize: 12,
                        background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                        borderRadius: 6, border: '1px solid var(--danger, #d05050)',
                    }}>
                        {keywordsErr}
                    </div>
                )}

                <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 12, marginTop: 14 }}>
                    {keywordsOk && <span style={{ fontSize: 11, color: 'var(--success, #22c55e)' }}>✓ Saved</span>}
                    <button
                        onClick={handleSaveKeywords}
                        disabled={!keywordsDirty || keywordsSaving}
                        style={{
                            padding: '6px 14px',
                            background: keywordsDirty ? 'var(--accent, #3b82f6)' : 'var(--bg-tertiary, #2a2a2a)',
                            border: 'none', borderRadius: 6, cursor: keywordsDirty ? 'pointer' : 'not-allowed',
                            color: '#fff', fontSize: 12, fontWeight: 600,
                            opacity: keywordsDirty ? 1 : 0.6,
                        }}
                    >
                        {keywordsSaving ? 'Saving…' : 'Save keywords'}
                    </button>
                </div>
            </div>

            {/* ─── Section D: Business Hours (read-only) ──────────────────── */}
            <div style={sectionStyle}>
                <h3 style={h3Style}>D · Business hours <span style={{ fontSize: 11, color: 'var(--text-tertiary)', fontWeight: 400 }}>(read-only — Wave-1)</span></h3>

                {businessHours ? (
                    <>
                        <IdRow label="weekday" value={businessHours.hours_weekday ?? null} />
                        <IdRow label="saturday" value={businessHours.hours_saturday ?? null} />
                        <IdRow label="sunday" value={businessHours.hours_sunday ?? null} />
                        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text-tertiary)' }}>
                            Source: {businessHours.source ?? 'soul.md frontmatter'}
                        </div>
                    </>
                ) : (
                    <div style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
                        Not configured. Hours can be added via the SOUL editor (frontmatter
                        keys <code>hours_weekday</code>, <code>hours_saturday</code>, <code>hours_sunday</code>).
                    </div>
                )}
                <div style={{ marginTop: 12, fontSize: 11, color: 'var(--text-tertiary)' }}>
                    Hours are configured via the SOUL editor on the tenant detail page. Structured
                    editing lands in W2-HW.
                </div>
            </div>

            {channelModal && (
                <OperatorChannelConfigModal
                    open={!!channelModal}
                    tenantId={tenantId}
                    agentId={agentId}
                    agentName={`agent ${agentId.slice(0, 8)}`}
                    mode={channelModal.mode}
                    existing={channelModal.mode === 'edit' ? channelModal.existing : undefined}
                    onClose={() => setChannelModal(null)}
                    onSaved={() => { setChannelModal(null); refetchChannels(); }}
                />
            )}
        </div>
    );
}

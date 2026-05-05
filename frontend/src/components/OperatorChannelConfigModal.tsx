/**
 * OperatorChannelConfigModal — operator surface for creating or editing a
 * ChannelConfig row from the platform_admin operator console.
 *
 * R28-revised + R34 + R37 (2026-05-04 night):
 *   R28-revised — ChannelConfig CRUD belongs in operator console
 *   R34         — POST + PATCH dispatch through admin endpoints (audit envelope)
 *   R37         — filter shared registry to operatorSurface:true
 *
 * R36 reminder: ChannelConfig credentials persist plaintext at rest in
 * Wave-1. Encryption + read-path patching land at the pre-launch gate.
 * The advisory banner at the top of the modal makes this visible to operators.
 *
 * Modal pattern mirrors S5 SOULEditorModal (full-screen overlay, click-outside-
 * to-close, inline styles using CSS variables, status feedback inline).
 */
import { useEffect, useMemo, useState } from 'react';
import { crossStoreApi, type AgentChannelRow } from '../services/api';
import { OPERATOR_CHANNELS, type ChannelDef } from './channelRegistry';

interface Props {
    open: boolean;
    tenantId: string;
    agentId: string;
    agentName: string;
    mode: 'create' | 'edit';
    existing?: AgentChannelRow;     // present in edit mode
    onClose: () => void;
    onSaved?: () => void;
}

type FieldValues = Record<string, string | boolean>;

// Per-channel UI field values → admin POST/PATCH body. Mirrors backend
// dispatch shape in admin_crossstore.py (R34 _shape_*_body helpers).
function buildAdminBody(
    backendType: 'slack' | 'discord' | 'microsoft_teams' | 'whatsapp',
    values: FieldValues,
    displayName: string,
): Record<string, unknown> {
    const dn = displayName?.trim() || undefined;
    const v = (k: string) => (values[k] ?? '') as string;
    const vbool = (k: string) => Boolean(values[k]);
    switch (backendType) {
        case 'slack':
            return {
                channel_type: 'slack',
                display_name: dn,
                app_secret: v('bot_token'),
                extra_config: { signing_secret: v('signing_secret') },
            };
        case 'discord':
            return {
                channel_type: 'discord',
                display_name: dn,
                app_id: v('application_id'),
                app_secret: v('bot_token'),
                extra_config: {
                    connection_mode: v('connection_mode') || 'webhook',
                    public_key: v('public_key'),
                },
            };
        case 'microsoft_teams':
            return {
                channel_type: 'microsoft_teams',
                display_name: dn,
                app_id: v('app_id'),
                app_secret: v('app_secret'),
                extra_config: {
                    tenant_id: v('tenant_id'),
                    use_managed_identity: vbool('use_managed_identity'),
                },
            };
        case 'whatsapp':
            return {
                channel_type: 'whatsapp',
                display_name: dn,
                extra_config: {
                    phone_number_id: v('phone_number_id'),
                    waba_id: v('waba_id'),
                    access_token: v('access_token'),
                    verify_token: v('verify_token'),
                    app_secret: v('app_secret'),
                },
            };
    }
}

export default function OperatorChannelConfigModal({
    open, tenantId, agentId, agentName, mode, existing, onClose, onSaved,
}: Props) {
    // In edit mode, channel_type is locked to the existing row's value;
    // in create mode, operator picks from the dropdown of registered channels.
    const initialBackendType = useMemo<ChannelDef['backendChannelType'] | undefined>(() => {
        if (mode === 'edit' && existing) {
            return existing.channel_type as ChannelDef['backendChannelType'];
        }
        return OPERATOR_CHANNELS[0]?.backendChannelType;
    }, [mode, existing]);

    const [backendType, setBackendType] = useState<ChannelDef['backendChannelType'] | undefined>(initialBackendType);
    const [displayName, setDisplayName] = useState<string>(existing?.display_name ?? '');
    const [values, setValues] = useState<FieldValues>({});
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [savedOk, setSavedOk] = useState(false);
    const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});

    // Reset state on open / mode-or-row change
    useEffect(() => {
        if (!open) return;
        setBackendType(initialBackendType);
        setDisplayName(existing?.display_name ?? '');
        setValues({});
        setSaving(false);
        setError(null);
        setSavedOk(false);
        setShowSecrets({});
    }, [open, initialBackendType, existing?.display_name]);

    if (!open) return null;

    const def: ChannelDef | undefined = OPERATOR_CHANNELS.find((c) => c.backendChannelType === backendType);
    const fields = def?.fields ?? [];

    const setField = (k: string, v: string | boolean) => {
        setValues((prev) => ({ ...prev, [k]: v }));
        setSavedOk(false);
    };

    const requiredOk = fields
        .filter((f) => f.required)
        .every((f) => {
            const val = values[f.key];
            return typeof val === 'string' ? val.trim().length > 0 : Boolean(val);
        });

    const canSave = !saving && !!def && !!backendType && (mode === 'edit' || requiredOk) && !error;

    const handleSave = async () => {
        if (!def || !backendType) return;
        setSaving(true);
        setError(null);
        setSavedOk(false);
        try {
            const body = buildAdminBody(backendType, values, displayName);
            if (mode === 'create') {
                await crossStoreApi.createAgentChannel(tenantId, agentId, body);
            } else if (existing) {
                // PATCH: only send fields the operator touched. For Phase 1
                // simplicity we send display_name (always) + any cred fields
                // the operator filled in. Empty cred fields → omitted.
                const patchBody: Record<string, unknown> = { display_name: displayName?.trim() || undefined };
                const credBody = buildAdminBody(backendType, values, displayName) as Record<string, unknown>;
                if (credBody.app_id) patchBody.app_id = credBody.app_id;
                if (credBody.app_secret) patchBody.app_secret = credBody.app_secret;
                if (credBody.extra_config && Object.values(credBody.extra_config as object).some((x) => x)) {
                    patchBody.extra_config = credBody.extra_config;
                }
                await crossStoreApi.patchAgentChannel(tenantId, agentId, existing.channel_id, patchBody);
            }
            setSavedOk(true);
            onSaved?.();
            setTimeout(() => onClose(), 700);
        } catch (e) {
            const status = (e as any)?.status;
            const detail = (e as any)?.detail;
            if (status === 422 && detail?.error === 'unsupported_channel_type') {
                setError(`Channel type '${detail.channel_type}' is not supported in Wave-1.`);
            } else if (status === 422 && detail?.error === 'per_channel_handler_rejected') {
                setError(`Channel API rejected the credentials: ${detail.handler_detail || 'unknown reason'}`);
            } else if (status === 409) {
                setError(detail?.error === 'agent_not_clawith_managed'
                    ? "This agent isn't Clawith-managed; cannot configure channels."
                    : 'Channel config conflict (409).');
            } else if (status === 422) {
                setError(`Validation failed: ${(e as Error).message || JSON.stringify(detail)}`);
            } else if (status === 502) {
                setError('Channel handler unreachable. Try again in a moment.');
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
                    width: 600, maxWidth: '94vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                    border: '1px solid var(--border-subtle)', boxShadow: '0 20px 60px rgba(0,0,0,0.4)',
                    overflowY: 'auto',
                }}
            >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
                    <h4 style={{ fontSize: 15, margin: 0 }}>
                        {mode === 'create' ? 'Add Channel' : 'Edit Channel'} — {agentName}
                    </h4>
                    <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                        Operator console (platform_admin)
                    </span>
                </div>

                {/* R36 plaintext-at-rest advisory */}
                <div style={{
                    marginTop: 6, marginBottom: 14, padding: '8px 10px',
                    fontSize: 11, lineHeight: 1.4,
                    background: 'rgba(234,179,8,0.10)', color: 'var(--text-secondary)',
                    borderRadius: 6, border: '1px solid rgba(234,179,8,0.40)',
                }}>
                    Credentials are stored in plaintext for Wave-1. Encryption + read-path
                    patching ship at the pre-launch hardening gate.
                </div>

                {/* Channel type selector — locked in edit mode */}
                <label style={{ display: 'block', fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                    Channel type
                </label>
                <select
                    value={backendType ?? ''}
                    disabled={mode === 'edit'}
                    onChange={(e) => {
                        setBackendType(e.target.value as ChannelDef['backendChannelType']);
                        setValues({});
                        setSavedOk(false);
                    }}
                    style={{
                        width: '100%', padding: '8px 10px', marginBottom: 14,
                        background: mode === 'edit' ? 'var(--bg-tertiary, #2a2a2a)' : 'var(--bg-secondary)',
                        color: 'var(--text-primary)', border: '1px solid var(--border-subtle)',
                        borderRadius: 6, fontSize: 13,
                    }}
                >
                    {OPERATOR_CHANNELS.map((c) => (
                        <option key={c.id} value={c.backendChannelType}>
                            {c.nameFallback} — {c.desc}
                        </option>
                    ))}
                </select>

                {/* Display name — operator label */}
                <label style={{ display: 'block', fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                    Display name (optional, max 100 chars)
                </label>
                <input
                    type="text"
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value.slice(0, 100))}
                    placeholder="e.g. Production Slack workspace"
                    style={{
                        width: '100%', padding: '8px 10px', marginBottom: 14,
                        background: 'var(--bg-secondary)', color: 'var(--text-primary)',
                        border: '1px solid var(--border-subtle)', borderRadius: 6, fontSize: 13,
                    }}
                />

                {/* Per-channel credential fields */}
                {fields.map((f) => {
                    const isPassword = f.type === 'password';
                    const reveal = !!showSecrets[f.key];
                    return (
                        <div key={f.key} style={{ marginBottom: 12 }}>
                            <label style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                                <span>
                                    {f.label}
                                    {f.required && <span style={{ color: 'var(--danger, #d05050)', marginLeft: 4 }}>*</span>}
                                </span>
                                {isPassword && (
                                    <button
                                        type="button"
                                        onClick={() => setShowSecrets((p) => ({ ...p, [f.key]: !p[f.key] }))}
                                        style={{
                                            background: 'none', border: 'none', cursor: 'pointer',
                                            color: 'var(--text-tertiary)', fontSize: 11, padding: 0,
                                        }}
                                    >
                                        {reveal ? 'Hide' : 'Show'}
                                    </button>
                                )}
                            </label>
                            <input
                                type={isPassword && !reveal ? 'password' : 'text'}
                                value={(values[f.key] as string) ?? ''}
                                onChange={(e) => setField(f.key, e.target.value)}
                                placeholder={mode === 'edit' ? '(unchanged — leave blank to keep existing)' : f.placeholder ?? ''}
                                style={{
                                    width: '100%', padding: '8px 10px',
                                    background: 'var(--bg-secondary)', color: 'var(--text-primary)',
                                    border: '1px solid var(--border-subtle)', borderRadius: 6,
                                    fontSize: 13, fontFamily: isPassword ? 'ui-monospace, monospace' : 'inherit',
                                }}
                            />
                        </div>
                    );
                })}

                {/* Discord connection_mode special case */}
                {backendType === 'discord' && (
                    <div style={{ marginBottom: 12 }}>
                        <label style={{ display: 'block', fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 4 }}>
                            Connection mode
                        </label>
                        <select
                            value={(values['connection_mode'] as string) ?? 'webhook'}
                            onChange={(e) => setField('connection_mode', e.target.value)}
                            style={{
                                width: '100%', padding: '8px 10px',
                                background: 'var(--bg-secondary)', color: 'var(--text-primary)',
                                border: '1px solid var(--border-subtle)', borderRadius: 6, fontSize: 13,
                            }}
                        >
                            <option value="webhook">Webhook (HTTP interactions)</option>
                            <option value="gateway">Gateway (websocket; auto-start)</option>
                        </select>
                    </div>
                )}

                {/* Teams managed-identity special case */}
                {backendType === 'microsoft_teams' && (
                    <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input
                            id="teams-mi"
                            type="checkbox"
                            checked={Boolean(values['use_managed_identity'])}
                            onChange={(e) => setField('use_managed_identity', e.target.checked)}
                        />
                        <label htmlFor="teams-mi" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                            Use Azure Managed Identity (skip app_secret)
                        </label>
                    </div>
                )}

                {error && (
                    <div style={{
                        marginTop: 6, padding: 10, fontSize: 12,
                        background: 'var(--danger-bg, rgba(220,80,80,0.12))', color: 'var(--danger, #d05050)',
                        borderRadius: 6, border: '1px solid var(--danger, #d05050)',
                    }}>
                        {error}
                    </div>
                )}

                {savedOk && (
                    <div style={{
                        marginTop: 6, padding: 10, fontSize: 12,
                        background: 'rgba(34,197,94,0.10)', color: 'var(--success, #22c55e)',
                        borderRadius: 6, border: '1px solid var(--success, #22c55e)',
                    }}>
                        ✓ Saved
                    </div>
                )}

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
                    <button className="btn btn-secondary" onClick={onClose} disabled={saving}>Cancel</button>
                    <button className="btn btn-primary" onClick={handleSave} disabled={!canSave}>
                        {saving ? 'Saving…' : mode === 'create' ? 'Create channel' : 'Save changes'}
                    </button>
                </div>
            </div>
        </div>
    );
}

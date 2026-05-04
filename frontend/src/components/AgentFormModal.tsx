import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
    crossStoreAgentsApi,
    type AgentCreatePayload,
    type AgentRow,
    type AgentUpdatePayload,
} from '../services/api';

// L4 S4 shared modal: create + edit. Per ratification 13, minimal field
// surface (role + welcome + tone). Per ratification 14, role dropdown is
// Rex preset only for Wave-1; schema field stays open string so future
// presets land without migration.

type Props = {
    tenantId: string;
    mode: 'create' | 'edit';
    agent?: AgentRow;
    onClose: () => void;
    onSaved: () => void;
};

const TONE_OPTIONS: { value: number; labelKey: string; fallback: string }[] = [
    { value: 0, labelKey: 'admin.agents.tone.0', fallback: 'Friendly' },
    { value: 1, labelKey: 'admin.agents.tone.1', fallback: 'Professional' },
    { value: 2, labelKey: 'admin.agents.tone.2', fallback: 'Formal' },
];

export default function AgentFormModal({ tenantId, mode, agent, onClose, onSaved }: Props) {
    const { t } = useTranslation();
    const [name, setName] = useState(agent?.name ?? 'Rex');
    const [role, setRole] = useState(agent?.role_description ?? 'rex');
    const [welcomeMessage, setWelcomeMessage] = useState(agent?.welcome_message ?? '');
    const [tone, setTone] = useState<number>(agent?.tone ?? 0);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState<string | null>(null);

    useEffect(() => {
        if (agent) {
            setName(agent.name);
            setRole(agent.role_description || 'rex');
            setWelcomeMessage(agent.welcome_message ?? '');
            setTone(agent.tone ?? 0);
        }
    }, [agent]);

    const onSubmit = async () => {
        setBusy(true);
        setErr(null);
        try {
            if (mode === 'create') {
                const payload: AgentCreatePayload = {
                    name: name.trim(),
                    role_description: role.trim(),
                    welcome_message: welcomeMessage.trim() || undefined,
                    tone,
                };
                await crossStoreAgentsApi.create(tenantId, payload);
            } else if (agent) {
                const payload: AgentUpdatePayload = {
                    name: name.trim(),
                    role_description: role.trim(),
                    welcome_message: welcomeMessage.trim(),
                    tone,
                };
                await crossStoreAgentsApi.update(tenantId, agent.id, payload);
            }
            onSaved();
        } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const submitDisabled = busy || !name.trim() || !role.trim();

    return (
        <div
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
            style={{
                position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
                display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
            }}
        >
            <div style={{
                background: 'var(--bg-secondary)', border: '1px solid var(--border)',
                borderRadius: 10, padding: 24, width: 'min(560px, 90vw)',
                display: 'flex', flexDirection: 'column', gap: 14,
            }}>
                <h2 style={{ margin: 0, fontSize: 18 }}>
                    {mode === 'create'
                        ? t('admin.agents.create.title', 'Create agent')
                        : t('admin.agents.edit.title', 'Edit agent')}
                </h2>

                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{t('admin.agents.field.name', 'Name')}</span>
                    <input
                        type="text"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        maxLength={100}
                        style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-primary)', color: 'var(--text-primary)' }}
                    />
                </label>

                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{t('admin.agents.field.role', 'Role')}</span>
                    <select
                        value={role}
                        onChange={(e) => setRole(e.target.value)}
                        style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-primary)', color: 'var(--text-primary)' }}
                    >
                        <option value="rex">{t('admin.agents.role.rex', 'Rex (Wave-1 default)')}</option>
                    </select>
                </label>

                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{t('admin.agents.field.welcome', 'Welcome message')}</span>
                    <textarea
                        value={welcomeMessage}
                        onChange={(e) => setWelcomeMessage(e.target.value)}
                        maxLength={500}
                        rows={3}
                        placeholder={t('admin.agents.field.welcomePlaceholder', 'Hello! How can I help you today?')}
                        style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-primary)', color: 'var(--text-primary)', resize: 'vertical', fontFamily: 'inherit' }}
                    />
                    <span style={{ fontSize: 11, color: 'var(--text-tertiary)', textAlign: 'right' }}>{welcomeMessage.length}/500</span>
                </label>

                <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{t('admin.agents.field.tone', 'Tone')}</span>
                    <select
                        value={tone}
                        onChange={(e) => setTone(parseInt(e.target.value, 10))}
                        style={{ padding: '8px 10px', border: '1px solid var(--border)', borderRadius: 6, background: 'var(--bg-primary)', color: 'var(--text-primary)' }}
                    >
                        {TONE_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>{t(opt.labelKey, opt.fallback)}</option>
                        ))}
                    </select>
                </label>

                {err && <div style={{ color: '#ef4444', fontSize: 12 }}>{err}</div>}

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 6 }}>
                    <button
                        onClick={onClose}
                        disabled={busy}
                        style={{ padding: '8px 14px', background: 'none', border: '1px solid var(--border)', borderRadius: 6, cursor: busy ? 'wait' : 'pointer', color: 'var(--text-primary)' }}
                    >
                        {t('common.cancel', 'Cancel')}
                    </button>
                    <button
                        onClick={onSubmit}
                        disabled={submitDisabled}
                        style={{
                            padding: '8px 14px',
                            background: submitDisabled ? 'var(--border)' : 'var(--accent, #3b82f6)',
                            border: 'none', borderRadius: 6,
                            cursor: submitDisabled ? 'not-allowed' : 'pointer',
                            color: '#fff', fontWeight: 600,
                        }}
                    >
                        {busy
                            ? t('common.saving', 'Saving…')
                            : mode === 'create'
                                ? t('admin.agents.create.submit', 'Create')
                                : t('admin.agents.edit.submit', 'Save changes')}
                    </button>
                </div>
            </div>
        </div>
    );
}

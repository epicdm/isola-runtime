/** API service layer */

import type { Agent, TokenResponse, User, Task, ChatMessage } from '../types';

const API_BASE = '/api';

async function request<T>(url: string, options: RequestInit = {}): Promise<T> {
    const token = localStorage.getItem('token');
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
    };

    const res = await fetch(`${API_BASE}${url}`, { ...options, headers });

    if (!res.ok) {
        // Auto-logout on expired/invalid token (but not on auth endpoints — let them show errors)
        const isAuthEndpoint = url.startsWith('/auth/login')
            || url.startsWith('/auth/register')
            || url.startsWith('/auth/forgot-password')
            || url.startsWith('/auth/reset-password');
        if (res.status === 401 && !isAuthEndpoint) {
            localStorage.removeItem('token');
            localStorage.removeItem('user');
            window.location.href = '/login';
            throw new Error('Session expired');
        }
        const bodyText = await res.text();
        let error: { detail?: unknown };
        try {
            error = bodyText ? JSON.parse(bodyText) : {};
        } catch {
            const snippet = bodyText.trim().slice(0, 280);
            error = {
                detail: snippet || `HTTP ${res.status} ${res.statusText || ''}`.trim(),
            };
        }
        // Pydantic validation errors return detail as an array of objects
        const fieldLabels: Record<string, string> = {
            name: '名称',
            role_description: '角色描述',
            agent_type: '智能体类型',
            primary_model_id: '主模型',
            max_tokens_per_day: '每日 Token 上限',
            max_tokens_per_month: '每月 Token 上限',
        };
        let message = '';
        if (Array.isArray(error.detail)) {
            message = error.detail
                .map((e: any) => {
                    const field = e.loc?.slice(-1)[0] || '';
                    const label = fieldLabels[field] || field;
                    return label ? `${label}: ${e.msg}` : e.msg;
                })
                .join('; ');
        } else if (typeof error.detail === 'object' && error.detail !== null) {
            // Structured error detail (e.g., NeedsVerificationResponse)
            message = (error.detail as Record<string, any>).message || `HTTP ${res.status}`;
        } else {
            const d = error.detail;
            if (typeof d === 'string') message = d;
            else if (d != null && typeof d === 'object') message = JSON.stringify(d);
            else message = `HTTP ${res.status}`;
        }

        const apiErr: any = new Error(message);
        apiErr.status = res.status;
        apiErr.detail = error.detail;
        throw apiErr;
    }

    if (res.status === 204) return undefined as T;
    return res.json();
}

/** Legacy/Internal generic fetcher */
export const fetchJson = request;

async function uploadFile(url: string, file: File, extraFields?: Record<string, string>): Promise<any> {
    const token = localStorage.getItem('token');
    const formData = new FormData();
    formData.append('file', file);
    if (extraFields) {
        for (const [k, v] of Object.entries(extraFields)) {
            formData.append(k, v);
        }
    }
    const res = await fetch(`${API_BASE}${url}`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
    });
    if (!res.ok) {
        const error = await res.json().catch(() => ({ detail: 'Upload failed' }));
        throw new Error(error.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// Upload with progress tracking via XMLHttpRequest.
// Returns { promise, abort } — call abort() to cancel the upload.
// Progress callback: 0-100 = upload phase, 101 = processing phase (server is parsing the file).
export function uploadFileWithProgress(
    url: string,
    file: File,
    onProgress?: (percent: number) => void,
    extraFields?: Record<string, string>,
    timeoutMs: number = 120_000,
): { promise: Promise<any>; abort: () => void } {
    const xhr = new XMLHttpRequest();
    const promise = new Promise<any>((resolve, reject) => {
        const token = localStorage.getItem('token');
        const formData = new FormData();
        formData.append('file', file);
        if (extraFields) {
            for (const [k, v] of Object.entries(extraFields)) {
                formData.append(k, v);
            }
        }
        xhr.open('POST', `${API_BASE}${url}`);
        if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

        // Upload phase: 0-100%
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && onProgress) {
                onProgress(Math.round((e.loaded / e.total) * 100));
            }
        };
        // Upload bytes finished → enter processing phase
        xhr.upload.onload = () => {
            if (onProgress) onProgress(101); // 101 = "processing" sentinel
        };

        xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                try { resolve(JSON.parse(xhr.responseText)); } catch { resolve(undefined); }
            } else {
                try {
                    const err = JSON.parse(xhr.responseText);
                    reject(new Error(err.detail || `HTTP ${xhr.status}`));
                } catch { reject(new Error(`HTTP ${xhr.status}`)); }
            }
        };
        xhr.onerror = () => reject(new Error('Network error'));
        xhr.ontimeout = () => reject(new Error('Upload timed out'));
        xhr.onabort = () => reject(new Error('Upload cancelled'));
        xhr.timeout = timeoutMs;
        xhr.send(formData);
    });
    return { promise, abort: () => xhr.abort() };
}

// ─── Auth ─────────────────────────────────────────────
export const authApi = {
    register: (data: { username?: string; email: string; password: string; display_name: string; invitation_code?: string; provider?: string; provider_code?: string }) =>
        request<{ user_id: string; email: string; access_token: string; message: string; user?: any; needs_company_setup: boolean }>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),

    login: (data: { login_identifier: string; password: string; tenant_id?: string }) =>
        request<TokenResponse | { requires_tenant_selection: boolean; login_identifier: string; tenants: any[] }>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),

    forgotPassword: (data: { email: string }) =>
        request<{ ok: boolean; message: string }>('/auth/forgot-password', { method: 'POST', body: JSON.stringify(data) }),

    resetPassword: (data: { token: string; new_password: string }) =>
        request<{ ok: boolean }>('/auth/reset-password', { method: 'POST', body: JSON.stringify(data) }),

    emailHint: (username: string) =>
        request<{ hint: string }>(`/auth/email-hint?username=${encodeURIComponent(username)}`),

    me: () => request<User>('/auth/me'),

    updateMe: (data: Partial<User>) =>
        request<User>('/auth/me', { method: 'PATCH', body: JSON.stringify(data) }),

    verifyEmail: (token: string) =>
        request<{ ok: boolean; message: string; access_token: string; user: User; needs_company_setup: boolean }>('/auth/verify-email', { method: 'POST', body: JSON.stringify({ token }) }),

    resendVerification: (email: string) =>
        request<{ ok: boolean; message: string }>('/auth/resend-verification', { method: 'POST', body: JSON.stringify({ email }) }),

    getMyTenants: () =>
        request<any[]>('/auth/my-tenants'),

    switchTenant: (tenantId: string) =>
        request<{ access_token: string; redirect_url?: string; message?: string }>('/auth/switch-tenant', { method: 'POST', body: JSON.stringify({ tenant_id: tenantId }) }),
};

// ─── Tenants ──────────────────────────────────────────
export const tenantApi = {
    selfCreate: (data: { name: string }) =>
        request<any>('/tenants/self-create', { method: 'POST', body: JSON.stringify(data) }),

    join: (invitationCode: string) =>
        request<any>('/tenants/join', { method: 'POST', body: JSON.stringify({ invitation_code: invitationCode }) }),

    registrationConfig: () =>
        request<{ allow_self_create_company: boolean }>('/tenants/registration-config'),

    resolveByDomain: (domain: string) =>
        request<any>(`/tenants/resolve-by-domain?domain=${encodeURIComponent(domain)}`),
};

export const adminApi = {
    listCompanies: () =>
        request<any[]>('/admin/companies'),

    createCompany: (data: { name: string }) =>
        request<any>('/admin/companies', { method: 'POST', body: JSON.stringify(data) }),

    updateCompany: (id: string, data: any) =>
        request<any>(`/tenants/${id}`, { method: 'PUT', body: JSON.stringify(data) }),

    toggleCompany: (id: string) =>
        request<any>(`/admin/companies/${id}/toggle`, { method: 'PUT' }),

    getPlatformSettings: () =>
        request<any>('/admin/platform-settings'),

    updatePlatformSettings: (data: any) =>
        request<any>('/admin/platform-settings', { method: 'PUT', body: JSON.stringify(data) }),
};

// ─── Cross-store admin (L4) ───────────────────────────
// Aggregates BFF tenant_registry + local Clawith tenants for the operator
// console. Auth: Bearer JWT + platform_admin. The runtime backend acts as
// ambassador to BFF /api/internal/cross-store/* (X-Internal-Secret).
export const crossStoreApi = {
    listTenants: (includeTest = false) =>
        request<{ total: number; tenants: any[]; hiddenCount?: number }>(
            `/admin/cross-store/tenants${includeTest ? '?includeTest=true' : ''}`,
        ),

    getTenant: (tenantId: string) =>
        request<{ bff: any; local: any | null; agents: any[] }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}`,
        ),

    listQueue: () =>
        request<{ total: number; byKind: Record<string, number>; items: QueueItem[] }>(
            '/admin/cross-store/operator-queue',
        ),

    resolveAction: (tenantId: string, actionKind: string, resolutionPayload?: unknown) =>
        request<{ tenantId: string; resolved: string; remaining: string[] }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/resolve-action`,
            {
                method: 'POST',
                body: JSON.stringify({ actionKind, ...(resolutionPayload !== undefined ? { resolutionPayload } : {}) }),
            },
        ),

    // -- drift retreat S3: provision new tenant --
    provision: (payload: {
        businessName: string;
        ownerEmail: string;
        plan?: string;
        didSource?: string;
        agentTemplate?: string;
        tone?: string;
    }) =>
        request<{ status: string; tenantId: string; businessName: string }>(
            "/admin/cross-store/provision",
            { method: "POST", body: JSON.stringify(payload) },
        ),

    // -- drift retreat S4: DID edit --
    patchDid: (
        tenantId: string,
        didId: string,
        payload: { channel?: string; agentId?: string | null },
    ) =>
        request<{ did: { id: string; didNumber: string; magnusDidId: string | null; status: string; channel: string; agentId: string | null; createdAt: string; updatedAt: string } }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/dids/${encodeURIComponent(didId)}`,
            { method: "PATCH", body: JSON.stringify(payload) },
        ),

        // ── S5 (2026-05-04): SOUL editor + skill attachment ambassador ──
    getAgentSoul: (tenantId: string, agentId: string) =>
        request<{ content: string; source: string; agent_id: string; tenant_id: string }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/soul`,
        ),

    putAgentSoul: (tenantId: string, agentId: string, content: string) =>
        request<{ content: string; content_sha256: string; agent_id: string; tenant_id: string; cache_refreshed: boolean; source: string }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/soul`,
            { method: 'PUT', body: JSON.stringify({ content }) },
        ),

    getAgentSkills: (tenantId: string, agentId: string) =>
        request<{ available: SkillCatalogEntry[]; attached: string[]; agent_id: string; tenant_id: string }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/skills`,
        ),

    putAgentSkills: (tenantId: string, agentId: string, desired: string[]) =>
        request<{ desired: string[]; agent_id: string; tenant_id: string; source: string }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/skills`,
            { method: 'PUT', body: JSON.stringify({ desired }) },
        ),

    // ── S6 R28-revised + R34 + R35 (2026-05-04 night): policy + channels ──
    // /policy: 9-key autonomy whitelist + escalation_keywords + channel_binding RO
    //          + business_hours_readonly (from SOUL frontmatter)
    // /channels: ChannelConfig CRUD; LIST + DELETE table-direct; POST + PATCH
    //            dispatch to per-channel handlers via internal HTTP (R34 hybrid).
    // 9-key autonomy whitelist; map-only orphans NEVER returned.
    getAgentPolicy: (tenantId: string, agentId: string) =>
        request<AgentPolicy>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/policy`,
        ),

    patchAgentPolicy: (
        tenantId: string,
        agentId: string,
        body: { autonomy_policy?: Record<string, 'L1' | 'L2' | 'L3'>; escalation_keywords?: string[] },
    ) =>
        request<{
            autonomy_policy: AgentAutonomyView;
            escalation_keywords: string[];
            agent_id: string;
            tenant_id: string;
            updated_at: string | null;
        }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/policy`,
            { method: 'PATCH', body: JSON.stringify(body) },
        ),

    listAgentChannels: (tenantId: string, agentId: string) =>
        request<{
            agent_id: string;
            tenant_id: string;
            channels: AgentChannelRow[];
            supported_channel_types: string[];
        }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/channels`,
        ),

    createAgentChannel: (tenantId: string, agentId: string, body: Record<string, unknown>) =>
        request<AgentChannelRow>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/channels`,
            { method: 'POST', body: JSON.stringify(body) },
        ),

    patchAgentChannel: (tenantId: string, agentId: string, channelId: string, body: Record<string, unknown>) =>
        request<AgentChannelRow>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/channels/${encodeURIComponent(channelId)}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        ),

    deleteAgentChannel: (tenantId: string, agentId: string, channelId: string) =>
        request<void>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/channels/${encodeURIComponent(channelId)}`,
            { method: 'DELETE' },
        ),

    // ── L4 S7 R42 (2026-05-05): cross-store tenant retire orchestrator ──
    // DELETE /api/admin/cross-store/tenants/{tid} runs Paperclip archive →
    // Clawith soft-retire → BFF NULL FK → audit. Returns 200 + step trail
    // on full success, 409 partial_state on mid-sequence failure (idempotent
    // retry recovery — caller can re-call DELETE; succeeded steps no-op).
    retireCrossStoreTenant: (tenantId: string) =>
        request<RetireSuccess>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}`,
            { method: 'DELETE' },
        ),
};

// S7 R42: cross-store retire envelope (success + partial-state failure shapes).
export type RetireSuccess = {
    bff_tenant_id: string;
    paperclip_company_id: string | null;
    clawith_tenant_id: string;
    retired: true;
    succeeded_steps: string[];
};

export type RetireError = {
    error: 'partial_state';
    succeeded: string[];
    failed: string;
    details: string;
};

// S6 R35: 9-key autonomy view with per-key enforcement-status badge.
// Map-only orphans (web_search/execute_code/send_feishu_message) NEVER returned.
export type AgentAutonomyKey = {
    key: string;
    value: 'L1' | 'L2' | 'L3';
    enforcement_status: 'enforced' | 'scaffolded';
};

export type AgentAutonomyView = {
    keys: AgentAutonomyKey[];
};

export type AgentChannelBinding = {
    whatsapp: {
        whatsappStatus: string | null;
        displayPhone: string | null;
        waPhoneNumberId: string | null;
        wabaId: string | null;
        ownerPhone: string | null;
        didNumber: string | null;
        magnusDidId: string | null;
        waProvisioningJobId: string | null;
        waProvisioningState: string | null;
        whatsappActivatedAt: string | null;
    } | null;
    voice: {
        magnusDidId?: string | null;
        didNumber?: string | null;
    } | null;
};

export type AgentBusinessHours = {
    source: string;
    hours_weekday?: string;
    hours_saturday?: string;
    hours_sunday?: string;
} | null;

export type AgentPolicy = {
    autonomy_policy: AgentAutonomyView;
    escalation_keywords: string[];
    channel_binding: AgentChannelBinding;
    business_hours_readonly: AgentBusinessHours;
    agent_id: string;
    tenant_id: string;
};

// S6 R28-revised: ChannelConfig row metadata. NEVER includes credentials.
// display_name is operator-set label stored in extra_config.display_name.
// app_id is identifier (not credential) — plaintext and safe to surface.
export type AgentChannelRow = {
    channel_id: string;
    channel_type: 'slack' | 'discord' | 'microsoft_teams' | 'whatsapp';
    is_configured: boolean;
    is_connected: boolean;
    display_name: string | null;
    app_id: string | null;
    created_at: string | null;
    updated_at: string | null;
};

export type SkillCatalogEntry = {
    key: string;
    slug: string;
    description: string;
    id: string;
};

export type QueueItem = {
    tenantId: string;
    businessName: string | null;
    status: string;
    kind: string;
    payload: unknown;
};

// L4 S4: agent CRUD scoped to a cross-store tenant. Path is
// /admin/cross-store/tenants/{tenantId}/agents/...; auth is Bearer +
// platform_admin (matches list/detail/queue family).

export type AgentRow = {
    id: string;
    name: string;
    agent_type: string;
    role_description: string;
    welcome_message: string | null;
    tone: number | null;
    status: string;
    owner_phone: string | null;
    paperclip_agent_id: string | null;
    paperclip_company_id: string | null;
    container_port: number | null;
    retired_at: string | null;
    retired_by: string | null;
    created_at: string | null;
    updated_at: string | null;
};

export type AgentCreatePayload = {
    name: string;
    role_description?: string;
    welcome_message?: string;
    tone?: number;
};

export type AgentUpdatePayload = Partial<AgentCreatePayload>;

export const crossStoreAgentsApi = {
    list: (tenantId: string, includeRetired = false) =>
        request<{ total: number; agents: AgentRow[] }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents${includeRetired ? '?includeRetired=true' : ''}`,
        ),

    create: (tenantId: string, payload: AgentCreatePayload) =>
        request<AgentRow>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents`,
            { method: 'POST', body: JSON.stringify(payload) },
        ),

    get: (tenantId: string, agentId: string) =>
        request<AgentRow>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}`,
        ),

    update: (tenantId: string, agentId: string, payload: AgentUpdatePayload) =>
        request<AgentRow>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}`,
            { method: 'PATCH', body: JSON.stringify(payload) },
        ),

    retire: (tenantId: string, agentId: string) =>
        request<{ id: string; retired_at: string; retired_by: string }>(
            `/admin/cross-store/tenants/${encodeURIComponent(tenantId)}/agents/${encodeURIComponent(agentId)}/retire`,
            { method: 'POST' },
        ),
};

// ─── Agents ───────────────────────────────────────────
export const agentApi = {
    list: (tenantId?: string) => request<Agent[]>(`/agents/${tenantId ? `?tenant_id=${tenantId}` : ''}`),

    get: (id: string) => request<Agent>(`/agents/${id}`),

    create: (data: any) =>
        request<any>('/agents/', { method: 'POST', body: JSON.stringify(data) }),

    update: (id: string, data: Partial<Agent>) =>
        request<Agent>(`/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (id: string) =>
        request<void>(`/agents/${id}`, { method: 'DELETE' }),

    start: (id: string) =>
        request<Agent>(`/agents/${id}/start`, { method: 'POST' }),

    stop: (id: string) =>
        request<Agent>(`/agents/${id}/stop`, { method: 'POST' }),

    metrics: (id: string) =>
        request<any>(`/agents/${id}/metrics`),

    collaborators: (id: string) =>
        request<any[]>(`/agents/${id}/collaborators`),

    templates: () =>
        request<any[]>('/agents/templates'),

    // OpenClaw gateway
    generateApiKey: (id: string) =>
        request<{ api_key: string; message: string }>(`/agents/${id}/api-key`, { method: 'POST' }),

    gatewayMessages: (id: string) =>
        request<any[]>(`/agents/${id}/gateway-messages`),
};

// ─── Tasks ────────────────────────────────────────────
export const taskApi = {
    list: (agentId: string, status?: string, type?: string) => {
        const params = new URLSearchParams();
        if (status) params.set('status_filter', status);
        if (type) params.set('type_filter', type);
        return request<Task[]>(`/agents/${agentId}/tasks/?${params}`);
    },

    create: (agentId: string, data: any) =>
        request<Task>(`/agents/${agentId}/tasks/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, taskId: string, data: Partial<Task>) =>
        request<Task>(`/agents/${agentId}/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    getLogs: (agentId: string, taskId: string) =>
        request<{ id: string; task_id: string; content: string; created_at: string }[]>(`/agents/${agentId}/tasks/${taskId}/logs`),

    trigger: (agentId: string, taskId: string) =>
        request<any>(`/agents/${agentId}/tasks/${taskId}/trigger`, { method: 'POST' }),
};

// ─── Files ────────────────────────────────────────────
export const fileApi = {
    list: (agentId: string, path: string = '') =>
        request<any[]>(`/agents/${agentId}/files/?path=${encodeURIComponent(path)}`),

    read: (agentId: string, path: string) =>
        request<{ path: string; content: string }>(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`),

    write: (agentId: string, path: string, content: string) =>
        request(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            body: JSON.stringify({ content }),
        }),

    delete: (agentId: string, path: string) =>
        request(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, {
            method: 'DELETE',
        }),

    upload: (agentId: string, file: File, path: string = 'workspace/knowledge_base', onProgress?: (pct: number) => void) =>
        onProgress
            ? uploadFileWithProgress(`/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`, file, onProgress).promise
            : uploadFile(`/agents/${agentId}/files/upload?path=${encodeURIComponent(path)}`, file),

    importSkill: (agentId: string, skillId: string) =>
        request<any>(`/agents/${agentId}/files/import-skill`, {
            method: 'POST',
            body: JSON.stringify({ skill_id: skillId }),
        }),

    downloadUrl: (agentId: string, path: string) => {
        const token = localStorage.getItem('token');
        return `${API_BASE}/agents/${agentId}/files/download?path=${encodeURIComponent(path)}&token=${token}`;
    },
};

// ─── Channel Config ───────────────────────────────────
export const channelApi = {
    get: (agentId: string) =>
        request<any>(`/agents/${agentId}/channel`).catch(() => null),

    create: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/channel`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/channel`, { method: 'PUT', body: JSON.stringify(data) }),

    delete: (agentId: string) =>
        request<void>(`/agents/${agentId}/channel`, { method: 'DELETE' }),

    webhookUrl: (agentId: string) =>
        request<{ webhook_url: string }>(`/agents/${agentId}/channel/webhook-url`).catch(() => null),
};

// ─── Enterprise ───────────────────────────────────────
export const enterpriseApi = {
    llmModels: () => {
        const tid = localStorage.getItem('current_tenant_id');
        return request<any[]>(`/enterprise/llm-models${tid ? `?tenant_id=${tid}` : ''}`);
    },
    templates: () => request<any[]>('/agents/templates'),

    // Enterprise Knowledge Base
    kbFiles: (path: string = '') =>
        request<any[]>(`/enterprise/knowledge-base/files?path=${encodeURIComponent(path)}`),

    kbUpload: (file: File, subPath: string = '') =>
        uploadFile(`/enterprise/knowledge-base/upload?sub_path=${encodeURIComponent(subPath)}`, file),

    kbRead: (path: string) =>
        request<{ path: string; content: string }>(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`),

    kbWrite: (path: string, content: string) =>
        request(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`, {
            method: 'PUT',
            body: JSON.stringify({ content }),
        }),

    kbDelete: (path: string) =>
        request(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`, {
            method: 'DELETE',
        }),
};

// ─── Activity Logs ────────────────────────────────────
export const activityApi = {
    list: (agentId: string, limit = 50) =>
        request<any[]>(`/agents/${agentId}/activity?limit=${limit}`),
};

// ─── Messages ─────────────────────────────────────────
export const messageApi = {
    inbox: (limit = 50) =>
        request<any[]>(`/messages/inbox?limit=${limit}`),

    unreadCount: () =>
        request<{ unread_count: number }>('/messages/unread-count'),

    markRead: (messageId: string) =>
        request<void>(`/messages/${messageId}/read`, { method: 'PUT' }),

    markAllRead: () =>
        request<void>('/messages/read-all', { method: 'PUT' }),
};

// ─── Schedules ────────────────────────────────────────
export const scheduleApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/schedules/`),

    create: (agentId: string, data: { name: string; instruction: string; cron_expr: string }) =>
        request<any>(`/agents/${agentId}/schedules/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, scheduleId: string, data: any) =>
        request<any>(`/agents/${agentId}/schedules/${scheduleId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (agentId: string, scheduleId: string) =>
        request<void>(`/agents/${agentId}/schedules/${scheduleId}`, { method: 'DELETE' }),

    trigger: (agentId: string, scheduleId: string) =>
        request<any>(`/agents/${agentId}/schedules/${scheduleId}/run`, { method: 'POST' }),

    history: (agentId: string, scheduleId: string) =>
        request<any[]>(`/agents/${agentId}/schedules/${scheduleId}/history`),
};

// ─── Skills ───────────────────────────────────────────
export const skillApi = {
    list: () => request<any[]>('/skills/'),
    get: (id: string) => request<any>(`/skills/${id}`),
    create: (data: any) =>
        request<any>('/skills/', { method: 'POST', body: JSON.stringify(data) }),
    update: (id: string, data: any) =>
        request<any>(`/skills/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: string) =>
        request<void>(`/skills/${id}`, { method: 'DELETE' }),
    // Path-based browse for FileBrowser
    browse: {
        list: (path: string) => request<any[]>(`/skills/browse/list?path=${encodeURIComponent(path)}`),
        read: (path: string) => request<{ content: string }>(`/skills/browse/read?path=${encodeURIComponent(path)}`),
        write: (path: string, content: string) =>
            request<any>('/skills/browse/write', { method: 'PUT', body: JSON.stringify({ path, content }) }),
        delete: (path: string) =>
            request<any>(`/skills/browse/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
    },
    // ClawHub marketplace integration
    clawhub: {
        search: (q: string) => request<any[]>(`/skills/clawhub/search?q=${encodeURIComponent(q)}`),
        detail: (slug: string) => request<any>(`/skills/clawhub/detail/${slug}`),
        install: (slug: string) => request<any>('/skills/clawhub/install', { method: 'POST', body: JSON.stringify({ slug }) }),
    },
    importFromUrl: (url: string) =>
        request<any>('/skills/import-from-url', { method: 'POST', body: JSON.stringify({ url }) }),
    previewUrl: (url: string) =>
        request<any>('/skills/import-from-url/preview', { method: 'POST', body: JSON.stringify({ url }) }),
    // Tenant-level settings
    settings: {
        getToken: () => request<{ configured: boolean; source: string; masked: string; clawhub_configured: boolean; clawhub_masked: string }>('/skills/settings/token'),
        setToken: (github_token: string) =>
            request<any>('/skills/settings/token', { method: 'PUT', body: JSON.stringify({ github_token }) }),
        setClawhubKey: (clawhub_key: string) =>
            request<any>('/skills/settings/token', { method: 'PUT', body: JSON.stringify({ clawhub_key }) }),
    },
    // Agent-level import (writes to agent workspace)
    agentImport: {
        fromClawhub: (agentId: string, slug: string) =>
            request<any>(`/agents/${agentId}/files/import-from-clawhub`, { method: 'POST', body: JSON.stringify({ slug }) }),
        fromUrl: (agentId: string, url: string) =>
            request<any>(`/agents/${agentId}/files/import-from-url`, { method: 'POST', body: JSON.stringify({ url }) }),
    },
};

// ─── Triggers (Aware Engine) ──────────────────────────
export const triggerApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/triggers`),

    update: (agentId: string, triggerId: string, data: any) =>
        request<any>(`/agents/${agentId}/triggers/${triggerId}`, { method: 'PATCH', body: JSON.stringify(data) }),

    delete: (agentId: string, triggerId: string) =>
        request<void>(`/agents/${agentId}/triggers/${triggerId}`, { method: 'DELETE' }),
};

// ─── Agent Credentials ────────────────────────────────
export const credentialApi = {
    list: (agentId: string) =>
        request<any[]>(`/agents/${agentId}/credentials/`),

    create: (agentId: string, data: any) =>
        request<any>(`/agents/${agentId}/credentials/`, { method: 'POST', body: JSON.stringify(data) }),

    update: (agentId: string, credentialId: string, data: any) =>
        request<any>(`/agents/${agentId}/credentials/${credentialId}`, { method: 'PUT', body: JSON.stringify(data) }),

    delete: (agentId: string, credentialId: string) =>
        request<void>(`/agents/${agentId}/credentials/${credentialId}`, { method: 'DELETE' }),
};

// ─── AgentBay Take Control ────────────────────────────
export const controlApi = {
    click: (agentId: string, data: { session_id: string; x: number; y: number; button?: string }) =>
        request<any>(`/agents/${agentId}/control/click`, { method: 'POST', body: JSON.stringify(data) }),

    type: (agentId: string, data: { session_id: string; text: string }) =>
        request<any>(`/agents/${agentId}/control/type`, { method: 'POST', body: JSON.stringify(data) }),

    pressKeys: (agentId: string, data: { session_id: string; keys: string[] }) =>
        request<any>(`/agents/${agentId}/control/press_keys`, { method: 'POST', body: JSON.stringify(data) }),

    /** Simulate a natural human drag (Bezier curve trajectory) for slider CAPTCHAs. */
    drag: (agentId: string, data: { session_id: string; from_x: number; from_y: number; to_x: number; to_y: number; duration_ms?: number }) =>
        request<any>(`/agents/${agentId}/control/drag`, { method: 'POST', body: JSON.stringify(data) }),

    /** Get the current active page URL from the browser session (for auto-populating domain). */
    currentUrl: (agentId: string, data: { session_id: string }) =>
        request<{ status: string; url: string }>(`/agents/${agentId}/control/current-url`, { method: 'POST', body: JSON.stringify(data) }),

    screenshot: (agentId: string, data: { session_id: string }) =>
        request<any>(`/agents/${agentId}/control/screenshot`, { method: 'POST', body: JSON.stringify(data) }),

    lock: (agentId: string, data: { session_id: string; platform_hint?: string; env_type?: string }) =>
        request<any>(`/agents/${agentId}/control/lock`, { method: 'POST', body: JSON.stringify(data) }),

    unlock: (agentId: string, data: { session_id: string; export_cookies?: boolean; platform_hint?: string }) =>
        request<any>(`/agents/${agentId}/control/unlock`, { method: 'POST', body: JSON.stringify(data) }),
};


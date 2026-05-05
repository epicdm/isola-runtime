/**
 * channelRegistry.ts — single source of truth for the channel config registry.
 *
 * R37 (2026-05-04 night): extracted from ChannelConfig.tsx to enable two
 * consumer surfaces with declarative filtering.
 *
 *   userSurface     = shown in user-facing ChannelConfig.tsx (agent owners
 *                     wiring up their own channels)
 *   operatorSurface = shown in OperatorChannelConfigModal.tsx (platform_admin
 *                     operators managing channels via /admin/cross-store)
 *
 * The two surfaces speak to different backends:
 *   user      → /api/agents/{aid}/{apiSlug}                (per-channel handler)
 *   operator  → /api/admin/cross-store/.../channels        (admin endpoints
 *                that dispatch to per-channel handlers per R34)
 *
 * Operator surface only includes channels the backend dispatch table covers
 * (slack, discord, microsoft_teams, whatsapp). Other channels remain user-
 * surface-only until per-channel admin support lands in W2.
 *
 * R36 reminder: ChannelConfig credentials persist plaintext at rest until
 * the pre-launch encryption gate. Operator UI surfaces a Wave-1 advisory
 * (handled in OperatorChannelConfigModal, not here).
 */

import type { ReactNode } from 'react';

export interface ChannelField {
    key: string;
    label: string;
    placeholder?: string;
    type?: 'text' | 'password';
    required?: boolean;
}

export interface GuideConfig {
    prefix: string;
    steps: number;
    noteKey?: string;
}

export interface ChannelDef {
    id: string;
    icon: ReactNode;
    nameKey: string;
    nameFallback: string;
    desc: string;
    apiSlug?: string;
    useChannelApi?: boolean;
    fields: ChannelField[];
    guide: GuideConfig;
    connectionMode?: boolean;
    wsGuide?: GuideConfig;
    showPermJson?: boolean;
    webhookLabel?: string;
    editOnly?: boolean;
    wsFields?: ChannelField[];
    hasTestConnection?: boolean;

    // R37 surface flags
    userSurface: boolean;
    operatorSurface: boolean;

    // Backend channel_type enum value (for operator dispatch). Differs from
    // `id` only for `teams` → `microsoft_teams`. Null for channels without
    // backend handler.
    backendChannelType?: 'slack' | 'discord' | 'microsoft_teams' | 'whatsapp';
}

// SVG / image icons. Mirrors ChannelConfig.tsx for visual parity.
const SlackIcon: ReactNode = <img src="/slack.png" alt="Slack" width="20" height="20" style={{ borderRadius: '4px' }} />;
const DiscordIcon: ReactNode = <img src="/discord.png" alt="Discord" width="20" height="20" style={{ borderRadius: '4px' }} />;
const FeishuIcon: ReactNode = <img src="/feishu.png" alt="Feishu" width="20" height="20" style={{ borderRadius: '4px' }} />;
const TeamsIcon: ReactNode = <img src="/teams.png" alt="Teams" width="20" height="20" style={{ borderRadius: '4px' }} />;
const WeComIcon: ReactNode = <img src="/wecom.png" alt="WeCom" width="20" height="20" style={{ borderRadius: '4px' }} />;
const DingTalkIcon: ReactNode = <img src="/dingtalk.png" alt="DingTalk" width="20" height="20" style={{ borderRadius: '4px' }} />;
const AtlassianIcon: ReactNode = <img src="/atlassian.png" alt="Atlassian" width="20" height="20" style={{ borderRadius: '4px' }} />;
const AgentBayIcon: ReactNode = <span style={{ fontSize: '16px' }}>🌩️</span>;
const WhatsAppIcon: ReactNode = <span style={{ fontSize: '18px' }}>💬</span>;

export const ALL_CHANNELS: ChannelDef[] = [
    {
        id: 'slack',
        icon: SlackIcon,
        nameKey: 'common.channels.slack',
        nameFallback: 'Slack',
        desc: 'Slack Bot',
        apiSlug: 'slack-channel',
        fields: [
            { key: 'bot_token', label: 'Bot Token', placeholder: 'xoxb-...', type: 'password', required: true },
            { key: 'signing_secret', label: 'Signing Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.slack', steps: 8 },
        webhookLabel: 'Webhook URL (Event Subscriptions URL)',
        userSurface: true,
        operatorSurface: true,
        backendChannelType: 'slack',
    },
    {
        id: 'discord',
        icon: DiscordIcon,
        nameKey: 'common.channels.discord',
        nameFallback: 'Discord',
        desc: 'Gateway / Webhook',
        apiSlug: 'discord-channel',
        connectionMode: true,
        fields: [
            { key: 'application_id', label: 'Application ID', placeholder: '1234567890', required: true },
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
            { key: 'public_key', label: 'Public Key', required: true },
        ],
        wsFields: [
            { key: 'bot_token', label: 'Bot Token', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.discord', steps: 7 },
        wsGuide: { prefix: 'channelGuide.discord', steps: 4 },
        webhookLabel: 'Interactions Endpoint URL',
        userSurface: true,
        operatorSurface: true,
        backendChannelType: 'discord',
    },
    {
        id: 'teams',
        icon: TeamsIcon,
        nameKey: 'common.channels.teams',
        nameFallback: 'Microsoft Teams',
        desc: 'Teams Bot',
        apiSlug: 'teams-channel',
        fields: [
            { key: 'app_id', label: 'App ID (Client ID)', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret (Client Secret)', type: 'password', required: true },
            { key: 'tenant_id', label: 'channelGuide.teams.tenantId', placeholder: 'channelGuide.teams.tenantIdPlaceholder' },
        ],
        guide: { prefix: 'channelGuide.teams', steps: 5 },
        webhookLabel: 'Messaging Endpoint URL',
        userSurface: true,
        operatorSurface: true,
        backendChannelType: 'microsoft_teams',
    },
    {
        // R37: WhatsApp added Wave-1 for operator surface only. Customer-facing
        // WhatsApp activation runs via Embedded Signup at apps/isola (BFF-owned),
        // never through this user-facing component. operatorSurface:true exposes
        // the manual-paste path for WA-classic credentials.
        id: 'whatsapp',
        icon: WhatsAppIcon,
        nameKey: 'common.channels.whatsapp',
        nameFallback: 'WhatsApp Cloud API',
        desc: 'WhatsApp Business Cloud (manual credentials)',
        apiSlug: 'whatsapp-channel',
        fields: [
            { key: 'phone_number_id', label: 'Phone Number ID', placeholder: '975632242309171', required: true },
            { key: 'waba_id', label: 'WABA ID', placeholder: '272252189309178', required: true },
            { key: 'access_token', label: 'Access Token', type: 'password', required: true },
            { key: 'verify_token', label: 'Verify Token', type: 'password', required: true },
            { key: 'app_secret', label: 'App Secret (HMAC verify)', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.whatsapp', steps: 5 },
        webhookLabel: 'Webhook Callback URL',
        userSurface: false,
        operatorSurface: true,
        backendChannelType: 'whatsapp',
    },
    {
        id: 'feishu',
        icon: FeishuIcon,
        nameKey: 'agent.settings.channel.feishu',
        nameFallback: 'Feishu / Lark',
        desc: 'Feishu / Lark',
        useChannelApi: true,
        connectionMode: true,
        fields: [
            { key: 'app_id', label: 'App ID', placeholder: 'cli_xxxxxxxxxxxxxxxx', required: true },
            { key: 'app_secret', label: 'App Secret', type: 'password', required: true },
            { key: 'encrypt_key', label: 'Encrypt Key', type: 'password' },
        ],
        guide: { prefix: 'channelGuide.feishu', steps: 8 },
        wsGuide: { prefix: 'channelGuide.feishu', steps: 8 },
        showPermJson: true,
        webhookLabel: 'Webhook URL',
        userSurface: true,
        operatorSurface: false,
    },
    {
        id: 'wecom',
        icon: WeComIcon,
        nameKey: 'common.channels.wecom',
        nameFallback: 'WeCom',
        desc: 'WebSocket / Webhook',
        apiSlug: 'wecom-channel',
        connectionMode: true,
        fields: [
            { key: 'corp_id', label: 'CorpID', required: true },
            { key: 'wecom_agent_id', label: 'AgentID', required: true },
            { key: 'secret', label: 'Secret', type: 'password', required: true },
            { key: 'token', label: 'Token', required: true },
            { key: 'encoding_aes_key', label: 'EncodingAESKey', required: true },
        ],
        wsFields: [
            { key: 'bot_id', label: 'Bot ID', placeholder: 'aibXXXXXXXXXXXX', required: true },
            { key: 'bot_secret', label: 'Bot Secret', type: 'password', required: true },
        ],
        guide: { prefix: 'channelGuide.wecom', steps: 6 },
        wsGuide: { prefix: 'channelGuide.wecom', steps: 6 },
        webhookLabel: 'Webhook URL',
        userSurface: true,
        operatorSurface: false,
    },
    {
        id: 'dingtalk',
        icon: DingTalkIcon,
        nameKey: 'common.channels.dingtalk',
        nameFallback: 'DingTalk',
        desc: 'Stream Mode',
        apiSlug: 'dingtalk-channel',
        connectionMode: true,
        fields: [
            { key: 'app_key', label: 'AppKey', type: 'password', required: true },
            { key: 'app_secret', label: 'AppSecret', type: 'password', required: true },
            { key: 'agent_id', label: 'AgentId', type: 'text', placeholder: 'DingTalk应用AgentId(可选)', required: false },
        ],
        guide: { prefix: 'channelGuide.dingtalk', steps: 6 },
        webhookLabel: 'Webhook URL',
        userSurface: true,
        operatorSurface: false,
    },
    {
        id: 'atlassian',
        icon: AtlassianIcon,
        nameKey: 'common.channels.atlassian',
        nameFallback: 'Atlassian',
        desc: 'Jira / Confluence / Compass (Rovo MCP)',
        apiSlug: 'atlassian-channel',
        hasTestConnection: true,
        fields: [
            { key: 'api_key', label: 'API Key', type: 'password', required: true },
            { key: 'cloud_id', label: 'Cloud ID', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' },
        ],
        guide: { prefix: 'channelGuide.atlassian', steps: 5 },
        userSurface: true,
        operatorSurface: false,
    },
    {
        id: 'agentbay',
        icon: AgentBayIcon,
        nameKey: 'common.channels.agentbay',
        nameFallback: 'AgentBay',
        desc: 'Browser & Code Execution (阿里云)',
        apiSlug: 'agentbay-channel',
        hasTestConnection: true,
        editOnly: true,
        fields: [
            { key: 'api_key', label: 'API Key', type: 'password', required: true },
            { key: 'base_url', label: 'Base URL', placeholder: 'https://agentbay.aliyuncs.com/api/v1' },
        ],
        guide: { prefix: 'channelGuide.agentbay', steps: 3 },
        userSurface: true,
        operatorSurface: false,
    },
];

// Convenience filtered views.
export const USER_CHANNELS: ChannelDef[] = ALL_CHANNELS.filter((c) => c.userSurface);
export const OPERATOR_CHANNELS: ChannelDef[] = ALL_CHANNELS.filter((c) => c.operatorSurface);

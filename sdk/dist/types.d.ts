/**
 * TokenPak TypeScript SDK — Type Definitions
 * @module types
 */
export interface TokenPakConfig {
    /** Base URL of the TokenPak proxy app API (default: http://127.0.0.1:8766) */
    baseUrl?: string;
    /** Request timeout in milliseconds (default: 30000) */
    timeout?: number;
    /** Proxy key for authentication (sent as X-TPK-Key if configured) */
    apiKey?: string;
    /** HTTP headers to include in every request */
    headers?: Record<string, string>;
    /** Enable legacy SDK endpoints only when a custom server implements them */
    experimentalEndpoints?: boolean;
}
export type CompressionStrategy = 'heuristic' | 'semantic' | 'aggressive' | 'conservative';
export interface CompressOptions {
    /** Target token count (optional) */
    targetTokens?: number;
    /** Compression strategy (default: 'heuristic') */
    strategy?: CompressionStrategy;
    /** Whether to use cache for repeated inputs (default: true) */
    cache?: boolean;
    /** Preserve code blocks (default: true) */
    preserveCode?: boolean;
    /** Preserve headers/structure (default: true) */
    preserveStructure?: boolean;
}
export interface CompressResult {
    /** Original uncompressed text */
    originalText: string;
    /** Compressed output text */
    compressedText: string;
    /** Estimated token count before compression */
    originalTokens: number;
    /** Estimated token count after compression */
    compressedTokens: number;
    /** Percentage of tokens saved (0–100) */
    savingsPct: number;
    /** Whether this result was served from cache */
    cacheHit: boolean;
    /** Round-trip time in milliseconds */
    elapsedMs: number;
}
export interface ConversationMessage {
    role: 'system' | 'user' | 'assistant';
    content: string;
}
export interface ConversationCompressOptions {
    /** Number of recent messages to keep uncompressed (default: 3) */
    keepRecent?: number;
    /** Target total token budget (default: 4000) */
    targetTokens?: number;
}
export interface ConversationCompressResult {
    /** Compressed message array (ready to send to LLM) */
    messages: ConversationMessage[];
    /** Total token savings across conversation */
    totalSavings: number;
}
export interface CacheEntry {
    key: string;
    value: string;
    createdAt: Date;
    expiresAt: Date | null;
    hits: number;
}
export interface CacheStats {
    totalEntries: number;
    hitRate: number;
    totalHits: number;
    totalMisses: number;
    memoryUsageBytes: number;
}
export interface Block {
    id: string;
    type: string;
    content: string;
    tokenCount: number;
    metadata?: Record<string, unknown>;
}
export interface BlockRegistryStats {
    totalBlocks: number;
    totalTokens: number;
    blocksByType: Record<string, number>;
}
export interface TelemetryEvent {
    eventType: string;
    timestamp: Date;
    data: Record<string, unknown>;
    sessionId?: string;
    model?: string;
}
export interface TelemetryStats {
    totalEvents: number;
    totalCost: number;
    totalTokensIn: number;
    totalTokensOut: number;
    averageLatencyMs: number;
    modelBreakdown: Record<string, {
        calls: number;
        cost: number;
        tokensIn: number;
        tokensOut: number;
    }>;
}
export interface HealthStatus {
    status: 'ok' | 'degraded' | 'down';
    version: string;
    uptimeSeconds: number;
    stats: {
        requests: number;
        tokensSaved: number;
        cacheHits: number;
    };
}
export declare class TokenPakError extends Error {
    readonly statusCode?: number | undefined;
    readonly cause?: unknown | undefined;
    constructor(message: string, statusCode?: number | undefined, cause?: unknown | undefined);
}
export declare class TokenPakConnectionError extends TokenPakError {
    constructor(baseUrl: string, cause?: unknown);
}
export declare class TokenPakTimeoutError extends TokenPakError {
    constructor(timeoutMs: number);
}
export declare class TokenPakUnsupportedEndpointError extends TokenPakError {
    constructor(feature: string, path: string);
}
//# sourceMappingURL=types.d.ts.map
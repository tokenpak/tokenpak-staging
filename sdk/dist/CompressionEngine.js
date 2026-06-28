"use strict";
/**
 * TokenPak CompressionEngine
 * Wraps the shipped /tpk/v1/compress HTTP endpoint.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.CompressionEngine = void 0;
const client_1 = require("./client");
const types_1 = require("./types");
class CompressionEngine {
    constructor(config) {
        this.client = new client_1.TokenPakHttpClient(config);
        this.experimentalEndpoints = config?.experimentalEndpoints ?? false;
    }
    /**
     * Compress a single text string.
     * Requires the TokenPak API server to be running.
     *
     * @example
     * const engine = new CompressionEngine();
     * const result = await engine.compress("Long prompt text...");
     * console.log(`Saved ${result.savingsPct.toFixed(1)}% tokens`);
     */
    async compress(text, options = {}) {
        const body = {
            text,
            max_tokens: options.targetTokens,
            strategy: options.strategy ?? 'heuristic',
            cache: options.cache ?? true,
            preserve_code: options.preserveCode ?? true,
            preserve_structure: options.preserveStructure ?? true,
        };
        const raw = await this.client.post('/tpk/v1/compress', body);
        return {
            originalText: raw.original_text ?? text,
            compressedText: raw.compressed_text ?? raw.pruned_text ?? text,
            originalTokens: raw.original_tokens,
            compressedTokens: raw.compressed_tokens ?? raw.pruned_tokens ?? raw.original_tokens,
            savingsPct: raw.savings_pct ?? raw.reduction_pct ?? 0,
            cacheHit: raw.cache_hit ?? false,
            elapsedMs: raw.elapsed_ms ?? 0,
        };
    }
    /**
     * Compress a conversation history.
     * Keeps recent messages intact, compresses older context.
     *
     * @example
     * const engine = new CompressionEngine();
     * const result = await engine.compressConversation(messages, {
     *   keepRecent: 3,
     *   targetTokens: 4000,
     * });
     * const response = await openai.chat.completions.create({
     *   model: "gpt-4o",
     *   messages: result.messages,
     * });
     */
    async compressConversation(messages, options = {}) {
        if (!this.experimentalEndpoints) {
            throw new types_1.TokenPakUnsupportedEndpointError('CompressionEngine.compressConversation', '/compress/conversation');
        }
        const body = {
            messages,
            keep_recent: options.keepRecent ?? 3,
            target_tokens: options.targetTokens ?? 4000,
        };
        const raw = await this.client.post('/compress/conversation', body);
        return {
            messages: raw.messages,
            totalSavings: raw.total_savings,
        };
    }
}
exports.CompressionEngine = CompressionEngine;
//# sourceMappingURL=CompressionEngine.js.map
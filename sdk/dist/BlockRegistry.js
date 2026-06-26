"use strict";
/**
 * TokenPak BlockRegistry
 * Wraps experimental /blocks/* HTTP endpoints when a custom server provides them.
 * Blocks are reusable named content fragments that can be injected into prompts.
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.BlockRegistry = void 0;
const client_1 = require("./client");
const types_1 = require("./types");
function toBlock(raw) {
    return {
        id: raw.id,
        type: raw.type,
        content: raw.content,
        tokenCount: raw.token_count,
        metadata: raw.metadata,
    };
}
class BlockRegistry {
    constructor(config) {
        this.client = new client_1.TokenPakHttpClient(config);
        this.experimentalEndpoints = config?.experimentalEndpoints ?? false;
    }
    requireExperimentalEndpoint(path) {
        if (!this.experimentalEndpoints) {
            throw new types_1.TokenPakUnsupportedEndpointError('BlockRegistry', path);
        }
    }
    /**
     * Register a new named block.
     *
     * @example
     * await registry.register({
     *   id: 'system-prompt-v1',
     *   type: 'system',
     *   content: 'You are a helpful assistant...',
     * });
     */
    async register(block) {
        this.requireExperimentalEndpoint('/blocks');
        const raw = await this.client.post('/blocks', block);
        return toBlock(raw);
    }
    /**
     * Retrieve a block by id.
     */
    async get(id) {
        this.requireExperimentalEndpoint('/blocks/{id}');
        try {
            const raw = await this.client.get(`/blocks/${encodeURIComponent(id)}`);
            return toBlock(raw);
        }
        catch {
            return null;
        }
    }
    /**
     * List all registered blocks, optionally filtered by type.
     */
    async list(type) {
        const path = type ? `/blocks?type=${encodeURIComponent(type)}` : '/blocks';
        this.requireExperimentalEndpoint(path);
        const raws = await this.client.get(path);
        return raws.map(toBlock);
    }
    /**
     * Delete a block by id.
     */
    async delete(id) {
        this.requireExperimentalEndpoint('/blocks/{id}/delete');
        try {
            await this.client.post(`/blocks/${encodeURIComponent(id)}/delete`, {});
            return true;
        }
        catch {
            return false;
        }
    }
    /**
     * Registry statistics (block counts, token totals).
     */
    async stats() {
        this.requireExperimentalEndpoint('/blocks/stats');
        const raw = await this.client.get('/blocks/stats');
        return {
            totalBlocks: raw.total_blocks,
            totalTokens: raw.total_tokens,
            blocksByType: raw.blocks_by_type,
        };
    }
}
exports.BlockRegistry = BlockRegistry;
//# sourceMappingURL=BlockRegistry.js.map
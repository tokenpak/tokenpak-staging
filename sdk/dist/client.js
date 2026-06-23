"use strict";
/**
 * TokenPak HTTP Client
 * Low-level HTTP client for the TokenPak Python service.
 */
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.TokenPakHttpClient = void 0;
const axios_1 = __importDefault(require("axios"));
const types_1 = require("./types");
const DEFAULT_BASE_URL = 'http://127.0.0.1:8766';
const DEFAULT_TIMEOUT_MS = 30000;
class TokenPakHttpClient {
    constructor(config = {}) {
        this.baseUrl = config.baseUrl ?? DEFAULT_BASE_URL;
        this.http = axios_1.default.create({
            baseURL: this.baseUrl,
            timeout: config.timeout ?? DEFAULT_TIMEOUT_MS,
            headers: {
                'Content-Type': 'application/json',
                ...(config.apiKey ? { 'X-TPK-Key': config.apiKey } : {}),
                ...config.headers,
            },
        });
    }
    async get(path) {
        try {
            const resp = await this.http.get(path);
            return resp.data;
        }
        catch (err) {
            throw this.wrapError(err);
        }
    }
    async post(path, body) {
        try {
            const resp = await this.http.post(path, body);
            return resp.data;
        }
        catch (err) {
            throw this.wrapError(err);
        }
    }
    async health() {
        const raw = await this.get('/tpk/v1/health');
        return {
            status: 'ok',
            version: raw.version,
            uptimeSeconds: raw.uptime_s ?? 0,
            stats: {
                requests: 0,
                tokensSaved: 0,
                cacheHits: 0,
            },
        };
    }
    wrapError(err) {
        if (axios_1.default.isAxiosError(err)) {
            const axiosErr = err;
            if (axiosErr.code === 'ECONNREFUSED' || axiosErr.code === 'ENOTFOUND') {
                return new types_1.TokenPakConnectionError(this.baseUrl, err);
            }
            if (axiosErr.code === 'ECONNABORTED' || axiosErr.message?.includes('timeout')) {
                return new types_1.TokenPakTimeoutError(DEFAULT_TIMEOUT_MS);
            }
            return new types_1.TokenPakError(axiosErr.message, axiosErr.response?.status, err);
        }
        return new types_1.TokenPakError(String(err), undefined, err);
    }
}
exports.TokenPakHttpClient = TokenPakHttpClient;
//# sourceMappingURL=client.js.map
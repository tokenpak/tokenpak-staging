/**
 * TokenPak SDK — Unit Tests
 * Tests imports, class instantiation, type exports, and non-HTTP logic.
 * Does NOT require a live server.
 */

jest.mock('axios', () => ({
  __esModule: true,
  default: {
    create: jest.fn(),
    isAxiosError: jest.fn(() => false),
  },
}));

import axios from 'axios';
import {
  CompressionEngine,
  CacheManager,
  BlockRegistry,
  TelemetryCollector,
  TokenPakHttpClient,
  TokenPakError,
  TokenPakConnectionError,
  TokenPakTimeoutError,
  TokenPakUnsupportedEndpointError,
  VERSION,
} from '../src/index';

const axiosMock = axios as unknown as {
  create: jest.Mock;
};

let mockGet: jest.Mock;
let mockPost: jest.Mock;

beforeEach(() => {
  jest.clearAllMocks();
  mockGet = jest.fn();
  mockPost = jest.fn();
  axiosMock.create.mockReturnValue({ get: mockGet, post: mockPost });
});

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------
describe('SDK exports', () => {
  test('VERSION is a non-empty string', () => {
    expect(typeof VERSION).toBe('string');
    expect(VERSION.length).toBeGreaterThan(0);
  });

  test('all main classes are exported', () => {
    expect(CompressionEngine).toBeDefined();
    expect(CacheManager).toBeDefined();
    expect(BlockRegistry).toBeDefined();
    expect(TelemetryCollector).toBeDefined();
    expect(TokenPakHttpClient).toBeDefined();
  });

  test('error classes are exported', () => {
    expect(TokenPakError).toBeDefined();
    expect(TokenPakConnectionError).toBeDefined();
    expect(TokenPakTimeoutError).toBeDefined();
    expect(TokenPakUnsupportedEndpointError).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Error classes
// ---------------------------------------------------------------------------
describe('Error classes', () => {
  test('TokenPakError is an instance of Error', () => {
    const err = new TokenPakError('test error');
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(TokenPakError);
    expect(err.message).toBe('test error');
    expect(err.name).toBe('TokenPakError');
  });

  test('TokenPakConnectionError extends TokenPakError with correct message', () => {
    const err = new TokenPakConnectionError('http://localhost:9999');
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(TokenPakError);
    expect(err).toBeInstanceOf(TokenPakConnectionError);
    expect(err.message).toContain('http://localhost:9999');
    expect(err.name).toBe('TokenPakConnectionError');
  });

  test('TokenPakTimeoutError extends TokenPakError with correct message', () => {
    const err = new TokenPakTimeoutError(5000);
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(TokenPakError);
    expect(err).toBeInstanceOf(TokenPakTimeoutError);
    expect(err.message).toContain('5000');
    expect(err.name).toBe('TokenPakTimeoutError');
  });

  test('TokenPakUnsupportedEndpointError names the feature and endpoint', () => {
    const err = new TokenPakUnsupportedEndpointError('CacheManager', '/cache');
    expect(err).toBeInstanceOf(TokenPakError);
    expect(err.message).toContain('CacheManager');
    expect(err.message).toContain('/cache');
    expect(err.name).toBe('TokenPakUnsupportedEndpointError');
  });
});

// ---------------------------------------------------------------------------
// Instantiation (no HTTP calls needed)
// ---------------------------------------------------------------------------
describe('Class instantiation', () => {
  test('CompressionEngine can be constructed with no args', () => {
    const engine = new CompressionEngine();
    expect(engine).toBeDefined();
    expect(typeof engine.compress).toBe('function');
    expect(typeof engine.compressConversation).toBe('function');
  });

  test('CompressionEngine can be constructed with config', () => {
    const engine = new CompressionEngine({ baseUrl: 'http://localhost:9999', timeout: 5000 });
    expect(engine).toBeDefined();
  });

  test('CacheManager can be constructed', () => {
    const cache = new CacheManager({ baseUrl: 'http://localhost:9999' });
    expect(cache).toBeDefined();
    expect(typeof cache.get).toBe('function');
    expect(typeof cache.set).toBe('function');
    expect(typeof cache.delete).toBe('function');
    expect(typeof cache.clear).toBe('function');
    expect(typeof cache.stats).toBe('function');
  });

  test('BlockRegistry can be constructed', () => {
    const registry = new BlockRegistry({ baseUrl: 'http://localhost:9999' });
    expect(registry).toBeDefined();
    expect(typeof registry.register).toBe('function');
    expect(typeof registry.get).toBe('function');
    expect(typeof registry.list).toBe('function');
    expect(typeof registry.delete).toBe('function');
  });

  test('TelemetryCollector can be constructed', () => {
    const telemetry = new TelemetryCollector({ baseUrl: 'http://localhost:9999' });
    expect(telemetry).toBeDefined();
    expect(typeof telemetry.record).toBe('function');
    expect(typeof telemetry.stats).toBe('function');
  });

  test('TokenPakHttpClient can be constructed with custom config', () => {
    const client = new TokenPakHttpClient({
      baseUrl: 'http://localhost:9999',
      timeout: 1000,
      apiKey: 'test-key',
      headers: { 'X-Custom': 'value' },
    });
    expect(client).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// Shipped app API contract
// ---------------------------------------------------------------------------
describe('Shipped app API endpoints', () => {
  test('TokenPakHttpClient defaults to the proxy app API and X-TPK-Key auth', () => {
    new TokenPakHttpClient({ apiKey: 'test-key' });

    expect(axiosMock.create).toHaveBeenCalledWith(
      expect.objectContaining({
        baseURL: 'http://127.0.0.1:8766',
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
          'X-TPK-Key': 'test-key',
        }),
      })
    );
  });

  test('health uses the shipped /tpk/v1/health route', async () => {
    mockGet.mockResolvedValue({ data: { version: '1.2.3', uptime_s: 42 } });
    const client = new TokenPakHttpClient();

    const health = await client.health();

    expect(mockGet).toHaveBeenCalledWith('/tpk/v1/health');
    expect(health.version).toBe('1.2.3');
    expect(health.uptimeSeconds).toBe(42);
  });

  test('CompressionEngine.compress posts to /tpk/v1/compress and maps app response fields', async () => {
    mockPost.mockResolvedValue({
      data: {
        pruned_text: 'short text',
        original_tokens: 100,
        pruned_tokens: 25,
        tokens_avoided: 75,
        reduction_pct: 75,
      },
    });
    const engine = new CompressionEngine();

    const result = await engine.compress('long text', { targetTokens: 25 });

    expect(mockPost).toHaveBeenCalledWith(
      '/tpk/v1/compress',
      expect.objectContaining({ text: 'long text', max_tokens: 25 })
    );
    expect(result).toMatchObject({
      originalText: 'long text',
      compressedText: 'short text',
      originalTokens: 100,
      compressedTokens: 25,
      savingsPct: 75,
      cacheHit: false,
      elapsedMs: 0,
    });
  });

  test('legacy resource endpoints are disabled unless explicitly enabled', async () => {
    await expect(new CacheManager().set('key', 'value')).rejects.toBeInstanceOf(
      TokenPakUnsupportedEndpointError
    );
    await expect(new BlockRegistry().list()).rejects.toBeInstanceOf(
      TokenPakUnsupportedEndpointError
    );
    await expect(new TelemetryCollector().stats()).rejects.toBeInstanceOf(
      TokenPakUnsupportedEndpointError
    );
    await expect(new CompressionEngine().compressConversation([])).rejects.toBeInstanceOf(
      TokenPakUnsupportedEndpointError
    );
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('experimentalEndpoints opt-in preserves legacy endpoint calls', async () => {
    mockPost.mockResolvedValue({ data: undefined });
    const cache = new CacheManager({ experimentalEndpoints: true });

    await cache.set('key', 'value', 60);

    expect(mockPost).toHaveBeenCalledWith('/cache', { key: 'key', value: 'value', ttl: 60 });
  });
});

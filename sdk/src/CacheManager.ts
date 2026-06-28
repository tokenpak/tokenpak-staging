/**
 * TokenPak CacheManager
 * Wraps experimental /cache/* HTTP endpoints when a custom server provides them.
 */

import { TokenPakHttpClient } from './client';
import { CacheEntry, CacheStats, TokenPakConfig, TokenPakUnsupportedEndpointError } from './types';

interface RawCacheEntry {
  key: string;
  value: string;
  created_at: string;
  expires_at: string | null;
  hits: number;
}

interface RawCacheStats {
  total_entries: number;
  hit_rate: number;
  total_hits: number;
  total_misses: number;
  memory_usage_bytes: number;
}

export class CacheManager {
  private readonly client: TokenPakHttpClient;
  private readonly experimentalEndpoints: boolean;

  constructor(config?: TokenPakConfig) {
    this.client = new TokenPakHttpClient(config);
    this.experimentalEndpoints = config?.experimentalEndpoints ?? false;
  }

  private requireExperimentalEndpoint(path: string): void {
    if (!this.experimentalEndpoints) {
      throw new TokenPakUnsupportedEndpointError('CacheManager', path);
    }
  }

  /**
   * Get a cached value by key.
   * Returns null if the key is not found or has expired.
   */
  async get(key: string): Promise<string | null> {
    this.requireExperimentalEndpoint('/cache/{key}');
    try {
      const raw = await this.client.get<RawCacheEntry>(`/cache/${encodeURIComponent(key)}`);
      return raw.value;
    } catch {
      return null;
    }
  }

  /**
   * Store a value in the cache.
   *
   * @param key   Cache key
   * @param value Value to store
   * @param ttl   Time-to-live in seconds (0 = no expiry)
   */
  async set(key: string, value: string, ttl = 0): Promise<void> {
    this.requireExperimentalEndpoint('/cache');
    await this.client.post('/cache', { key, value, ttl });
  }

  /**
   * Delete a cached entry by key.
   */
  async delete(key: string): Promise<boolean> {
    this.requireExperimentalEndpoint('/cache/{key}/delete');
    try {
      await this.client.post(`/cache/${encodeURIComponent(key)}/delete`, {});
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Clear all cached entries.
   */
  async clear(): Promise<void> {
    this.requireExperimentalEndpoint('/cache/clear');
    await this.client.post('/cache/clear', {});
  }

  /**
   * Retrieve cache statistics (hit rate, entry count, memory usage).
   */
  async stats(): Promise<CacheStats> {
    this.requireExperimentalEndpoint('/cache/stats');
    const raw = await this.client.get<RawCacheStats>('/cache/stats');
    return {
      totalEntries: raw.total_entries,
      hitRate: raw.hit_rate,
      totalHits: raw.total_hits,
      totalMisses: raw.total_misses,
      memoryUsageBytes: raw.memory_usage_bytes,
    };
  }
}

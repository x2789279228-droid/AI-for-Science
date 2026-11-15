/**
 * 前端数据缓存层（性能修复）：内存 TTL + 在途请求去重 + 失效接口。
 *
 * 解决「同一份 /api/papers 被 AppShell/首页/图谱页/阅读器各自重复拉取」：
 * - TTL 内直接命中内存（同步级），过期后重取；
 * - 相同 URL 的并发请求共享同一次网络往返（in-flight 去重）；
 * - 写操作（上传/删除/改名/去重决策）经 invalidate() 立即失效相关缓存——
 *   服务端 ETag 用 no-cache 语义，永不脏读，本层负责消除重复往返。
 * 服务端 304 命中时 fetch 已免下载，这里再省掉重复发起。
 */

type Entry = { data: unknown; ts: number };

const store = new Map<string, Entry>();
const inflight = new Map<string, Promise<unknown>>();

function now(): number {
  return Date.now();
}

/** 带 TTL 的 JSON GET：命中内存直接返回；同 URL 并发共享一次网络请求。 */
export async function cachedJSON<T>(url: string, ttlMs: number, opts?: { force?: boolean }): Promise<T> {
  const hit = store.get(url);
  if (!opts?.force && hit && now() - hit.ts < ttlMs) {
    return hit.data as T;
  }
  const pending = inflight.get(url);
  if (pending && !opts?.force) {
    return pending as Promise<T>;
  }
  const p = (async () => {
    const r = await fetch(url, { cache: "no-cache" });   // ETag 重验证：304 免下载
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = (await r.json()) as T;
    store.set(url, { data, ts: now() });
    return data;
  })();
  inflight.set(url, p);
  try {
    return await p;
  } finally {
    inflight.delete(url);
  }
}

/** 失效以 prefix 开头的缓存（缺省全清）。写操作成功后按前缀精准失效。 */
export function invalidate(prefix?: string): void {
  if (!prefix) {
    store.clear();
    return;
  }
  for (const key of Array.from(store.keys())) {
    if (key.includes(prefix)) store.delete(key);
  }
}

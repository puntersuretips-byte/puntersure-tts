const B2_BUCKET = "puntersure-audio";
const B2_API_URL = "https://api.backblazeb2.com";
const B2_ALLOWED_ORIGINS = ["https://puntersure-tips.com"];

let authCache = { token: null, downloadUrl: null, expires: 0 };

async function getAuth(env) {
  if (authCache.token && Date.now() < authCache.expires) return authCache;
  const res = await fetch(`${B2_API_URL}/b2api/v3/b2_authorize_account`, {
    headers: {
      Authorization: "Basic " + btoa(`${env.B2_KEY_ID_CF}:${env.B2_APP_KEY_CF}`),
    },
  });
  if (!res.ok) throw new Error("authorize failed: " + res.status);
  const data = await res.json();
  authCache = {
    token: data.authorizationToken,
    downloadUrl: data.apiInfo.storageApi.downloadUrl,
    expires: Date.now() + 60 * 60 * 1000,
  };
  return authCache;
}

function addCors(headers) {
  const origin = headers.get("Origin");
  if (origin && B2_ALLOWED_ORIGINS.includes(origin)) {
    return {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
      "Access-Control-Allow-Headers": "Range, Content-Type",
      "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges, Content-Type",
      "Vary": "Origin",
    };
  }
  return {};
}

function parseRange(range, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!m) return null;
  let start = m[1] === "" ? null : parseInt(m[1], 10);
  let end = m[2] === "" ? null : parseInt(m[2], 10);
  if (start === null && end === null) return null;
  if (start === null) {
    start = Math.max(0, size - end);
    end = size - 1;
  } else {
    if (start >= size) return null;
    if (end === null || end >= size) end = size - 1;
    if (end < start) return null;
  }
  return { start, end };
}

const CACHE_HEADERS = {
  "content-type": "audio/mpeg",
  "cache-control": "public, max-age=31536000, immutable",
  "accept-ranges": "bytes",
};

async function serve(key, env, request, cors) {
  const url = new URL(request.url);
  const cache = caches.default;
  const cacheKey = new Request(url.origin + "/" + key, { method: "GET" });
  const rangeHeader = request.headers.get("Range");
  const wantHead = request.method === "HEAD";

  const cached = await cache.match(cacheKey);

  if (cached) {
    const bytes = await cached.arrayBuffer();
    const size = bytes.byteLength;
    if (rangeHeader) {
      const r = parseRange(rangeHeader, size);
      if (!r) {
        return new Response(null, { status: 416, headers: { ...cors, ...CACHE_HEADERS, "content-range": `bytes */${size}` } });
      }
      const slice = bytes.slice(r.start, r.end + 1);
      return new Response(wantHead ? null : slice, {
        status: 206,
        headers: { ...cors, ...CACHE_HEADERS, "content-range": `bytes ${r.start}-${r.end}/${size}`, "content-length": String(slice.byteLength) },
      });
    }
    return new Response(wantHead ? null : bytes, {
      status: 200,
      headers: { ...cors, ...CACHE_HEADERS, "content-length": String(size) },
    });
  }

  // Cache miss — fetch the FULL file from B2 (ignore Range) so we cache the
  // whole object and can serve any seek from the edge cache afterwards.
  const auth = await getAuth(env);
  const dlRes = await fetch(
    `${auth.downloadUrl}/file/${B2_BUCKET}/${encodeURIComponent(key)}`,
    { headers: { Authorization: auth.token } },
  );
  if (dlRes.status === 401 || dlRes.status === 404) {
    return new Response("Audio not found", { status: 404, headers: cors });
  }
  if (!dlRes.ok) {
    return new Response("Upstream error", { status: 502, headers: cors });
  }

  const fullBytes = await dlRes.arrayBuffer();
  const size = fullBytes.byteLength;

  await cache.put(
    cacheKey,
    new Response(fullBytes, { status: 200, headers: CACHE_HEADERS }),
  );

  if (rangeHeader) {
    const r = parseRange(rangeHeader, size);
    if (!r) {
      return new Response(null, { status: 416, headers: { ...cors, ...CACHE_HEADERS, "content-range": `bytes */${size}` } });
    }
    const slice = fullBytes.slice(r.start, r.end + 1);
    return new Response(wantHead ? null : slice, {
      status: 206,
      headers: { ...cors, ...CACHE_HEADERS, "content-range": `bytes ${r.start}-${r.end}/${size}`, "content-length": String(slice.byteLength) },
    });
  }
  return new Response(wantHead ? null : fullBytes, {
    status: 200,
    headers: { ...cors, ...CACHE_HEADERS, "content-length": String(size) },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") return new Response("ok");
    if (url.pathname === "/") {
      return new Response(
        "Puntersure audio proxy. Usage: /tts/<filename>.mp3",
        { headers: { "content-type": "text/plain" } },
      );
    }

    const key = url.pathname.replace(/^\//, "");
    if (!key.endsWith(".mp3")) {
      return new Response("Not found", { status: 404 });
    }

    const cors = addCors(request.headers);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    try {
      return await serve(key, env, request, cors);
    } catch (err) {
      return new Response("Proxy error", { status: 502, headers: cors });
    }
  },
};

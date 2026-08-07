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
      const auth = await getAuth(env);
      const headers = { Authorization: auth.token };
      const range = request.headers.get("Range");
      if (range) headers["Range"] = range;
      const dlRes = await fetch(
        `${auth.downloadUrl}/file/${B2_BUCKET}/${encodeURIComponent(key)}`,
        { headers },
      );
      if (dlRes.status === 401 || dlRes.status === 404) {
        return new Response("Audio not found", { status: 404, headers: cors });
      }
      if (!dlRes.ok) {
        return new Response("Upstream error", {
          status: 502,
          headers: cors,
        });
      }
      const out = {
        ...cors,
        "content-type": "audio/mpeg",
        "cache-control": "public, max-age=86400, immutable",
        "accept-ranges": "bytes",
      };
      const cr = dlRes.headers.get("Content-Range");
      const cl = dlRes.headers.get("Content-Length");
      if (cr) out["content-range"] = cr;
      if (cl) out["content-length"] = cl;
      return new Response(dlRes.body, { status: dlRes.status, headers: out });
    } catch (err) {
      return new Response("Proxy error", { status: 502, headers: cors });
    }
  },
};

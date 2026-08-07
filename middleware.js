// Vercel Edge Middleware: HTTP Basic auth across the whole deployment.
//
// This runs before every request Vercel serves — index.html, /api/*, all of
// it — so the dashboard and its data are behind a browser password prompt
// rather than public on the internet.
//
// Setup (Vercel dashboard -> Project -> Settings -> Environment Variables):
//
//   SITE_PASSWORD   required. The password.
//   SITE_USER       optional. Defaults to "nq".
//
// Set them for BOTH Production and Preview, or preview deployments will
// answer 503 (see below) and you will not be able to test a PR.
//
// **Fails closed.** With no SITE_PASSWORD set, this serves 503 to everything
// instead of falling back to open access. A guard that quietly disables
// itself when misconfigured is worse than no guard, because you would have
// no way to tell the difference from the outside.
//
// What this does NOT protect: the VPS deployment, which is a different
// machine running dashboard.py — use its own `--token` flag there. And this
// is transport-level auth over HTTPS, not per-user accounts: one password,
// shared, rotate it by changing the env var and redeploying.

const REALM = "NQ Toolkit";

function unauthorized(body) {
  return new Response(body, {
    status: 401,
    headers: {
      // Triggers the browser's native username/password prompt.
      "WWW-Authenticate": `Basic realm="${REALM}", charset="UTF-8"`,
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

// Compares without leaking the answer through how long it took. The edge
// runtime has no crypto.timingSafeEqual, so this is the manual version:
// always walk the full length, never return early on a mismatch.
function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  }
  return diff === 0;
}

export default function middleware(request) {
  const expectedPass = process.env.SITE_PASSWORD || "";
  const expectedUser = process.env.SITE_USER || "nq";

  if (!expectedPass) {
    return new Response(
      "This deployment is password protected, but SITE_PASSWORD is not set.\n" +
        "Set it in the Vercel project's environment variables and redeploy.\n",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8",
                                "Cache-Control": "no-store" } },
    );
  }

  const header = request.headers.get("authorization") || "";
  const [scheme, encoded] = header.split(" ");
  if (scheme !== "Basic" || !encoded) {
    return unauthorized("Authentication required.\n");
  }

  let decoded;
  try {
    decoded = atob(encoded);
  } catch {
    return unauthorized("Malformed credentials.\n");
  }

  // Only the first colon separates user from password; passwords may contain
  // colons and splitting on all of them would silently truncate them.
  const sep = decoded.indexOf(":");
  const user = sep === -1 ? decoded : decoded.slice(0, sep);
  const pass = sep === -1 ? "" : decoded.slice(sep + 1);

  // Both comparisons always run — short-circuiting on the username would
  // turn "is this a valid user?" into a separate, cheaper question.
  const userOk = safeEqual(user, expectedUser);
  const passOk = safeEqual(pass, expectedPass);
  if (!(userOk && passOk)) {
    return unauthorized("Invalid credentials.\n");
  }

  // Authorised: hand the request on to the static file or Python function.
  return undefined;
}

import { auth } from "@clerk/nextjs/server";
import { NextRequest, NextResponse } from "next/server";
import { buildBackendUrl } from "@/lib/proxy";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const { path } = await ctx.params;
  let url: string;
  try { url = buildBackendUrl(path, req.nextUrl.search, BACKEND); } catch { return NextResponse.json({ detail: "Bad path" }, { status: 400 }); }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  try {
    const res = await fetch(url, {
      method: req.method,
      headers: { Authorization: `Bearer ${token}`, ...(hasBody ? { "Content-Type": "application/json" } : {}) },
      body: hasBody ? await req.text() : undefined,
      cache: "no-store",
    });
    return new NextResponse(await res.text(), { status: res.status, headers: { "Content-Type": res.headers.get("content-type") ?? "application/json" } });
  } catch {
    return NextResponse.json({ detail: "Backend unreachable" }, { status: 503 });
  }
}
export { handler as GET, handler as POST, handler as PUT, handler as DELETE };

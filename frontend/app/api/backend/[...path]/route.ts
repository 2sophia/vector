import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";

const FASTAPI_URL =
  process.env.FASTAPI_URL ||
  `http://localhost:${process.env.SOPHIA_VECTOR_BACKEND_PORT || "8100"}`;

async function proxyRequest(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const session = await getServerSession(authOptions);
  if (!session?.user) {
    return Response.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const { path } = await params;
  // Il backend Sophia Vector espone le API sotto /v1/*
  const upstream = `${FASTAPI_URL}/v1/${path.join("/")}`;
  const url = new URL(upstream);

  // Forward query params
  const reqUrl = new URL(request.url);
  reqUrl.searchParams.forEach((value, key) => url.searchParams.set(key, value));

  // Build headers — inject user ID for backend isolation
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);

  const userId = (session.user as Record<string, unknown>).id as string;
  headers.set("x-user-id", userId);

  // Forward body for non-GET
  let body: ArrayBuffer | null = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    body = await request.arrayBuffer();
  }

  const resp = await fetch(url.toString(), {
    method: request.method,
    headers,
    body,
  });

  return new Response(resp.body, {
    status: resp.status,
    statusText: resp.statusText,
    headers: {
      "content-type": resp.headers.get("content-type") || "application/json",
    },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;

export const maxDuration = 300;
export const fetchCache = "force-no-store";

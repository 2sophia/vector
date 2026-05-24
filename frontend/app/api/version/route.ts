const FASTAPI_URL =
  process.env.FASTAPI_URL ||
  `http://localhost:${process.env.SOPHIA_VECTOR_BACKEND_PORT || "8100"}`;

export async function GET() {
  try {
    const res = await fetch(`${FASTAPI_URL}/health`, { next: { revalidate: 60 } });
    if (res.ok) {
      const data = await res.json();
      return Response.json({ version: data.version });
    }
  } catch {}
  return Response.json({ version: null });
}

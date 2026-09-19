// In-memory users (the load generator creates synthetic ones). Kept on globalThis so all route modules share it.
const g = globalThis as unknown as { __vayunxUsers?: Map<string, string> };
export const users: Map<string, string> = (g.__vayunxUsers ??= new Map());

export async function readCredentials(req: Request): Promise<{ username: string; password: string } | null> {
  try {
    const body = (await req.json()) as { username?: unknown; password?: unknown };
    return typeof body.username === "string" && typeof body.password === "string"
      ? { username: body.username, password: body.password } : null;
  } catch {
    return null;
  }
}

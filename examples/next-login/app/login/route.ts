import { verifyPassword } from "../../lib/passwords";
import { scope } from "../../lib/scope";
import { readCredentials, users } from "../../lib/users";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const c = await readCredentials(req);
  const stored = c ? users.get(c.username) : undefined;
  if (!c || stored === undefined) return Response.json({ error: "invalid credentials" }, { status: 401 });
  const ok = await scope("login", () => verifyPassword(stored, c.password));
  return ok ? Response.json({ ok: true }) : Response.json({ error: "invalid credentials" }, { status: 401 });
}

import { hashPassword } from "../../lib/passwords";
import { scope } from "../../lib/scope";
import { readCredentials, users } from "../../lib/users";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const c = await readCredentials(req);
  if (!c) return Response.json({ error: "username and password required" }, { status: 400 });
  const stored = await scope("register", () => hashPassword(c.password));
  if (users.has(c.username)) return Response.json({ error: "user exists" }, { status: 409 });
  users.set(c.username, stored);
  return Response.json({ username: c.username }, { status: 201 });
}

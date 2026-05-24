import { withAuth } from "next-auth/middleware";

/**
 * NextAuth gate.
 * Unauthenticated requests are redirected to `pages.signIn` from authOptions ("/auth").
 * The `matcher` below scopes the gate so it doesn't intercept public assets,
 * the auth handler itself, or the health-rewrite.
 */
export default withAuth({});

export const config = {
  matcher: [
    "/((?!auth|api/auth|api/version|_next/static|_next/image|favicon.ico|health|.*\\.svg).*)",
  ],
};

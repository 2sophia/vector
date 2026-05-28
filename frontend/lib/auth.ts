import CredentialsProvider from "next-auth/providers/credentials";
import AzureADProvider from "next-auth/providers/azure-ad";
import { compare, hash } from "bcryptjs";
import clientPromise from "./mongodb";
import type { NextAuthOptions } from "next-auth";
import type { Collection } from "mongodb";

async function getDb() {
  const client = await clientPromise;
  return client.db(process.env.AUTH_DB || "sophia_vector");
}

// Unique index su email: evita utenti duplicati e chiude la corsa (TOCTOU) sul
// bootstrap del primo admin — due login concorrenti non possono creare due record.
// Idempotente; guard in-memory per non ripetere la round-trip a ogni login.
let _userIndexesEnsured = false;
async function ensureUserIndexes(users: Collection) {
  if (_userIndexesEnsured) return;
  try {
    await users.createIndex({ email: 1 }, { unique: true });
    _userIndexesEnsured = true;
  } catch (e) {
    // email duplicate preesistenti o permessi mancanti: logga, non bloccare il login
    console.warn("ensureUserIndexes: unique index su users.email non creato:", e);
  }
}

export const authOptions: NextAuthOptions = {
  providers: [
    CredentialsProvider({
      name: "Credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.password) return null;

        const db = await getDb();
        const users = db.collection("users");
        await ensureUserIndexes(users);
        const email = credentials.email.toLowerCase();

        // Bootstrap del primo admin. Se BOOTSTRAP_ADMIN_EMAIL è impostata, SOLO
        // quell'email può diventare l'admin iniziale (chiude la finestra di takeover
        // su un deploy pubblicamente raggiungibile). Se non impostata, comportamento
        // legacy: il primo utente in assoluto diventa admin.
        const bootstrapEmail = process.env.BOOTSTRAP_ADMIN_EMAIL?.toLowerCase().trim();
        let user = await users.findOne({ email });
        if (!user) {
          const count = await users.countDocuments();
          const canBootstrap = count === 0 && (!bootstrapEmail || email === bootstrapEmail);
          if (canBootstrap) {
            const hashed = await hash(credentials.password, 12);
            try {
              const result = await users.insertOne({
                name: email.split("@")[0],
                email,
                password: hashed,
                role: "admin",
                isSuperAdmin: true,
                provider: "credentials",
                deny: false,
                createdAt: new Date(),
              });
              user = await users.findOne({ _id: result.insertedId });
            } catch (e: unknown) {
              // corsa concorrente: l'unique index ha respinto il secondo insert →
              // un altro login ha già creato l'utente, rileggilo.
              if ((e as { code?: number })?.code === 11000) {
                user = await users.findOne({ email });
              } else {
                throw e;
              }
            }
          }
        }

        if (!user) return null;
        if (user.deny) return null;
        if (!user.password) return null;

        const valid = await compare(credentials.password, user.password);
        if (!valid) return null;

        return {
          id: user._id.toString(),
          email: user.email,
          name: user.name,
          role: user.role || "user",
          isSuperAdmin: user.isSuperAdmin || user.role === "admin" || false,
        };
      },
    }),

    ...(process.env.AZURE_AD_CLIENT_ID
      ? [
          AzureADProvider({
            clientId: process.env.AZURE_AD_CLIENT_ID!,
            clientSecret: process.env.AZURE_AD_CLIENT_SECRET!,
            tenantId: process.env.AZURE_AD_TENANT_ID!,
          }),
        ]
      : []),
  ],

  session: { strategy: "jwt", maxAge: 24 * 60 * 60 },

  pages: {
    signIn: "/auth",
  },

  callbacks: {
    async signIn({ user, account, profile }) {
      if (account?.provider === "azure-ad") {
        try {
          const db = await getDb();
          const users = db.collection("users");

          const email = (profile?.email || user?.email)?.toLowerCase();
          const name = profile?.name || user?.name;

          if (!email) return false;

          // Domain allowlist. "*" disables the check (dev only).
          const allowedDomainsRaw = process.env.AZURE_AD_ALLOWED_DOMAINS ?? "*";
          const allowAny = allowedDomainsRaw.trim() === "*";
          const allowedDomains = allowedDomainsRaw
            .split(",")
            .map((d) => d.trim().toLowerCase().replace(/^@/, ""))
            .filter(Boolean);

          if (!allowAny && allowedDomains.length > 0) {
            const domain = email.split("@")[1] || "";
            if (!allowedDomains.includes(domain)) {
              console.warn(`Azure AD: rejecting login, domain not in allowlist: ${email}`);
              return false;
            }
          }

          await ensureUserIndexes(users);
          const existing = await users.findOne({ email });

          if (!existing) {
            // Stesso gate del flusso credentials: il primo admin è limitato a
            // BOOTSTRAP_ADMIN_EMAIL se impostata, altrimenti è il primo utente.
            const bootstrapEmail = process.env.BOOTSTRAP_ADMIN_EMAIL?.toLowerCase().trim();
            const isFirstUser =
              (await users.countDocuments()) === 0 &&
              (!bootstrapEmail || email === bootstrapEmail);
            await users.insertOne({
              email,
              name,
              provider: "azure-ad",
              azureId:
                (profile as Record<string, unknown>)?.sub ||
                (profile as Record<string, unknown>)?.oid,
              role: isFirstUser ? "admin" : "user",
              isSuperAdmin: isFirstUser,
              deny: false,
              createdAt: new Date(),
            });
          } else {
            if (existing.deny) return false;
            await users.updateOne(
              { _id: existing._id },
              { $set: { lastLogin: new Date() } }
            );
          }

          return true;
        } catch (error) {
          console.error("Azure AD signIn error:", error);
          return false;
        }
      }

      return true;
    },

    async jwt({ token, user, account, profile }) {
      if (account) {
        if (account.provider === "azure-ad") {
          const db = await getDb();
          const users = db.collection("users");
          const email = (
            ((profile as Record<string, unknown>)?.email as string) ||
            user?.email
          )?.toLowerCase();
          const dbUser = await users.findOne({ email });

          if (dbUser) {
            token.userId = dbUser._id.toString();
            token.name = dbUser.name;
            token.email = dbUser.email;
            token.role = dbUser.role || "user";
            token.isSuperAdmin = dbUser.isSuperAdmin || false;
          }
        } else if (user) {
          token.userId = user.id;
          token.role = (user as unknown as Record<string, unknown>).role;
          token.isSuperAdmin =
            (user as unknown as Record<string, unknown>).isSuperAdmin || false;
        }
      }
      return token;
    },

    async session({ session, token }) {
      if (session.user) {
        (session.user as Record<string, unknown>).id = token.userId;
        (session.user as Record<string, unknown>).role = token.role;
        (session.user as Record<string, unknown>).isSuperAdmin = token.isSuperAdmin;
      }
      return session;
    },
  },
};

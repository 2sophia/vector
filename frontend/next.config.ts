import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Permette l'accesso dev anche dall'IP di rete (oltre a localhost).
  allowedDevOrigins: ["192.168.1.9"],
  async rewrites() {
    return [
      {
        source: "/health",
        destination: `${process.env.FASTAPI_URL || "http://localhost:8100"}/health`,
      },
    ];
  },
};

export default nextConfig;

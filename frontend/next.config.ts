import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Convención del proyecto: dev local por subdominio (<slug>.lvh.me:3000,
  // ver CLAUDE.md) — sin esto, Next 16 bloquea como cross-origin todo el
  // tráfico de dev (RSC, HMR) que no venga de localhost y la app nunca hidrata.
  allowedDevOrigins: ["lvh.me", "*.lvh.me"],
};

export default nextConfig;

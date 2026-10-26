import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: false,
  },
  async redirects() {
    // Legacy /dashboard/* routes -> dashboard v2 equivalents.
    return [
      { source: "/dashboard", destination: "/portfolio", permanent: false },
      { source: "/dashboard/risk", destination: "/portfolio", permanent: false },
      { source: "/dashboard/orders", destination: "/portfolio", permanent: false },
      { source: "/dashboard/trades", destination: "/portfolio", permanent: false },
      { source: "/dashboard/ops", destination: "/ops", permanent: false },
      { source: "/dashboard/history", destination: "/ops", permanent: false },
      { source: "/dashboard/strategies", destination: "/controls", permanent: false },
      { source: "/dashboard/backtest", destination: "/controls", permanent: false },
      { source: "/dashboard/email", destination: "/controls", permanent: false },
    ];
  },
};

export default nextConfig;

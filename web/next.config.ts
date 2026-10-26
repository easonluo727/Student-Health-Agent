import type { NextConfig } from "next";

// STATIC_EXPORT=1 builds plain files into out/, which the agent service (workflow/server.py) serves from the same
// origin as its API (Dockerfile). Route files are .ts and pages .tsx, so the export leaves out the mock API routes.
const staticExport = process.env.STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  ...(staticExport
    ? { output: "export", pageExtensions: ["tsx", "jsx"], images: { unoptimized: true }, trailingSlash: true }
    : { cacheComponents: true, partialPrefetching: true }),
  turbopack: {
    rules: {
      "*.css": {
        condition: { not: "foreign" },
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;

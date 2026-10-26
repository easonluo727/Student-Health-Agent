// The only file that differs between the Lovable (Vite) copy and this Next.js app. Next inlines
// process.env.NEXT_PUBLIC_API_BASE at build time only when written with dot notation.
function readBase(): string | undefined {
  const value = process.env.NEXT_PUBLIC_API_BASE?.trim()
  return value ? value.replace(/\/+$/, "") : undefined
}

/** API base URL, or undefined → use the built-in mock transport. */
export const API_BASE = readBase();
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
export const UPLOAD_TYPES = ["application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"];
export const UPLOAD_ACCEPT = "application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif";

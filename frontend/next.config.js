/** @type {import('next').NextConfig} */

/**
 * The console is a static site.
 *
 * `output: "export"` produces plain files, which lets the backend serve the console from
 * the same origin as the API (see `backend/app/main.py`). One origin means one deployment
 * and no CORS configuration to get wrong; it also means the console works on any host that
 * can run one container.
 *
 * Consequence to know about: a statically exported site cannot pre-render an unknown
 * dynamic segment, so the room screen reads its room id from `?room=` rather than living
 * at `/rooms/[roomId]`. Every page here is `"use client"` and talks to the API at runtime,
 * so nothing else is lost by exporting.
 *
 * `NEXT_PUBLIC_API_BASE` is baked in at build time. Empty means "same origin as this page",
 * which is what the container build passes; a dev run defaults to the local backend.
 */
const nextConfig = {
  output: "export",
  // Emits `room/index.html` instead of `room.html`, which is what a static file server
  // needs in order to serve `/room/` without special-case rewrite rules.
  trailingSlash: true,
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000",
  },
};

module.exports = nextConfig;

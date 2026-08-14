import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Room",
  description: "Temporary shared rooms where AI agents belonging to different humans coordinate.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Rooms",
  description:
    "A provider-neutral live collaboration network for independently owned AI agents. " +
    "Shared work awareness and coordination, not chat.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

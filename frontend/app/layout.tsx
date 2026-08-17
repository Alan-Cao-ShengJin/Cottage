import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cottage — AI agents working as one team",
  description:
    "A shared workspace where independent AI agents coordinate work across models, " +
    "vendors, and runtimes.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Paper Trading Research",
  description: "Probabilistic paper-trading research system"
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "RecoverAI Dashboard",
  description: "Autonomous revenue recovery agent — merchant dashboard, metrics and explainable audit timeline",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-zinc-50 text-zinc-900">
        <header className="border-b border-zinc-200 bg-white">
          <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
            <Link href="/" className="text-lg font-semibold tracking-tight">
              RecoverAI
            </Link>
            <nav className="flex gap-4 text-sm text-zinc-600">
              <Link href="/" className="hover:text-zinc-900">
                Dashboard
              </Link>
              <Link href="/cases" className="hover:text-zinc-900">
                Cases
              </Link>
            </nav>
            <span className="ml-auto rounded-full border border-amber-300 bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800">
              Demo mode — recovery actions &amp; outcomes are simulated
            </span>
          </div>
        </header>
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">{children}</main>
        <footer className="border-t border-zinc-200 bg-white py-3">
          <div className="mx-auto w-full max-w-6xl px-4 text-xs text-zinc-500">
            RecoverAI · Razorpay Buildathon · every displayed number is computed from stored data
          </div>
        </footer>
      </body>
    </html>
  );
}

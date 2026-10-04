import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dave Inspector",
  description: "What the LLM and Jev saw at each decision of a recorded Dangerous Dave episode",
};

// Bundles change whenever `dave-agent inspect` runs again, so every page reads them per request.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <header className="site">
          <Link href="/" className="brand">
            Dave Inspector
          </Link>
          <Link href="/">Inspected runs</Link>
          <Link href="/live">Live</Link>
          <span className="muted">local view of `dave-agent inspect` bundles and `dave-agent live` runs</span>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
